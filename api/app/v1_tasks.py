"""OpsMemory v1 task creation API (Phase UI-2B3-1, Codex Option D).

POST /v1/tasks/preview   — synchronous deterministic dedup search
                            (no LLM, business-scoped). Returns up to
                            5 candidate tasks the operator might be
                            duplicating. Idempotent (no side effects).

POST /v1/tasks           — atomic create with idempotency-replay,
                            commit-time retrieve recheck, business-
                            scoped owner resolution, and a synthetic
                            auto-approved review_item for audit.

This endpoint replaces the stop-gap POST /v1/quick_tasks shipped in
UI-2B2. It exists to give "manual create" first-class footing
alongside the LLM pipeline path: same on-disk shape (via
core_apply.create_task_inner), same audit chain (ingest_event ->
review_item -> task), same idempotency contract (client_mutations).

Why a separate endpoint instead of nesting under v1_quick_tasks:
  - v1_quick_tasks was named after the UI affordance. Several other
    surfaces will create tasks too (Slack /add, MCP write mode,
    mobile share-sheet) and they should call /v1/tasks too. The
    domain command is "create a task"; Quick Add is one consumer.

Why bypass the async LLM pipeline:
  - For typed input the operator IS the proposer — extract+choose
    do not add value.
  - Latency: pipeline is worker-based (5-min timer); the operator
    expects sub-second feedback.
  - Cost: choose+extract LLM calls would cost ~$0.001 per typed
    task with no upside.
  - We DO reuse retrieve (deterministic SQL, $0) so the operator
    gets duplicate-detection that the pipeline path provides.

Concurrency / TOCTOU note (Codex Option D blocker P4):
  preview returns a `preview_token` (epoch ms of the search). On
  commit, if the client passes the token, we re-run retrieve once
  inside the apply transaction; if a NEW high-similarity candidate
  appeared since the preview, we 409 with the new candidates. The
  client surfaces the dedup modal again. Without the token (or
  with `force_create=true`), commit skips the recheck.

Authz:
  user principals only. visible_business_ids must include the
  requested business slug (admins None = unrestricted; owners
  scoped to their memberships). Service principals 403.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .auth import Principal, require_principal
from .authz import visible_business_ids
from .core_apply import CoreApplyError, create_task_inner
from .reconciliation.retrieve import retrieve_candidates

log = logging.getLogger("opsmemory.v1_tasks")

router = APIRouter(prefix="/v1/tasks")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PreviewBody(BaseModel):
    """Dedup-search payload. Same fields as create except no
    idempotency_key, no force_create."""
    summary: str = Field(..., min_length=1, max_length=4096)
    business_slug: str = Field(..., min_length=1, max_length=64)
    due_at: str | None = Field(default=None, max_length=64)


class CreateBody(BaseModel):
    """Manual create payload.

    summary, business_slug         — required.
    due_at                         — optional ISO-8601.
    category, kind                 — optional. kind='event' folds into
                                     category='event' (read-model
                                     filter; schema split is future).
    description                    — optional notes.
    owner_user_id                  — optional already-resolved user id.
                                     Server validates membership in the
                                     selected business.
    owner_display                  — optional free-text name; server
                                     resolves via display_name ILIKE
                                     SCOPED to business members.
                                     Multiple matches => 409.
    idempotency_key                — required, ≤128 chars. PWA generates
                                     a fresh UUID per submit.
    force_create                   — bool; when true, skip TOCTOU
                                     recheck on commit (operator
                                     already saw and dismissed dedup
                                     hits).
    preview_token                  — opaque string from a prior
                                     /preview response. When supplied
                                     and force_create=false, commit
                                     re-runs retrieve and returns 409
                                     duplicate_candidates_changed if a
                                     new high-similarity candidate
                                     appeared.
    """
    summary: str = Field(..., min_length=1, max_length=4096)
    business_slug: str = Field(..., min_length=1, max_length=64)
    due_at: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=8192)
    owner_user_id: str | None = Field(default=None, max_length=64)
    owner_display: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    force_create: bool = False
    preview_token: str | None = Field(default=None, max_length=64)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Codex Option D answer A1: cap retrieve runtime so a runaway lex-scan
# can't hold the request thread. Postgres stops the query and returns
# an error; we surface as 503 with retry hint.
_RETRIEVE_STATEMENT_TIMEOUT_MS = 1500
_PREVIEW_TOP_N = 5
# Codex Option D recommendation: scope retrieve to OPEN active tasks
# for typed input (caller wants to know about open tasks they might
# be duplicating, not historical done ones).
_PREVIEW_REQUIRE_OPEN = True


def _coerce_due_at(raw: str | None) -> datetime | None:
    if raw is None or raw == "":
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "bad_due_at", "detail": str(exc)},
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _resolve_business(conn, slug: str, principal_visible: list[str] | None
                            ) -> dict:
    """Return {id, slug, name} or raise 404/403."""
    row = await conn.fetchrow(
        "SELECT id::text AS id, slug::text AS slug, name "
        "FROM businesses "
        "WHERE slug::text = $1 AND deletion_state = 'active'",
        slug,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "business_not_found",
                    "detail": f"business_slug={slug!r}"},
        )
    if principal_visible is not None and row["id"] not in principal_visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "business_not_visible",
                    "detail": "you don't have access to this business"},
        )
    return dict(row)


async def _resolve_owner(conn, *, business_id: str, owner_user_id: str | None,
                         owner_display: str | None) -> str | None:
    """Resolve owner to a user_id, scoped to the business's members.

    Returns None when neither field is supplied. Raises 409 on
    ambiguous display match (multiple users) — Codex Option D answer
    A6: silent global ILIKE is multi-tenant unsafe.
    """
    if owner_user_id:
        row = await conn.fetchrow(
            """
            SELECT u.id::text AS id
              FROM users u
              JOIN business_memberships bm ON bm.user_id = u.id
             WHERE u.id = $1::uuid
               AND u.status = 'active'
               AND bm.business_id = $2::uuid
               AND bm.status = 'active'
            """,
            owner_user_id, business_id,
        )
        if not row:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "owner_not_in_business",
                        "detail": "owner_user_id is not a member of the "
                                  "selected business"},
            )
        return row["id"]
    if owner_display and owner_display.strip():
        rows = await conn.fetch(
            """
            SELECT u.id::text AS id, u.display_name AS display_name
              FROM users u
              JOIN business_memberships bm ON bm.user_id = u.id
             WHERE u.display_name ILIKE $1
               AND u.status = 'active'
               AND bm.business_id = $2::uuid
               AND bm.status = 'active'
            """,
            owner_display.strip(), business_id,
        )
        if not rows:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "owner_display_not_found",
                        "detail": f"no business member named {owner_display!r}"},
            )
        if len(rows) > 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "owner_display_ambiguous",
                        "detail": f"{len(rows)} business members match "
                                  f"{owner_display!r}",
                        "matches": [{"user_id": r["id"],
                                     "display_name": r["display_name"]}
                                    for r in rows]},
            )
        return rows[0]["id"]
    return None


async def _run_retrieve(conn, *, summary: str, business_slug: str,
                        business_id: str, due_at_iso: str | None,
                        actor_business_ids: list[str] | None,
                        top_n: int) -> list[dict]:
    """Bounded sync retrieve. Returns rows; raises 503 on timeout."""
    candidate = {
        "summary": summary,
        "businesses": [business_slug],
        "due_at": due_at_iso,
    }
    business_id_by_slug = {business_slug: business_id}
    try:
        async with conn.transaction():
            await conn.execute(
                f"SET LOCAL statement_timeout = {_RETRIEVE_STATEMENT_TIMEOUT_MS}"
            )
            rows, _skipped = await retrieve_candidates(
                conn,
                candidate,
                top_n=top_n,
                business_id_by_slug=business_id_by_slug,
                actor_business_ids=actor_business_ids,
                # Recency fallback for the no-due-at case so we don't
                # surface dormant 2-year-old tasks as "duplicates".
                recency_fallback_days=180,
            )
    except asyncpg.QueryCanceledError:
        log.warning("retrieve_timeout", extra={"summary": summary[:80]})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "retrieve_timeout",
                    "detail": "duplicate search timed out — try again"},
        )

    if _PREVIEW_REQUIRE_OPEN:
        rows = [r for r in rows if r.get("status") == "open"]

    # Slim the response shape — operator UI only needs id, summary,
    # status, due_at, and the score so we can sort.
    return [{
        "id": r["id"],
        "summary": r["summary"],
        "status": r["status"],
        "due_at": r.get("due_at"),
        "last_activity_at": r.get("last_activity_at"),
        "lex_score": int(r.get("lex_score") or 0),
        "businesses": list(r.get("businesses") or []),
    } for r in rows]


# ---------------------------------------------------------------------------
# POST /v1/tasks/preview
# ---------------------------------------------------------------------------

@router.post("/preview")
async def preview_task(
    body: PreviewBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Search for existing tasks that look like this proposal.

    No side effects; safe to call repeatedly. The PWA calls this on
    Submit before committing so the operator can disambiguate.
    """
    if principal.principal_type != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="task preview is a human-only endpoint",
        )

    visible = visible_business_ids(principal)
    if visible is not None and len(visible) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no business visibility",
        )

    pool = request.app.state.db
    async with pool.acquire() as conn:
        biz = await _resolve_business(conn, body.business_slug, visible)
        due_at = _coerce_due_at(body.due_at)
        candidates = await _run_retrieve(
            conn,
            summary=body.summary,
            business_slug=biz["slug"],
            business_id=biz["id"],
            due_at_iso=due_at.isoformat() if due_at else None,
            actor_business_ids=visible,
            top_n=_PREVIEW_TOP_N,
        )

    return {
        "candidates": candidates,
        # Token is wall-clock ms — opaque to the client. Used at
        # commit time to recheck whether new candidates appeared
        # since this preview returned.
        "preview_token": str(int(time.time() * 1000)),
    }


# ---------------------------------------------------------------------------
# POST /v1/tasks
# ---------------------------------------------------------------------------

@router.post("")
async def create_task(
    body: CreateBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Create a task atomically. Idempotent on idempotency_key."""
    if principal.principal_type != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="task create is a human-only endpoint",
        )

    summary = body.summary.strip()
    if not summary:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "summary_required",
                    "detail": "summary cannot be blank"},
        )
    due_at_dt = _coerce_due_at(body.due_at)
    # kind='event' folds into category for the read model.
    category = body.category
    if (body.kind or "").strip().lower() == "event":
        category = "event"

    visible = visible_business_ids(principal)
    if visible is not None and len(visible) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no business visibility",
        )

    payload = {
        "action": "create_task",
        "summary": summary,
        "business_slug": body.business_slug,
        "due_at": due_at_dt.isoformat() if due_at_dt else None,
        "category": category,
        "kind": body.kind,
        "owner_user_id": body.owner_user_id,
        "owner_display": body.owner_display,
    }

    pool = request.app.state.db
    async with pool.acquire() as conn:
        # ----- Idempotency replay -----
        prior = await _replay_existing(conn, body.idempotency_key)
        if prior is not None:
            return _emit_replay(prior)

        # Resolve business + owner OUTSIDE the apply txn so error
        # messages return clean 4xx without rolling back later writes.
        biz = await _resolve_business(conn, body.business_slug, visible)
        owner_user_id = await _resolve_owner(
            conn,
            business_id=biz["id"],
            owner_user_id=body.owner_user_id,
            owner_display=body.owner_display,
        )
        # Codex round-2 blocker: when no owner is specified, default to
        # the creator. The B2 path auto-assigned the creator; without
        # this default an owner-role user could create tasks they then
        # can't complete (mutations.toggle_done rejects with
        # 'task_not_assigned'). We still validate the creator's
        # business membership to keep the resolver-side invariant
        # (owner_user_id always passes through _resolve_owner's
        # business-scoped check).
        if owner_user_id is None and principal.principal_type == "user":
            try:
                owner_user_id = await _resolve_owner(
                    conn,
                    business_id=biz["id"],
                    owner_user_id=principal.id,
                    owner_display=None,
                )
            except HTTPException as exc:
                # If the creator isn't a member of the selected business
                # (admins are visible without membership rows), leave
                # owner_user_id=None — the task is created unassigned
                # and the creator (an admin) can assign later.
                if (isinstance(exc.detail, dict)
                        and exc.detail.get("code") == "owner_not_in_business"):
                    owner_user_id = None
                else:
                    raise

        # ----- Apply transaction -----
        # Race recovery (Codex B3 blocker): if a concurrent retry
        # beats this one to the idempotency key, the inner INSERT
        # raises UniqueViolationError, the txn rolls back, and we
        # re-fetch the row outside the rolled-back txn to replay
        # the cached response. If the concurrent request is still
        # 'received' (apply mid-flight), surface 503.
        try:
          async with conn.transaction():
            # Insert client_mutations row first to claim the
            # idempotency key. UniqueViolation on a concurrent retry
            # rolls back the whole txn (no orphan ingest_event,
            # review_item, or task partial state).
            #
            # Indent note: the `async with` block body sits at 12
            # spaces (2 in from `async with` at 10, which is 2 in
            # from `try:` at 8). The 2-space steps are unusual but
            # avoid having to re-indent ~200 lines of pre-existing
            # txn body. Future edits should preserve relative depth.
            await conn.execute(
                """
                INSERT INTO client_mutations
                  (idempotency_key, actor_user_id,
                   client_id, payload, status, request_id)
                VALUES
                  ($1, $2::uuid, $3, $4::jsonb, 'received', $5)
                """,
                body.idempotency_key,
                principal.id,
                "pwa-quick-add",
                payload,  # JSONB codec encodes
                getattr(request.state, "request_id", None),
            )

            # ----- Commit-time retrieve recheck (TOCTOU defense) -----
            new_candidates: list[dict] = []
            if body.preview_token and not body.force_create:
                rechecked = await _run_retrieve(
                    conn,
                    summary=summary,
                    business_slug=biz["slug"],
                    business_id=biz["id"],
                    due_at_iso=due_at_dt.isoformat() if due_at_dt else None,
                    actor_business_ids=visible,
                    top_n=_PREVIEW_TOP_N,
                )
                # If ANY high-similarity candidate exists at commit
                # time, surface them and let the operator re-decide.
                # The strict choice here is "any candidate" — a soft
                # filter (lex_score >= 2) could be revisited.
                if rechecked:
                    new_candidates = rechecked

            if new_candidates:
                # Persist the conflict record so the same idempotency
                # key replayed returns the same 409 (Codex chunk-6-
                # step1: idempotency replay must serve cached failure
                # rows, not just successes).
                conflict_payload = {
                    "code": "duplicate_candidates_changed",
                    "detail": "new candidates appeared since preview",
                    "candidates": new_candidates,
                }
                await conn.execute(
                    """
                    UPDATE client_mutations
                       SET status = 'conflict',
                           response_status = 409,
                           error_payload = $2::jsonb,
                           applied_at = now(),
                           rejection_reason = 'duplicate_candidates_changed'
                     WHERE idempotency_key = $1
                    """,
                    body.idempotency_key,
                    conflict_payload,  # JSONB codec encodes
                )
                raise HTTPException(status_code=409, detail=conflict_payload)

            # ----- Synthesize ingest_event -----
            req_uuid = str(uuid.uuid4())
            ts_iso = datetime.now(timezone.utc).isoformat()
            envelope = (
                f"[manual_quick_add {ts_iso} req:{req_uuid}]\n"
                f"summary: {summary}\n"
                f"business: {body.business_slug}\n"
                f"due_at: {due_at_dt.isoformat() if due_at_dt else ''}\n"
                f"category: {category or ''}\n"
                f"description: {body.description or ''}"
            )
            normalized_hash = hashlib.sha256(envelope.encode("utf-8")).hexdigest()
            event_row = await conn.fetchrow(
                """
                INSERT INTO ingest_events
                  (source, source_external_id,
                   raw_content, normalized_hash, source_metadata,
                   status, received_at, processed_at,
                   actor_type, actor_user_id,
                   request_id)
                VALUES
                  ('manual_quick_add', $1,
                   $2, $3, $4::jsonb,
                   'processed', now(), now(),
                   'user', $5::uuid,
                   $6)
                RETURNING id::text AS id
                """,
                req_uuid,
                envelope,
                normalized_hash,
                {"channel": "pwa_quick_add",
                 "idempotency_key": body.idempotency_key},
                principal.id,
                req_uuid,
            )
            ingest_event_id = event_row["id"]

            # ----- Synthesize auto-approved review_item (audit row) -----
            # Codex answer Q4: every task on disk has a review_item_id
            # audit chain entry. Operator-typed Quick Adds get
            # was_auto_approved=true so the Triage UI excludes them
            # from the "Completed today" sub-tab.
            review_item_row = await conn.fetchrow(
                """
                INSERT INTO review_items
                  (ingest_event_id, proposed_action, target_task_id,
                   proposed_patch, candidate_facts, retrieved_candidates,
                   confidence, reason,
                   status, reviewer_id, reviewed_at, applied_at,
                   was_auto_approved,
                   request_id)
                VALUES
                  ($1::uuid, 'CREATE_TASK', NULL,
                   $2::jsonb, $3::jsonb, '[]'::jsonb,
                   1.0, $4,
                   'approved', $5::uuid, now(), now(),
                   true,
                   $6)
                RETURNING id::text AS id
                """,
                ingest_event_id,
                {"create": {
                    "summary": summary,
                    "businesses": [body.business_slug],
                    "due_at": due_at_dt.isoformat() if due_at_dt else None,
                    "category": category,
                    "description": body.description,
                    "owner_user_id": owner_user_id,
                }},
                {"summary": summary,
                 "businesses": [body.business_slug]},
                "manual quick add",
                principal.id,
                req_uuid,
            )
            review_item_id = review_item_row["id"]

            # ----- Apply via shared core_apply primitive -----
            try:
                task_id = await create_task_inner(
                    conn,
                    ingest_event_id=ingest_event_id,
                    summary=summary,
                    business_ids=[{"id": biz["id"], "slug": biz["slug"]}],
                    due_at=due_at_dt,
                    # For manual create the operator typed the ISO,
                    # so isoformat() of the parsed datetime IS the
                    # canonical form. Store that.
                    due_at_for_history=(due_at_dt.isoformat()
                                        if due_at_dt else None),
                    description=body.description,
                    category=category,
                    priority=None,
                    dependency_text=None,
                    owner_user_id=owner_user_id,
                    actor_user_id=principal.id,
                    actor_service_id=None,
                    actor_type="user",
                    mutation_id=f"client:{body.idempotency_key}",
                    history_reason="manual create via PWA",
                    history_confidence=1.0,
                    transition_reason="manual create via PWA",
                    transition_metadata={"review_item_id": review_item_id,
                                         "ingest_event_id": ingest_event_id,
                                         "via": "manual_quick_add"},
                )
            except CoreApplyError as exc:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"code": "core_apply_failed",
                            "detail": str(exc)},
                )

            # Patch the review_item with the resolved task id (audit
            # tip — applied_task_id closes the loop pipeline-side
            # too, so Triage detail can show "this auto-approved
            # entry created task X").
            await conn.execute(
                """
                UPDATE review_items
                   SET applied_task_id = $2::uuid,
                       apply_mutation_id = $3
                 WHERE id = $1::uuid
                """,
                review_item_id,
                task_id,
                f"client:{body.idempotency_key}",
            )

            # ----- Persist success on client_mutations -----
            response_payload = {
                "task_id": task_id,
                "summary": summary,
                "status": "open",
                "due_at": due_at_dt.isoformat() if due_at_dt else None,
                "business_slug": body.business_slug,
                "category": category,
                "owner_user_id": owner_user_id,
                "review_item_id": review_item_id,
            }
            await conn.execute(
                """
                UPDATE client_mutations
                   SET status = 'applied',
                       response_status = 200,
                       result_payload = $2::jsonb,
                       task_id = $3::uuid,
                       applied_at = now()
                 WHERE idempotency_key = $1
                """,
                body.idempotency_key,
                response_payload,  # JSONB codec encodes
                task_id,
            )
        except asyncpg.UniqueViolationError:
            # Concurrent retry won the race. Re-fetch the cached
            # response from the row that beat us. If it's still
            # 'received' (apply mid-flight), tell the client to
            # retry (503).
            prior = await _replay_existing(conn, body.idempotency_key)
            if prior is not None:
                return _emit_replay(prior)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "idempotency_in_flight",
                        "detail": "concurrent request still applying — retry"},
            )

    log.info("task_created", extra={
        "task_id": task_id,
        "creator_id": principal.id,
        "business_slug": body.business_slug,
        "kind": body.kind or "task",
        "idempotency_key": body.idempotency_key,
    })
    return response_payload


# ---------------------------------------------------------------------------
# Idempotency helpers (mirrors v1_mutations._replay_existing pattern)
# ---------------------------------------------------------------------------

async def _replay_existing(conn, idempotency_key: str
                           ) -> tuple[int, dict] | None:
    """Return (status_code, body) for a prior mutation, or None if new."""
    row = await conn.fetchrow(
        """
        SELECT status, response_status, result_payload, error_payload,
               task_id::text         AS task_id,
               payload->>'action'    AS action
        FROM client_mutations
        WHERE idempotency_key = $1
        """,
        idempotency_key,
    )
    if not row:
        return None
    if row["action"] != "create_task":
        return (409, {
            "code": "idempotency_action_mismatch",
            "detail": (f"idempotency_key already used for action "
                       f"{row['action']!r}; this request was for "
                       f"'create_task'"),
        })
    if row["status"] == "applied":
        return (row["response_status"] or 200, row["result_payload"])
    if row["status"] == "conflict":
        return (row["response_status"] or 409, row["error_payload"])
    if row["status"] == "rejected":
        return (row["response_status"] or 422, row["error_payload"])
    # 'received' — apply still in flight; treat as collision.
    return None


def _emit_replay(prior: tuple[int, dict]):
    status_code, body = prior
    if status_code == 200:
        return {**body, "deduped": True}
    raise HTTPException(status_code=status_code,
                        detail={**body, "deduped": True})

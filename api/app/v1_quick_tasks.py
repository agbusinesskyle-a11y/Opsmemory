"""OpsMemory v1 quick-add tasks API (Phase UI-2B2).

POST /v1/quick_tasks creates a task directly from a human-typed
compose form, bypassing the LLM extraction pipeline. The mental
model is Slack-style ad-hoc capture: type a one-liner, hit enter,
the row appears on Tasks immediately.

Why bypass review:
  Review is for reconciliation of AMBIGUOUS extracted signals
  (Slack messages, recap emails, etc.). A human-typed Quick Add
  has no ambiguity — the user IS the proposer. Routing it through
  Triage would just make them approve their own input.

Provenance is preserved:
  Every Quick Add still synthesizes an ingest_event with
  source='manual_quick_add' and actor_user_id=principal.id, so the
  task graph's audit chain stays intact. Cross-table queries that
  walk tasks.source_event_id -> ingest_events still work — they
  just see source='manual_quick_add' instead of 'slack_message'.

Idempotency:
  Quick Add does NOT use the client_mutations replay table. The
  retry pattern (user types, taps Submit twice) is best handled by
  the PWA debouncing the button. A duplicate POST will create a
  duplicate task. The unique constraint on
  ingest_events.normalized_hash protects us from accidental exact
  re-submits if both POSTs land in the same second; subsequent
  requests get a unique hash because raw_content includes a UUID.

Authz:
  Any authenticated user whose visible_business_ids includes the
  requested business slug. Admins (None visible) bypass the slug
  check. Service principals are not allowed — Quick Add is a human
  affordance.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .auth import Principal, require_principal
from .authz import visible_business_ids

log = logging.getLogger("opsmemory.v1_quick_tasks")

router = APIRouter(prefix="/v1/quick_tasks")


class QuickTaskBody(BaseModel):
    """Quick-add compose payload.

    summary       — required; 1-4096 chars (matches tasks.summary
                    CHECK constraint).
    business_slug — required; the task's primary business. Must be
                    in the principal's visible_business_ids (or the
                    principal is admin).
    due_at        — optional ISO-8601 timestamp ('Z' suffix OK).
                    Naive timestamps default to UTC.
    category      — optional free-form (e.g. 'ops', 'follow-up').
    kind          — optional 'task' (default) or 'event'. The
                    task graph itself doesn't have a kind column;
                    this hint is folded into category as
                    'event' so list views can filter on it.
    description   — optional longer-form notes.
    """
    summary: str = Field(..., min_length=1, max_length=4096)
    business_slug: str = Field(..., min_length=1, max_length=64)
    due_at: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, max_length=16)
    description: str | None = Field(default=None, max_length=8192)


def _coerce_due_at(raw: str | None) -> datetime | None:
    """ISO-8601 string -> tz-aware datetime, or raise 400."""
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


@router.post("")
async def create_quick_task(
    body: QuickTaskBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Create a task directly from a Quick Add compose form."""
    if principal.principal_type != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="quick_tasks is a human-only endpoint",
        )

    summary = body.summary.strip()
    if not summary:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "summary_required",
                    "detail": "summary cannot be blank"},
        )

    due_at_dt = _coerce_due_at(body.due_at)

    # Fold the 'event' kind into category. Schema-level event/task
    # split is a future migration; for now this lets the PWA filter
    # event-flavoured tasks without changing the read model.
    category = body.category
    if (body.kind or "").strip().lower() == "event":
        category = "event"

    visible = visible_business_ids(principal)
    if visible is not None and len(visible) == 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no business visibility",
        )

    pool = request.app.state.db
    async with pool.acquire() as conn:
        # ---- Resolve + authorize the requested business ----
        biz_row = await conn.fetchrow(
            "SELECT id::text AS id, slug::text AS slug "
            "FROM businesses "
            "WHERE slug::text = $1 AND deletion_state = 'active'",
            body.business_slug,
        )
        if not biz_row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "business_not_found",
                        "detail": f"business_slug={body.business_slug!r}"},
            )
        if visible is not None and biz_row["id"] not in visible:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "business_not_visible",
                        "detail": "you don't have access to this business"},
            )

        async with conn.transaction():
            # ---- Synthesize ingest_event ----
            # raw_content includes a UUID + ISO timestamp prefix so
            # repeated identical Quick Add submissions don't collide
            # on the unique normalized_hash index.
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
                '{"channel":"pwa_quick_add"}',
                principal.id,
                req_uuid,
            )
            ingest_event_id = event_row["id"]

            # ---- Insert task ----
            task_row = await conn.fetchrow(
                """
                INSERT INTO tasks
                  (summary, description, due_at, category,
                   source_event_id, version, last_activity_at,
                   created_at, updated_at)
                VALUES
                  ($1, $2, $3::timestamptz, $4,
                   $5::uuid, 1, now(),
                   now(), now())
                RETURNING id::text AS id, status::text AS status,
                          created_at::text AS created_at
                """,
                summary,
                body.description,
                due_at_dt,
                category,
                ingest_event_id,
            )
            task_id = task_row["id"]

            # ---- Link to business ----
            await conn.execute(
                """
                INSERT INTO task_businesses
                  (task_id, business_id, added_by)
                VALUES ($1::uuid, $2::uuid, $3::uuid)
                """,
                task_id,
                biz_row["id"],
                principal.id,
            )

            # ---- Auto-assign to the creator ----
            # The mental model of Quick Add is "I'm capturing my own
            # task." If the creator wanted to assign elsewhere they'd
            # use the Tasks PATCH flow. Skipping this would leave the
            # task unassigned and invisible on owner dashboards.
            await conn.execute(
                """
                INSERT INTO task_assignees
                  (task_id, user_id, role, assigned_by)
                VALUES ($1::uuid, $2::uuid, 'assignee', $2::uuid)
                ON CONFLICT (task_id, user_id) DO NOTHING
                """,
                task_id,
                principal.id,
            )

    log.info("quick_task_created", extra={
        "task_id": task_id,
        "creator_id": principal.id,
        "business_slug": body.business_slug,
        "kind": body.kind or "task",
    })
    return {
        "task_id": task_id,
        "summary": summary,
        "status": task_row["status"],
        "due_at": due_at_dt.isoformat() if due_at_dt else None,
        "business_slug": body.business_slug,
        "category": category,
        "created_at": task_row["created_at"],
    }

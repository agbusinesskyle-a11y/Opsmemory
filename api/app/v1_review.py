"""OpsMemory v1 review queue API.

Endpoints (Chunk 4):

  GET   /v1/review                  list pending+needs_changes review items
  GET   /v1/review/{id}             read one review item with full detail
  POST  /v1/review/{id}/approve     transactionally apply the proposed_patch
  POST  /v1/review/{id}/reject      mark as rejected with optional reason
  PATCH /v1/review/{id}             edit proposed_patch (typed body),
                                    re-validate, re-snapshot base versions,
                                    return updated review item

Authz: admin-only first pass per Codex chunk-3-close plan. Owner
reviewers (where they have business membership in the candidate's
businesses) widen access in a later commit.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .auth import Principal, require_principal
from .authz import require_admin
from .reconciliation.validate import validate_decision
from .review_apply import (
    ApplyConflict,
    ApplyNotImplemented,
    ApplyValidationError,
    apply_review_item,
)

log = logging.getLogger("opsmemory.v1_review")

router = APIRouter(prefix="/v1/review")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RejectBody(BaseModel):
    reason: str | None = Field(default=None, max_length=2048)


class _CreatePatch(BaseModel):
    summary: str = Field(..., min_length=1, max_length=4096)
    due_at: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    dependency_text: str | None = Field(default=None, max_length=2048)
    businesses: list[str] = Field(..., min_length=1, max_length=8)


class _UpdatePatch(BaseModel):
    summary: str | None = Field(default=None, min_length=1, max_length=4096)
    due_at: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    dependency_text: str | None = Field(default=None, max_length=2048)


class _CompletePatch(BaseModel):
    completion_note: str | None = Field(default=None, max_length=4096)


class PatchBody(BaseModel):
    """Typed patch envelope. Exactly one of create/update/complete must
    be set, and it must match the review_item's existing proposed_action.
    Action change is not supported in this endpoint.
    """
    create: _CreatePatch | None = None
    update: _UpdatePatch | None = None
    complete: _CompletePatch | None = None
    edit_reason: str | None = Field(default=None, max_length=2048)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LIST_STATUSES_DEFAULT = ("pending", "needs_changes")


def _serialize_review_row(row: Any) -> dict:
    """Map an asyncpg row to the API JSON shape."""
    return {
        "id": row["id"],
        "ingest_event_id": row["ingest_event_id"],
        "proposed_action": row["proposed_action"],
        "target_task_id": row["target_task_id"],
        "confidence": float(row["confidence"]) if row["confidence"] is not None else 0.0,
        "reason": row.get("reason"),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row.get("updated_at"),
        "applied_task_id": row.get("applied_task_id"),
        "apply_mutation_id": row.get("apply_mutation_id"),
        "last_apply_error": row.get("last_apply_error"),
        "snoozed_until": row.get("snoozed_until"),
        "snooze_reason": row.get("snooze_reason"),
    }


class SnoozeBody(BaseModel):
    """Phase UI-2B1 snooze payload.

    snoozed_until — required ISO-8601 timestamp. Must be > now() at
                    apply time (server re-checks). Past timestamps
                    return 400 rather than silently un-snoozing —
                    callers asking "snooze for 0 minutes" are most
                    likely buggy.
    reason        — optional free text (max 2048).

    No new lifecycle state. The row stays status='pending' and
    re-enters Inbox the moment snoozed_until <= now(). No scheduler
    needs to wake it up.
    """
    snoozed_until: str = Field(..., max_length=64)
    reason: str | None = Field(default=None, max_length=2048)


# ---------------------------------------------------------------------------
# GET /v1/review — list
# ---------------------------------------------------------------------------

@router.get("")
async def list_review_items(
    request: Request,
    principal: Principal = Depends(require_principal),
    status_filter: str = Query(
        default="pending,needs_changes",
        alias="status",
        description="Comma-separated list of review_lifecycle_state values",
    ),
    snoozed: str = Query(
        default="exclude",
        description="Snooze filter: 'exclude' (default — hide currently-snoozed), "
                    "'only' (only currently-snoozed), 'all' (no filter)",
    ),
    include_auto: bool = Query(
        default=False,
        description="Include auto-approved review_items (manual /v1/tasks "
                    "submissions). Default false hides them from operator-"
                    "facing Triage so the Completed sub-tab doesn't fill "
                    "with the operator's own typed entries.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Admin-only. Default returns pending + needs_changes, excluding
    items whose snoozed_until is in the future (they re-surface in
    the Snoozed sub-tab via ?snoozed=only).
    """
    require_admin(principal)

    requested = [s.strip() for s in status_filter.split(",") if s.strip()]
    valid = {"pending", "approved", "rejected", "needs_changes", "superseded"}
    bad = [s for s in requested if s not in valid]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid status(es): {bad}",
        )
    if not requested:
        requested = list(_LIST_STATUSES_DEFAULT)
    if snoozed not in ("exclude", "only", "all"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid snoozed filter: {snoozed!r}",
        )

    # Phase UI-2B3-1: exclude auto-approved review_items by default.
    # These rows are stamped by /v1/tasks for audit chain consistency
    # (every task has a review_item_id provenance pointer) and would
    # otherwise pollute the Completed sub-tab seconds after the
    # operator submits a Quick Add. Pass include_auto=true to surface
    # them in audit views (admin debug, weekly digest reports).
    if include_auto:
        auto_where = ""
    else:
        auto_where = "AND ri.was_auto_approved = false"

    # Build the snooze WHERE clause. Snooze is a visibility filter
    # for the actionable queue only (pending / needs_changes). Items
    # in terminal states (approved / rejected / superseded) are
    # always returned regardless of any leftover snoozed_until — a
    # row can be approved before its snooze expires, and we don't
    # want it to disappear from GET ?status=approved.
    #
    # Codex UI-2B1 review note: the previous snippet applied to all
    # statuses, so a snoozed-then-approved row was hidden from the
    # Completed sub-tab unless callers explicitly passed snoozed=all.
    #
    # The partial index review_items_snoozed_until_idx covers the
    # IS NOT NULL branch.
    _ACTIONABLE = "ri.status IN ('pending', 'needs_changes')"
    snooze_where = {
        "exclude": (
            f"AND (NOT ({_ACTIONABLE}) "
            f"     OR ri.snoozed_until IS NULL "
            f"     OR ri.snoozed_until <= now())"
        ),
        "only":    (
            f"AND {_ACTIONABLE} "
            f"AND ri.snoozed_until IS NOT NULL "
            f"AND ri.snoozed_until > now()"
        ),
        "all":     "",
    }[snoozed]

    pool = request.app.state.db
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
              ri.id::text                  AS id,
              ri.ingest_event_id::text     AS ingest_event_id,
              ri.proposed_action           AS proposed_action,
              ri.target_task_id::text      AS target_task_id,
              ri.confidence                AS confidence,
              ri.reason                    AS reason,
              ri.status::text              AS status,
              ri.created_at::text          AS created_at,
              ri.updated_at::text          AS updated_at,
              ri.applied_task_id::text     AS applied_task_id,
              ri.apply_mutation_id         AS apply_mutation_id,
              ri.last_apply_error          AS last_apply_error,
              ri.snoozed_until::text       AS snoozed_until,
              ri.snooze_reason             AS snooze_reason
            FROM review_items ri
            WHERE ri.status = ANY($1::review_lifecycle_state[])
              {snooze_where}
              {auto_where}
            ORDER BY ri.created_at DESC
            LIMIT $2 OFFSET $3
            """,
            requested,
            limit,
            offset,
        )
        count_row = await conn.fetchrow(
            f"SELECT count(*) AS c FROM review_items ri "
            f"WHERE ri.status = ANY($1::review_lifecycle_state[]) "
            f"  {snooze_where} "
            f"  {auto_where}",
            requested,
        )

    return {
        "items": [_serialize_review_row(r) for r in rows],
        "limit": limit,
        "offset": offset,
        "total": int(count_row["c"]),
        "status_filter": requested,
    }


# ---------------------------------------------------------------------------
# GET /v1/review/{id} — detail
# ---------------------------------------------------------------------------

@router.get("/{review_item_id}")
async def get_review_item(
    review_item_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Admin-only. Returns the review item with full proposal context."""
    require_admin(principal)

    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              ri.id::text                  AS id,
              ri.ingest_event_id::text     AS ingest_event_id,
              ri.proposed_action           AS proposed_action,
              ri.target_task_id::text      AS target_task_id,
              ri.proposed_patch            AS proposed_patch,
              ri.candidate_facts           AS candidate_facts,
              ri.retrieved_candidates      AS retrieved_candidates,
              ri.confidence                AS confidence,
              ri.reason                    AS reason,
              ri.base_task_version         AS base_task_version,
              ri.base_field_versions       AS base_field_versions,
              ri.validation_errors         AS validation_errors,
              ri.status::text              AS status,
              ri.reviewer_id::text         AS reviewer_id,
              ri.reviewed_at::text         AS reviewed_at,
              ri.applied_at::text          AS applied_at,
              ri.applied_task_id::text     AS applied_task_id,
              ri.apply_mutation_id         AS apply_mutation_id,
              ri.last_apply_error          AS last_apply_error,
              ri.rejection_reason          AS rejection_reason,
              ri.snoozed_until::text       AS snoozed_until,
              ri.snooze_reason             AS snooze_reason,
              ri.created_at::text          AS created_at,
              ri.updated_at::text          AS updated_at,
              ie.source                    AS source,
              ie.received_at::text         AS event_received_at,
              ie.actor_type                AS event_actor_type,
              ie.actor_user_id::text       AS event_actor_user_id
            FROM review_items ri
            JOIN ingest_events ie ON ie.id = ri.ingest_event_id
            WHERE ri.id = $1::uuid
            """,
            str(review_item_id),
        )

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="review_item not found",
        )
    return dict(row)


# ---------------------------------------------------------------------------
# POST /v1/review/{id}/approve — apply
# ---------------------------------------------------------------------------

@router.post("/{review_item_id}/approve")
async def approve_review_item(
    review_item_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Admin-only. Apply the proposed_patch in a single transaction.

    Returns 200 on success with the new/updated task id.
    409 on conflict (target version moved, target deleted, business not
        found, terminal state). Sets review_item.status='needs_changes'
        and persists last_apply_error so the reviewer sees what
        happened.
    422 on validation failure (re-validation inside the txn). Same
        side-effect.
    501 for actions not yet implemented (UPDATE_TASK, COMPLETE_TASK).
    """
    require_admin(principal)

    pool = request.app.state.db
    rid = str(review_item_id)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await apply_review_item(conn, rid, reviewer=principal)
        log.info("review_item_approved", extra={
            "review_item_id": rid,
            "reviewer_id": principal.id,
            "applied_task_id": result.get("applied_task_id"),
        })
        return result
    except ApplyValidationError as exc:
        # Persist last_apply_error and demote to needs_changes.
        await _persist_apply_failure(
            pool, rid,
            new_status="needs_changes",
            error_payload={
                "code": "validation_failed",
                "errors": exc.errors,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_failed", "errors": exc.errors},
        )
    except ApplyConflict as exc:
        await _persist_apply_failure(
            pool, rid,
            new_status="needs_changes",
            error_payload={"code": exc.code, "detail": exc.detail},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "detail": exc.detail},
        )
    except ApplyNotImplemented as exc:
        # Don't demote to needs_changes — the proposal is still valid,
        # we just haven't shipped the apply path yet. Leave the item in
        # its current pending state.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"code": "action_not_implemented", "action": exc.action},
        )


async def _persist_apply_failure(pool, review_item_id: str, *,
                                  new_status: str, error_payload: dict) -> None:
    """Run a guarded UPDATE outside the apply txn so the side-effect
    survives the rolled-back txn.

    Race-safety: the apply txn rolled back without acquiring any locks
    that survive, so between rollback and this call a concurrent worker
    or operator could legitimately have rejected, approved, or
    superseded the same review_item. The guard refuses to demote a row
    that has left the actionable queue, preserving terminal states.

    Pool's jsonb codec encodes raw Python dicts; pass error_payload as
    a dict, not a json.dumps() string (chunk-4-step2 Codex blocker).
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """
                    UPDATE review_items
                       SET status = $2::review_lifecycle_state,
                           last_apply_error = $3::jsonb
                     WHERE id = $1::uuid
                       AND status IN ('pending', 'needs_changes')
                       AND applied_at IS NULL
                       AND apply_mutation_id IS NULL
                    """,
                    review_item_id,
                    new_status,
                    error_payload,
                )
                # asyncpg returns "UPDATE N" — N=0 means the guard refused.
                if result.endswith(" 0"):
                    log.info("persist_apply_failure_skipped_terminal",
                             extra={"review_item_id": review_item_id,
                                    "intended_status": new_status})
    except Exception:
        log.exception("persist_apply_failure_failed",
                      extra={"review_item_id": review_item_id})


# ---------------------------------------------------------------------------
# POST /v1/review/{id}/reject
# ---------------------------------------------------------------------------

@router.post("/{review_item_id}/reject")
async def reject_review_item(
    review_item_id: uuid.UUID,
    body: RejectBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Admin-only. Mark the review item as rejected with an optional
    reason. No task graph mutation. Idempotent: re-rejecting a rejected
    item returns the prior reason.
    """
    require_admin(principal)

    rid = str(review_item_id)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id::text AS id, status::text AS status,
                       rejection_reason
                FROM review_items
                WHERE id = $1::uuid
                FOR UPDATE
                """,
                rid,
            )
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="review_item not found",
                )
            current = row["status"]
            # Idempotent re-reject.
            if current == "rejected":
                return {
                    "review_item_id": row["id"],
                    "status": "rejected",
                    "rejection_reason": row["rejection_reason"],
                    "deduped": True,
                }
            # Terminal states block rejection. The state machine only
            # allows pending / needs_changes -> rejected.
            if current not in ("pending", "needs_changes"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "non_actionable_state",
                            "detail": f"cannot reject from status {current!r}"},
                )

            await conn.execute(
                """
                UPDATE review_items
                   SET status = 'rejected',
                       reviewer_id = $2::uuid,
                       reviewed_at = now(),
                       rejection_reason = $3
                 WHERE id = $1::uuid
                """,
                rid,
                principal.id if principal.principal_type == "user" else None,
                body.reason,
            )

    log.info("review_item_rejected", extra={
        "review_item_id": rid,
        "reviewer_id": principal.id,
    })
    return {
        "review_item_id": rid,
        "status": "rejected",
        "rejection_reason": body.reason,
        "deduped": False,
    }


# ---------------------------------------------------------------------------
# POST /v1/review/{id}/snooze    Phase UI-2B1
# ---------------------------------------------------------------------------

@router.post("/{review_item_id}/snooze")
async def snooze_review_item(
    review_item_id: uuid.UUID,
    body: SnoozeBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Admin-only. Hide the review item from Inbox until snoozed_until.

    Snooze does NOT introduce a new lifecycle state — the row stays
    'pending'. The list endpoint's ?snoozed=exclude filter (default)
    drops items where snoozed_until > now() out of Inbox/Stale; the
    Snoozed sub-tab uses ?snoozed=only to fetch them.

    Idempotent: re-snoozing a snoozed item just overwrites the
    deadline + reason. Snoozing a terminal-state item (approved /
    rejected / superseded) returns 409.

    Snooze deadline must be > now() server-side. Client-supplied
    timestamps are validated post-parse, so a clock-skewed client
    can't accidentally un-snooze.
    """
    require_admin(principal)

    # Parse the timestamp early so we can return 400 with a sensible
    # error before we touch the DB.
    from datetime import datetime, timezone
    try:
        # asyncpg accepts an ISO string OR a datetime; we parse to a
        # tz-aware datetime so the comparison in the WHERE clause is
        # unambiguous and the future-check is server-clock-anchored.
        raw = body.snoozed_until.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        deadline = datetime.fromisoformat(raw)
        if deadline.tzinfo is None:
            # No timezone — assume UTC. This matches the rest of the
            # code (review_apply._coerce_due_at also defaults to UTC).
            deadline = deadline.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "bad_snoozed_until", "detail": str(exc)},
        )
    if deadline <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "snooze_in_past",
                    "detail": "snoozed_until must be in the future"},
        )

    rid = str(review_item_id)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id::text AS id, status::text AS status
                FROM review_items
                WHERE id = $1::uuid
                FOR UPDATE
                """,
                rid,
            )
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="review_item not found",
                )
            current = row["status"]
            if current not in ("pending", "needs_changes"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "non_actionable_state",
                            "detail": f"cannot snooze from status {current!r}"},
                )

            await conn.execute(
                """
                UPDATE review_items
                   SET snoozed_until = $2,
                       snooze_reason = $3
                 WHERE id = $1::uuid
                """,
                rid,
                deadline,
                body.reason,
            )

    log.info("review_item_snoozed", extra={
        "review_item_id": rid,
        "reviewer_id": principal.id,
        "snoozed_until": deadline.isoformat(),
    })
    return {
        "review_item_id": rid,
        "status": current,
        "snoozed_until": deadline.isoformat(),
        "snooze_reason": body.reason,
    }


# POST /v1/review/{id}/unsnooze — clear the snooze.
@router.post("/{review_item_id}/unsnooze")
async def unsnooze_review_item(
    review_item_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Admin-only. Clear snooze fields. Idempotent (no-op on un-snoozed)."""
    require_admin(principal)
    rid = str(review_item_id)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE review_items
               SET snoozed_until = NULL,
                   snooze_reason = NULL
             WHERE id = $1::uuid
            """,
            rid,
        )
    log.info("review_item_unsnoozed", extra={
        "review_item_id": rid,
        "reviewer_id": principal.id,
    })
    return {"review_item_id": rid, "snoozed_until": None}


# ---------------------------------------------------------------------------
# PATCH /v1/review/{id} — typed edit
# ---------------------------------------------------------------------------

@router.patch("/{review_item_id}")
async def patch_review_item(
    review_item_id: uuid.UUID,
    body: PatchBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Admin-only. Edit the proposed_patch with a typed body.

    The body must contain exactly one of create / update / complete,
    and that key must match the review_item's existing proposed_action.
    Changing the action is not supported here — different target rules,
    different base-version capture, different validation.

    Side-effects:
      - Replaces proposed_patch with the rebuilt JSON envelope.
      - For UPDATE_TASK and COMPLETE_TASK: re-snapshots
        base_task_version + base_field_versions against the current
        target task (so subsequent approval is checked against the
        edited-time-zero state, not the original review-time state).
      - Re-runs validate_decision() and stores the result in
        validation_errors.
      - Resets the apply-side state: status='pending', clears
        last_apply_error / reviewer_id / reviewed_at / applied_at /
        applied_task_id / apply_mutation_id / rejection_reason.
      - Stamps edited_at, edited_by, edit_reason.
    """
    require_admin(principal)

    # Body shape: exactly one of create/update/complete.
    populated = [k for k in ("create", "update", "complete")
                 if getattr(body, k) is not None]
    if len(populated) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "patch_envelope_invalid",
                    "detail": "exactly one of create/update/complete required",
                    "got": populated},
        )
    body_action_key = populated[0]
    expected_action = {
        "create": "CREATE_TASK",
        "update": "UPDATE_TASK",
        "complete": "COMPLETE_TASK",
    }[body_action_key]

    rid = str(review_item_id)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id::text                AS id,
                       ingest_event_id::text   AS ingest_event_id,
                       proposed_action         AS proposed_action,
                       target_task_id::text    AS target_task_id,
                       candidate_facts         AS candidate_facts,
                       confidence              AS confidence,
                       status::text            AS status
                FROM review_items
                WHERE id = $1::uuid
                FOR UPDATE
                """,
                rid,
            )
            if not row:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="review_item not found",
                )
            if row["status"] not in ("pending", "needs_changes"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "non_actionable_state",
                            "detail": f"cannot edit from status {row['status']!r}"},
                )
            if row["proposed_action"] != expected_action:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "patch_action_mismatch",
                            "detail": (f"review_item proposed_action is "
                                       f"{row['proposed_action']!r}; PATCH body "
                                       f"key {body_action_key!r} maps to "
                                       f"{expected_action!r}")},
                )

            # Build the new proposed_patch envelope from the typed body.
            #
            # CREATE: include all keys (even when null) — fresh row, the
            #   nulls are intentional initial values.
            # UPDATE: ONLY include keys the client explicitly set. With
            #   exclude_unset=True a PATCH like {"update": {"summary":
            #   "x"}} writes summary only; due_at/category/dependency_text
            #   are not in patch_value, so the apply loop won't touch
            #   them. Without this, every omitted field would be set to
            #   NULL on approve (Codex chunk-4-close blocker).
            # COMPLETE: only completion_note exists, semantics same as
            #   CREATE.
            body_obj = getattr(body, body_action_key)
            if body_action_key == "update":
                patch_value = body_obj.model_dump(exclude_unset=True)
                if not patch_value:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={"code": "update_empty",
                                "detail": "update PATCH must touch at least one field"},
                    )
                # NOT NULL in DB — explicit summary=null is invalid for UPDATE.
                if "summary" in patch_value and patch_value["summary"] is None:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={"code": "summary_null_invalid",
                                "detail": "summary cannot be null on UPDATE"},
                    )
            else:
                patch_value = body_obj.model_dump(exclude_none=False)
            new_proposed_patch = {body_action_key: patch_value}

            # Build a synthetic candidate for re-validation. CREATE pulls
            # summary + businesses straight from the edit. UPDATE/COMPLETE
            # keep the existing candidate_facts.businesses (the patch
            # doesn't change scope) but reflect any edited summary.
            candidate_facts = row["candidate_facts"]
            if isinstance(candidate_facts, str):
                # Defensive — codec should have decoded already.
                import json as _json
                candidate_facts = _json.loads(candidate_facts)
            if not isinstance(candidate_facts, dict):
                candidate_facts = {}

            if body_action_key == "create":
                synth_candidate = dict(candidate_facts)
                synth_candidate["summary"] = patch_value["summary"]
                synth_candidate["businesses"] = patch_value["businesses"]
                synth_candidate["due_at"] = patch_value.get("due_at")
                synth_candidate["category"] = patch_value.get("category")
                synth_candidate["dependency_text"] = patch_value.get("dependency_text")
            else:
                synth_candidate = dict(candidate_facts)
                # UPDATE may edit summary/due_at/category/dependency_text;
                # mirror every set key into candidate_facts so the audit
                # trail and any downstream validator that re-reads
                # candidate_facts sees the post-edit shape.
                if body_action_key == "update":
                    for fname in patch_value.keys():
                        synth_candidate[fname] = patch_value[fname]

            # Re-snapshot base versions for UPDATE/COMPLETE so the next
            # approve compares against the post-edit state, not the
            # original review-time state.
            new_base_task_version: int | None = None
            new_base_field_versions: dict = {}
            target_id = row["target_task_id"]
            if expected_action in ("UPDATE_TASK", "COMPLETE_TASK") and target_id:
                t = await conn.fetchrow(
                    "SELECT version FROM tasks WHERE id = $1::uuid",
                    target_id,
                )
                if t:
                    new_base_task_version = t["version"]
                fv = await conn.fetch(
                    "SELECT field_name, version FROM task_field_versions "
                    "WHERE task_id = $1::uuid",
                    target_id,
                )
                new_base_field_versions = {
                    r["field_name"]: r["version"] for r in fv
                }

            # Reviewer authz scope (admin -> None for pure unscoped).
            actor_business_slugs = (
                None if principal.role == "admin"
                else [b["slug"] for b in principal.businesses]
            )

            # Re-run validation with the new patch.
            decision = {
                "action": expected_action,
                "target_task_id": target_id,
                "confidence": float(row["confidence"] or 0),
                "reason": "post-PATCH revalidation",
            }
            validation_errors = await validate_decision(
                conn, synth_candidate, decision,
                actor_business_slugs=actor_business_slugs,
            )

            # Apply the edit + reset apply-side state. Pool's jsonb codec
            # encodes raw Python dicts/lists.
            reviewer_id = principal.id if principal.principal_type == "user" else None
            await conn.execute(
                """
                UPDATE review_items
                   SET proposed_patch        = $2::jsonb,
                       candidate_facts       = $3::jsonb,
                       base_task_version     = $4,
                       base_field_versions   = $5::jsonb,
                       validation_errors     = $6::jsonb,
                       status                = 'pending',
                       last_apply_error      = '{}'::jsonb,
                       reviewer_id           = NULL,
                       reviewed_at           = NULL,
                       applied_at            = NULL,
                       applied_task_id       = NULL,
                       apply_mutation_id     = NULL,
                       rejection_reason      = NULL,
                       edited_at             = now(),
                       edited_by             = $7::uuid,
                       edit_reason           = $8
                 WHERE id = $1::uuid
                """,
                rid,
                new_proposed_patch,
                synth_candidate,
                new_base_task_version,
                new_base_field_versions,
                validation_errors,
                reviewer_id,
                body.edit_reason,
            )

    log.info("review_item_patched", extra={
        "review_item_id": rid,
        "edited_by": principal.id,
        "action": expected_action,
        "validation_errors": len(validation_errors),
    })
    return {
        "review_item_id": rid,
        "status": "pending",
        "proposed_action": expected_action,
        "proposed_patch": new_proposed_patch,
        "validation_errors": validation_errors,
        "base_task_version": new_base_task_version,
        "base_field_versions": new_base_field_versions,
        "edited_at_now": True,
    }

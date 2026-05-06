"""OpsMemory v1 review queue API.

Endpoints (Chunk 4 step 1 — admin-only, CREATE_TASK end-to-end):

  GET   /v1/review                  list pending+needs_changes review items
  GET   /v1/review/{id}             read one review item with full detail
  POST  /v1/review/{id}/approve     transactionally apply the proposed_patch
  POST  /v1/review/{id}/reject      mark as rejected with optional reason

UPDATE_TASK and COMPLETE_TASK approve flows + PATCH (edit proposed_patch)
land in the next commit. PWA review tab lands in the commit after that.

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
    }


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
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Admin-only. Default returns pending + needs_changes."""
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

    pool = request.app.state.db
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
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
              ri.last_apply_error          AS last_apply_error
            FROM review_items ri
            WHERE ri.status = ANY($1::review_lifecycle_state[])
            ORDER BY ri.created_at DESC
            LIMIT $2 OFFSET $3
            """,
            requested,
            limit,
            offset,
        )
        count_row = await conn.fetchrow(
            "SELECT count(*) AS c FROM review_items "
            "WHERE status = ANY($1::review_lifecycle_state[])",
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
    """
    import json as _json
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
                    _json.dumps(error_payload),
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

"""OpsMemory v1 client mutation API.

Endpoints (Chunk 6 step 1):

  POST /v1/tasks/{id}/toggle_done    Idempotent open<->done toggle.

The contract proof for the PWA's eventual offline outbox: every
mutation here is keyed by a client-supplied idempotency_key plus the
optimistic-lock fields (base_task_version, base_field_versions) the
client read at edit time. A retried POST of the same key returns the
same response (200 success body, or 409/422 with cached error). A
stale base version returns 409 with the current task state so the
client can re-prompt the user.

Authz: any authenticated principal whose visible_business_ids
intersect the task's businesses. Service principals require
`tasks:read:all` (or specific business membership in the future) —
default-deny via authz.visible_business_ids.

Future endpoints (Chunk 6 step 2+): apply / reopen / patch summary.
This module gets the contract right for one mutation; subsequent
endpoints reuse the same idempotency + optimistic-lock pattern.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .auth import Principal, require_principal
from .authz import visible_business_ids

log = logging.getLogger("opsmemory.v1_mutations")

router = APIRouter(prefix="/v1/tasks")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ToggleDoneBody(BaseModel):
    model_config = {"extra": "forbid"}

    idempotency_key: str = Field(..., min_length=1, max_length=128,
                                  description="Client-supplied UUID. Replays "
                                              "with the same key return the "
                                              "cached response.")
    base_task_version: int = Field(..., ge=1,
                                     description="tasks.version the client "
                                                 "read when the user clicked.")
    base_field_versions: dict[str, int] = Field(
        default_factory=dict,
        description="Per-field versions the client read. The mutation "
                    "checks status / completed_at / completed_by / "
                    "completion_note (going to done) or status / "
                    "completed_at / completed_by / completion_note / "
                    "reopened_at / reopened_by (going to open). Missing "
                    "entries are treated as 'no version' and skip the "
                    "compare for that field — first-time mutation case.",
    )
    completion_note: str | None = Field(
        default=None, max_length=4096,
        description="Free-form note shown when toggling open->done. "
                    "Ignored on done->open.",
    )


# Field set bumped on each direction. Per Codex chunk-4-step1 plan
# COMPLETE_TASK uses whole-task version + these touched fields.
_COMPLETE_FIELDS = ("status", "completed_at", "completed_by", "completion_note")
_REOPEN_FIELDS = ("status", "completed_at", "completed_by",
                   "completion_note", "reopened_at", "reopened_by")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _replay_existing(
    conn,
    idempotency_key: str,
) -> tuple[int, dict] | None:
    """Return (status_code, body) for a prior mutation, or None if new.

    Lifecycle states:
      'applied'  -> 200 + result_payload
      'conflict' -> 409 + error_payload
      'rejected' -> response_status + error_payload
      'received' -> in-flight; we don't replay (treat as new and let
                    the FOR UPDATE serialize). NOTE: if the prior
                    process crashed between INSERT(received) and
                    final UPDATE, the row stays 'received' forever
                    until vacuumed. Treating it as new + relying on
                    the unique pk to collide is the safe path.
    """
    row = await conn.fetchrow(
        """
        SELECT status, response_status, result_payload, error_payload
        FROM client_mutations
        WHERE idempotency_key = $1
        """,
        idempotency_key,
    )
    if not row:
        return None
    if row["status"] == "applied":
        return (row["response_status"] or 200, row["result_payload"])
    if row["status"] == "conflict":
        return (row["response_status"] or 409, row["error_payload"])
    if row["status"] == "rejected":
        return (row["response_status"] or 422, row["error_payload"])
    return None  # 'received' — treat as new, FOR UPDATE will serialize


def _principal_actor(principal: Principal) -> tuple[str, str | None, str | None]:
    """Return (actor_kind, actor_user_id, actor_service_account_id).

    Mirrors the task_history / task_state_transitions actor CHECK shape.
    """
    if principal.principal_type == "user":
        return ("user", principal.id, None)
    if principal.principal_type == "service":
        return ("service", None, principal.id)
    return ("system", None, None)


# ---------------------------------------------------------------------------
# POST /v1/tasks/{id}/toggle_done
# ---------------------------------------------------------------------------

@router.post("/{task_id}/toggle_done")
async def toggle_done(
    task_id: uuid.UUID,
    body: ToggleDoneBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Idempotent open<->done toggle for a task.

    Optimistic-lock contract:
      - 200 + new task state on success.
      - 409 + current task state when base_task_version doesn't match
        OR any touched field's base_field_versions entry doesn't match.
      - 422 with errors when the mutation is invalid (e.g. task is
        soft-deleted, principal lacks business membership).
      - 200 with deduped=true when the same idempotency_key was already
        applied (replay returns the cached body).

    Concurrency: SELECT tasks ... FOR UPDATE serializes concurrent
    toggles; the second sees the new version and 409s. Same lock
    order as review_apply (review_items first when applicable, then
    target task) — there is no review row here so we lock the task
    directly.
    """
    rid = str(task_id)
    pool = request.app.state.db
    actor_kind, actor_user_id, actor_service_id = _principal_actor(principal)

    async with pool.acquire() as conn:
        # ----- Idempotency replay (read-only fast path) -----
        prior = await _replay_existing(conn, body.idempotency_key)
        if prior is not None:
            status_code, payload = prior
            if status_code == 200:
                return {**payload, "deduped": True}
            raise HTTPException(status_code=status_code, detail=payload)

        async with conn.transaction():
            # ----- Lock target task -----
            task = await conn.fetchrow(
                """
                SELECT id::text                    AS id,
                       status::text                AS status,
                       deletion_state::text        AS deletion_state,
                       completion_note,
                       completed_at::text          AS completed_at,
                       completed_by::text          AS completed_by,
                       reopened_at::text           AS reopened_at,
                       reopened_by::text           AS reopened_by,
                       version
                FROM tasks WHERE id = $1::uuid FOR UPDATE
                """,
                rid,
            )
            if not task or task["deletion_state"] != "active":
                # Reveal nothing about deleted tasks — same response as
                # not-found (matches the read API's posture).
                err = {"code": "task_not_found", "task_id": rid}
                await _record_mutation(
                    conn, body=body, principal=principal, task_id=rid,
                    status_value="rejected", response_status=404,
                    error_payload=err,
                )
                raise HTTPException(status_code=404, detail=err)

            # ----- Authz: business intersect -----
            visible = visible_business_ids(principal)
            if visible is not None:
                biz_rows = await conn.fetch(
                    "SELECT business_id::text AS id FROM task_businesses "
                    "WHERE task_id = $1::uuid",
                    rid,
                )
                task_bizs = {r["id"] for r in biz_rows}
                if not task_bizs.intersection(visible):
                    err = {"code": "task_not_visible", "task_id": rid}
                    await _record_mutation(
                        conn, body=body, principal=principal, task_id=rid,
                        status_value="rejected", response_status=404,
                        error_payload=err,
                    )
                    # 404 not 403 — don't leak existence.
                    raise HTTPException(status_code=404, detail=err)

            # ----- Optimistic-lock check -----
            if task["version"] != body.base_task_version:
                err = {
                    "code": "task_version_moved",
                    "task_id": rid,
                    "base_task_version": body.base_task_version,
                    "current_task_version": task["version"],
                }
                await _record_mutation(
                    conn, body=body, principal=principal, task_id=rid,
                    status_value="conflict", response_status=409,
                    error_payload=err,
                )
                raise HTTPException(status_code=409, detail=err)

            # Per-field check: only fields that actually exist in the
            # client's base_field_versions are compared (so a client
            # reading task vN can pass {"status": Vs, "completed_at":
            # Vc} and skip the rest).
            if body.base_field_versions:
                fv_rows = await conn.fetch(
                    "SELECT field_name, version FROM task_field_versions "
                    "WHERE task_id = $1::uuid AND field_name = ANY($2::text[])",
                    rid,
                    list(body.base_field_versions.keys()),
                )
                current_fv = {r["field_name"]: r["version"] for r in fv_rows}
                for fname, base_v in body.base_field_versions.items():
                    cur_v = current_fv.get(fname)
                    if cur_v is None and base_v == 0:
                        continue  # row never existed; client correctly
                                  # passed 0 to mean "no baseline".
                    if cur_v != base_v:
                        err = {
                            "code": "field_version_moved",
                            "task_id": rid,
                            "field_name": fname,
                            "base_version": base_v,
                            "current_version": cur_v,
                        }
                        await _record_mutation(
                            conn, body=body, principal=principal, task_id=rid,
                            status_value="conflict", response_status=409,
                            error_payload=err,
                        )
                        raise HTTPException(status_code=409, detail=err)

            # ----- Toggle + bump versions -----
            going_to_done = task["status"] != "done"

            if going_to_done:
                updated = await conn.fetchrow(
                    """
                    UPDATE tasks
                       SET status           = 'done',
                           completed_at     = now(),
                           completed_by     = $2::uuid,
                           completion_note  = $3,
                           version          = version + 1,
                           last_activity_at = now(),
                           updated_at       = now()
                     WHERE id = $1::uuid
                    RETURNING status::text AS status,
                              completed_at::text AS completed_at,
                              completed_by::text AS completed_by,
                              completion_note,
                              version
                    """,
                    rid, actor_user_id, body.completion_note,
                )
                from_state, to_state = "open", "done"
                touched_fields = _COMPLETE_FIELDS
                history_rows = (
                    ("status", task["status"], "done"),
                    ("completed_at", task["completed_at"], updated["completed_at"]),
                    ("completed_by", task["completed_by"], actor_user_id),
                    ("completion_note", task["completion_note"], body.completion_note),
                )
            else:
                updated = await conn.fetchrow(
                    """
                    UPDATE tasks
                       SET status           = 'open',
                           completed_at     = NULL,
                           completed_by     = NULL,
                           completion_note  = NULL,
                           reopened_at      = now(),
                           reopened_by      = $2::uuid,
                           version          = version + 1,
                           last_activity_at = now(),
                           updated_at       = now()
                     WHERE id = $1::uuid
                    RETURNING status::text AS status,
                              reopened_at::text AS reopened_at,
                              reopened_by::text AS reopened_by,
                              version
                    """,
                    rid, actor_user_id,
                )
                from_state, to_state = "done", "open"
                touched_fields = _REOPEN_FIELDS
                history_rows = (
                    ("status", task["status"], "open"),
                    ("completed_at", task["completed_at"], None),
                    ("completed_by", task["completed_by"], None),
                    ("completion_note", task["completion_note"], None),
                    ("reopened_at", task["reopened_at"], updated["reopened_at"]),
                    ("reopened_by", task["reopened_by"], actor_user_id),
                )

            # ----- Bump field versions -----
            for fname in touched_fields:
                await conn.execute(
                    """
                    INSERT INTO task_field_versions
                      (task_id, field_name, version, updated_by)
                    VALUES ($1::uuid, $2, 1, $3::uuid)
                    ON CONFLICT (task_id, field_name) DO UPDATE
                      SET version    = task_field_versions.version + 1,
                          updated_at = now(),
                          updated_by = EXCLUDED.updated_by
                    """,
                    rid, fname, actor_user_id,
                )

            # ----- task_history per field -----
            change_type = "complete" if going_to_done else "reopen"
            for fname, old_val, new_val in history_rows:
                await conn.execute(
                    """
                    INSERT INTO task_history
                      (task_id, mutation_id, field_name, change_type,
                       old_value, new_value,
                       actor_user_id, actor_service_account_id, actor_type,
                       reason)
                    VALUES
                      ($1::uuid, $2, $3, $4,
                       $5::jsonb, $6::jsonb,
                       $7::uuid, $8::uuid, $9,
                       $10)
                    """,
                    rid,
                    f"client:{body.idempotency_key}:{fname}",
                    fname,
                    change_type,
                    old_val,
                    new_val,
                    actor_user_id,
                    actor_service_id,
                    actor_kind,
                    f"toggle_done by client mutation {body.idempotency_key}",
                )

            # ----- task_state_transitions: one row per business -----
            biz_rows = await conn.fetch(
                "SELECT business_id::text AS business_id "
                "FROM task_businesses WHERE task_id = $1::uuid",
                rid,
            )
            for biz in biz_rows:
                await conn.execute(
                    """
                    INSERT INTO task_state_transitions
                      (task_id, business_id, from_state, to_state,
                       actor_kind, actor_user_id, actor_service_account_id,
                       reason, metadata)
                    VALUES
                      ($1::uuid, $2::uuid, $3, $4,
                       $5, $6::uuid, $7::uuid,
                       $8, $9::jsonb)
                    """,
                    rid, biz["business_id"], from_state, to_state,
                    actor_kind, actor_user_id, actor_service_id,
                    f"toggle_done by client mutation {body.idempotency_key}",
                    {"idempotency_key": body.idempotency_key},
                )

            # ----- Result payload -----
            result_payload: dict[str, Any] = {
                "task_id": rid,
                "status": updated["status"],
                "version": updated["version"],
                "idempotency_key": body.idempotency_key,
            }
            if going_to_done:
                result_payload["completed_at"] = updated["completed_at"]
                result_payload["completed_by"] = updated["completed_by"]
                result_payload["completion_note"] = updated["completion_note"]
            else:
                result_payload["reopened_at"] = updated["reopened_at"]
                result_payload["reopened_by"] = updated["reopened_by"]

            await _record_mutation(
                conn, body=body, principal=principal, task_id=rid,
                status_value="applied", response_status=200,
                result_payload=result_payload,
            )

        log.info("toggle_done_applied", extra={
            "task_id": rid,
            "idempotency_key": body.idempotency_key,
            "going_to_done": going_to_done,
            "actor_user_id": actor_user_id,
        })
        return {**result_payload, "deduped": False}


async def _record_mutation(
    conn,
    *,
    body: ToggleDoneBody,
    principal: Principal,
    task_id: str,
    status_value: str,
    response_status: int,
    result_payload: dict | None = None,
    error_payload: dict | None = None,
) -> None:
    """INSERT (or UPSERT) the client_mutations row.

    On idempotency-key collision (concurrent retry inside the same txn
    window), the unique pk would 23505. We catch and let the caller's
    earlier _replay_existing branch handle a future retry.
    """
    actor_kind = principal.principal_type
    actor_user_id = principal.id if actor_kind == "user" else None
    actor_service_id = principal.id if actor_kind == "service" else None
    payload = {
        "action": "toggle_done",
        "task_id": task_id,
        "completion_note": body.completion_note,
    }
    try:
        await conn.execute(
            """
            INSERT INTO client_mutations
              (idempotency_key, actor_user_id, actor_service_account_id,
               client_id, task_id,
               base_task_version, base_field_versions,
               payload, status, applied_at,
               result_payload, error_payload, response_status)
            VALUES
              ($1, $2::uuid, $3::uuid,
               $4, $5::uuid,
               $6, $7::jsonb,
               $8::jsonb, $9, now(),
               $10::jsonb, $11::jsonb, $12)
            """,
            body.idempotency_key,
            actor_user_id,
            actor_service_id,
            "pwa",  # chunk 6 first commit assumes browser PWA; future
                    # outbox/SW can pass an explicit client_id.
            task_id,
            body.base_task_version,
            body.base_field_versions,
            payload,
            status_value,
            result_payload or {},
            error_payload or {},
            response_status,
        )
    except asyncpg.UniqueViolationError:
        # Another concurrent retry won the race; the earlier
        # _replay_existing branch will return the cached response on
        # the next request. For this in-flight call, swallow the
        # collision — our own state mutation already happened (or was
        # rolled back if we're inside a failing txn).
        log.warning("client_mutation_idempotency_collision", extra={
            "idempotency_key": body.idempotency_key, "task_id": task_id,
        })

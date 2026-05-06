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

Replay correctness contract (per Codex chunk-6-step1 review):

  - Acquire the idempotency_key by INSERTing client_mutations
    (status='received') as the FIRST DB operation in the apply
    transaction. This serializes the key BEFORE any task side
    effects. A concurrent retry with the same key collides on the
    primary key, the txn rolls back, and the request falls into the
    replay branch.

  - Compute the outcome (applied / conflict / rejected) inside the
    transaction. UPDATE the client_mutations row with the final
    status + result_payload / error_payload / response_status BEFORE
    the txn commits, so the cached row IS the response. Failure rows
    persist (the bug fixed in chunk6-step1-close was that
    HTTPException was raised inside the txn, rolling back the
    failure record too).

  - Raise HTTPException OUTSIDE the transaction so deterministic
    failures (404/409/422) commit their record before becoming a
    response.

  - Replay lookup verifies stored task_id and stored action match
    the current request, so a deterministic key reused on a
    different task path can't return the prior task's result.

Authz: any authenticated principal whose visible_business_ids
intersect the task's businesses. Service principals fall through
visible_business_ids' default-deny ([]) unless they hold an
all-business pipeline scope.

Future endpoints (Chunk 6 step 2+) reuse the same idempotency +
optimistic-lock pattern.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

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
                    "completion_note (going to done) or those plus "
                    "reopened_at / reopened_by (going to open). Missing "
                    "entries skip the compare for that field. A value "
                    "of 0 means 'no baseline' (pre-field-version task).",
    )
    completion_note: str | None = Field(
        default=None, max_length=4096,
        description="Free-form note shown when toggling open->done. "
                    "Ignored on done->open.",
    )

    @field_validator("base_field_versions")
    @classmethod
    def _check_base_field_versions(cls, value: dict[str, int]) -> dict[str, int]:
        # Reject negative versions (Codex chunk-6-step1: 0 is a valid
        # 'no baseline' marker; negatives are nonsense). Reject unknown
        # field names so a typo doesn't silently bypass concurrency.
        allowed = set(_COMPLETE_FIELDS) | set(_REOPEN_FIELDS)
        for fname, fver in value.items():
            if not isinstance(fver, int) or fver < 0:
                raise ValueError(f"base_field_versions[{fname!r}] must be int >= 0")
            if fname not in allowed:
                raise ValueError(f"base_field_versions[{fname!r}] is not a recognized "
                                 f"toggle_done field; expected one of {sorted(allowed)}")
        return value


# Field set bumped on each direction. Per Codex chunk-4-step1 plan
# COMPLETE_TASK uses whole-task version + these touched fields.
_COMPLETE_FIELDS = ("status", "completed_at", "completed_by", "completion_note")
_REOPEN_FIELDS = ("status", "completed_at", "completed_by",
                   "completion_note", "reopened_at", "reopened_by")


# ---------------------------------------------------------------------------
# Internal sentinels
# ---------------------------------------------------------------------------

class _IdempotencyCollision(Exception):
    """Raised inside the apply txn when the INSERT(received) hits a
    UniqueViolation on idempotency_key. Caught by the outer handler,
    which rolls the txn back and falls into the replay branch.
    """


class _Outcome:
    __slots__ = ("kind", "response_status", "payload")

    def __init__(self, kind: str, response_status: int, payload: dict):
        self.kind = kind                       # 'applied'|'conflict'|'rejected'
        self.response_status = response_status # 200 / 409 / 404 / etc.
        self.payload = payload                  # body dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _replay_existing(
    conn,
    idempotency_key: str,
    *,
    task_id: str,
    action: str,
) -> tuple[int, dict] | None:
    """Return (status_code, body) for a prior mutation, or None if new.

    Verifies the stored task_id and action match the current request
    so a reused key on a different task / endpoint doesn't replay the
    wrong result. On mismatch returns a synthetic 409 explaining why.

    'received' rows mean another request is in flight; returning None
    lets the caller's INSERT collide with UniqueViolation, which
    triggers an explicit replay-or-503 path.
    """
    row = await conn.fetchrow(
        """
        SELECT status, response_status, result_payload, error_payload,
               task_id::text         AS task_id,
               payload->>'action'    AS action,
               payload->>'task_id'   AS payload_task_id
        FROM client_mutations
        WHERE idempotency_key = $1
        """,
        idempotency_key,
    )
    if not row:
        return None

    # Action must match this endpoint.
    if row["action"] != action:
        return (409, {
            "code": "idempotency_action_mismatch",
            "detail": (f"idempotency_key already used for action "
                       f"{row['action']!r}; this request was for {action!r}"),
        })

    # task_id must match the requested task. The stored row.task_id is
    # NULL when the original request 404'd on a missing/invisible task,
    # so fall back to comparing the originally-requested task_id stored
    # in payload (Codex chunk-6-close: previously this fallback was
    # absent, letting a same-key retry against a different task_id
    # silently replay the prior 404 as if it were for the new task).
    stored = row["task_id"] if row["task_id"] is not None else row["payload_task_id"]
    if stored is not None and stored != task_id:
        return (409, {
            "code": "idempotency_task_mismatch",
            "detail": (f"idempotency_key already used for task "
                       f"{stored!r}; this request was for {task_id!r}"),
        })

    if row["status"] == "applied":
        return (row["response_status"] or 200, row["result_payload"])
    if row["status"] == "conflict":
        return (row["response_status"] or 409, row["error_payload"])
    if row["status"] == "rejected":
        return (row["response_status"] or 422, row["error_payload"])
    # 'received' — apply still in flight; caller handles.
    return None


def _principal_actor(principal: Principal) -> tuple[str, str | None, str | None]:
    """Return (actor_kind, actor_user_id, actor_service_account_id)."""
    if principal.principal_type == "user":
        return ("user", principal.id, None)
    if principal.principal_type == "service":
        return ("service", None, principal.id)
    return ("system", None, None)


def _emit_replay(prior: tuple[int, dict]):
    status_code, body = prior
    if status_code == 200:
        return {**body, "deduped": True}
    raise HTTPException(status_code=status_code,
                        detail={**body, "deduped": True})


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
    """Idempotent open<->done toggle for a task. See module docstring for
    the replay correctness contract.
    """
    rid = str(task_id)
    pool = request.app.state.db
    actor_kind, actor_user_id, actor_service_id = _principal_actor(principal)

    async with pool.acquire() as conn:
        # ---- Fast replay (no lock, no txn) ----
        prior = await _replay_existing(conn, body.idempotency_key,
                                        task_id=rid, action="toggle_done")
        if prior is not None:
            return _emit_replay(prior)

        outcome: _Outcome | None = None
        payload_marker = {
            "action": "toggle_done",
            "task_id": rid,
            "completion_note": body.completion_note,
        }

        try:
            async with conn.transaction():
                # ---- Acquire idempotency key (serialization point) ----
                # task_id is initially NULL; populated to rid only when
                # the apply succeeds. Keeps the FK to tasks(id) valid
                # for missing-task and not-visible cases.
                try:
                    await conn.execute(
                        """
                        INSERT INTO client_mutations
                          (idempotency_key, actor_user_id, actor_service_account_id,
                           client_id, task_id,
                           base_task_version, base_field_versions,
                           payload, status,
                           result_payload, error_payload, response_status)
                        VALUES
                          ($1, $2::uuid, $3::uuid,
                           'pwa', NULL,
                           $4, $5::jsonb,
                           $6::jsonb, 'received',
                           '{}'::jsonb, '{}'::jsonb, NULL)
                        """,
                        body.idempotency_key,
                        actor_user_id,
                        actor_service_id,
                        body.base_task_version,
                        body.base_field_versions,
                        payload_marker,
                    )
                except asyncpg.UniqueViolationError:
                    raise _IdempotencyCollision()

                # ---- Lock target task ----
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
                    outcome = _Outcome("rejected", 404,
                                        {"code": "task_not_found",
                                         "task_id": rid})
                else:
                    # ---- Authz: business intersect (visibility) ----
                    visible = visible_business_ids(principal)
                    if visible is not None:
                        biz_rows = await conn.fetch(
                            "SELECT business_id::text AS id FROM task_businesses "
                            "WHERE task_id = $1::uuid",
                            rid,
                        )
                        task_bizs = {r["id"] for r in biz_rows}
                        if not task_bizs.intersection(visible):
                            outcome = _Outcome("rejected", 404,
                                                {"code": "task_not_visible",
                                                 "task_id": rid})

                # ---- Mutation authz (per locked design 01-design.md §1) ----
                # Admins can toggle any active task. Owners can complete or
                # reopen ONLY tasks they are assigned to (task_assignees row
                # with role='assignee'); reopen by an owner only within the
                # 24h reversal window — after that, admin only. Service
                # principals are rejected here because no toggle-mutation
                # scope is defined yet (per Codex chunk-6-close blocker).
                if outcome is None:
                    if principal.principal_type == "user" and principal.role == "admin":
                        pass
                    elif principal.principal_type == "user" and principal.role == "owner":
                        is_assignee = await conn.fetchrow(
                            "SELECT 1 FROM task_assignees "
                            "WHERE task_id = $1::uuid AND user_id = $2::uuid "
                            "  AND role = 'assignee'",
                            rid, principal.id,
                        )
                        if not is_assignee:
                            outcome = _Outcome("rejected", 403, {
                                "code": "task_not_assigned",
                                "task_id": rid,
                                "detail": ("owner can complete/reopen only "
                                           "tasks they are assigned to"),
                            })
                        elif task["status"] == "done":
                            # Going-to-open path. Check 24h window.
                            window = await conn.fetchrow(
                                "SELECT (completed_at IS NOT NULL "
                                "        AND completed_at >= now() - interval '24 hours') "
                                "       AS within FROM tasks WHERE id = $1::uuid",
                                rid,
                            )
                            if window and not window["within"]:
                                outcome = _Outcome("rejected", 403, {
                                    "code": "reopen_window_expired",
                                    "task_id": rid,
                                    "detail": ("only admins can reopen tasks "
                                               "completed more than 24 hours ago"),
                                })
                    else:
                        outcome = _Outcome("rejected", 403, {
                            "code": "service_mutation_forbidden",
                            "task_id": rid,
                            "detail": ("toggle_done requires a user "
                                       "principal; service principals are "
                                       "not granted this mutation"),
                        })

                if outcome is None:
                    # ---- Optimistic-lock: whole-task version ----
                    if task["version"] != body.base_task_version:
                        outcome = _Outcome("conflict", 409, {
                            "code": "task_version_moved",
                            "task_id": rid,
                            "base_task_version": body.base_task_version,
                            "current_task_version": task["version"],
                        })

                if outcome is None and body.base_field_versions:
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
                            continue  # pre-field-version task
                        if cur_v != base_v:
                            outcome = _Outcome("conflict", 409, {
                                "code": "field_version_moved",
                                "task_id": rid,
                                "field_name": fname,
                                "base_version": base_v,
                                "current_version": cur_v,
                            })
                            break

                if outcome is None:
                    # ---- Apply ----
                    going_to_done = task["status"] != "done"
                    result_payload = await _apply_toggle(
                        conn, rid=rid,
                        going_to_done=going_to_done,
                        task=task,
                        body=body,
                        actor_kind=actor_kind,
                        actor_user_id=actor_user_id,
                        actor_service_id=actor_service_id,
                    )
                    outcome = _Outcome("applied", 200, result_payload)

                # ---- Persist final outcome on the client_mutations row ----
                if outcome.kind == "applied":
                    await conn.execute(
                        """
                        UPDATE client_mutations
                           SET status          = 'applied',
                               task_id         = $2::uuid,
                               result_payload  = $3::jsonb,
                               response_status = 200,
                               applied_at      = now()
                         WHERE idempotency_key = $1
                        """,
                        body.idempotency_key, rid, outcome.payload,
                    )
                else:
                    # task_id stays NULL for missing/invisible tasks; for
                    # version conflicts the task exists, so we can record
                    # it. Use rid when the row "task" was loaded; else NULL.
                    record_task_id = rid if task else None
                    await conn.execute(
                        """
                        UPDATE client_mutations
                           SET status          = $2,
                               task_id         = $3::uuid,
                               error_payload   = $4::jsonb,
                               response_status = $5,
                               applied_at      = now()
                         WHERE idempotency_key = $1
                        """,
                        body.idempotency_key,
                        outcome.kind,
                        record_task_id,
                        outcome.payload,
                        outcome.response_status,
                    )
                # COMMIT here.
        except _IdempotencyCollision:
            # Concurrent retry won the key. Replay.
            prior = await _replay_existing(conn, body.idempotency_key,
                                            task_id=rid, action="toggle_done")
            if prior is None:
                # The other request hasn't committed yet (still in flight,
                # holding the txn open). Best to advise the client to
                # retry rather than spin.
                raise HTTPException(
                    status_code=503,
                    detail={"code": "idempotency_in_flight",
                            "detail": ("Another request with this "
                                        "idempotency_key is processing; "
                                        "retry shortly")},
                )
            return _emit_replay(prior)

    # ---- Outside the txn — emit response based on outcome ----
    assert outcome is not None  # all paths set it
    log.info("toggle_done_processed", extra={
        "task_id": rid,
        "idempotency_key": body.idempotency_key,
        "outcome_kind": outcome.kind,
        "actor_user_id": actor_user_id,
    })
    if outcome.kind == "applied":
        return {**outcome.payload, "deduped": False}
    raise HTTPException(status_code=outcome.response_status,
                        detail={**outcome.payload, "deduped": False})


# ---------------------------------------------------------------------------
# Apply (called only on the success path)
# ---------------------------------------------------------------------------

async def _apply_toggle(
    conn,
    *,
    rid: str,
    going_to_done: bool,
    task: Any,
    body: ToggleDoneBody,
    actor_kind: str,
    actor_user_id: str | None,
    actor_service_id: str | None,
) -> dict:
    """Mutate the task + audit + state transitions. Returns result_payload."""
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

    # Bump field versions
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

    # task_history per field
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

    # task_state_transitions per business
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
    return result_payload

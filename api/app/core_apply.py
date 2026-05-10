"""OpsMemory shared task-mutation primitives.

Phase UI-2B3-1 (Codex Option D): consolidate the create-task write
path so review_apply (pipeline-derived) and v1_tasks (operator
direct) cannot drift.

Both callers must produce the SAME on-disk shape:
  - tasks row with summary/due_at/category/etc
  - task_businesses links
  - task_assignees auto-link if owner_user_id provided
  - task_field_versions baseline rows for the 4 mutable create fields
  - task_history (single 'create' row)
  - task_state_transitions (one per business)
  - sop_generated_tasks backfill (no-op for non-SOP origins)

Centralising this here means future schema additions (history
columns, new field-version dimensions, status enum changes) update
exactly one site, not two.

Boundaries — what core_apply does NOT do:
  - It does NOT manage transactions. Caller owns the txn.
  - It does NOT validate principal authz. Caller checks
    visible_business_ids + business membership BEFORE calling.
  - It does NOT touch ingest_events. Caller has already inserted
    the source event and passes its id.
  - It does NOT touch review_items. Pipeline path: caller manages
    review_item lifecycle. Manual path: caller stamps an auto-
    approved review_item separately.
  - It does NOT enforce idempotency. Caller must check
    client_mutations OR a unique key BEFORE entering core_apply.

Why a separate module rather than a helper inside review_apply:
  Symmetry. Both review_apply._apply_create_task and v1_tasks
  import the same shared primitive. Putting it inside review_apply
  would imply v1_tasks depends on review_apply (the LLM/pipeline
  path), which is the wrong direction — typed entries don't need
  the pipeline.
"""

from __future__ import annotations

from typing import Any


# Fields whose values come from the create payload. These get a row
# in task_field_versions on create so future per-field concurrency
# checks have a baseline to compare against.
_CREATE_FIELD_NAMES = ("summary", "due_at", "category", "dependency_text")


class CoreApplyError(Exception):
    """Raised on internal apply failure that the caller surfaces as 5xx."""


async def create_task_inner(
    conn,
    *,
    ingest_event_id: str,
    summary: str,
    business_ids: list[dict],          # list of {"id": ..., "slug": ...}
    due_at,                             # tz-aware datetime or None
    due_at_for_history: str | None,    # raw string the caller wants stored
                                        # in task_history.new_value (NULL ok).
                                        # Preserves prior behavior for the
                                        # pipeline path where new_value got
                                        # the raw proposed_patch string.
    description: str | None,
    category: str | None,
    priority: str | None,
    dependency_text: str | None,
    owner_user_id: str | None,         # already-validated user id, or None
    actor_user_id: str | None,         # principal.id when actor_type='user'
    actor_service_id: str | None,      # principal.id when actor_type='service'
    actor_type: str,                   # 'user' | 'service' | 'system'
    mutation_id: str,                  # opaque correlation id for task_history
    history_reason: str,                # task_history.reason
    history_confidence: float | None,
    transition_reason: str,             # task_state_transitions.reason
                                        # (separate from history_reason —
                                        # pipeline path uses different strings)
    transition_metadata: dict[str, Any] | None = None,
    history_business_slugs: list[str] | None = None,
                                        # Caller-supplied slug order for
                                        # task_history.new_value.businesses.
                                        # Defaults to business_ids' order.
) -> str:
    """Apply a CREATE_TASK write atomically. Caller owns the txn.

    Returns the new task id.
    """
    if not summary or not summary.strip():
        raise CoreApplyError("summary is empty")
    if not business_ids:
        raise CoreApplyError("business_ids is empty")
    if actor_type not in ("user", "service", "system"):
        raise CoreApplyError(f"invalid actor_type: {actor_type!r}")

    summary_clean = summary.strip()

    # ----- INSERT tasks -----
    task_row = await conn.fetchrow(
        """
        INSERT INTO tasks
          (summary, description, due_at, category, priority,
           dependency_text,
           source_event_id, version, last_activity_at,
           created_at, updated_at)
        VALUES
          ($1, $2, $3::timestamptz, $4, $5,
           $6,
           $7::uuid, 1, now(),
           now(), now())
        RETURNING id::text AS id
        """,
        summary_clean,
        description,
        due_at,
        category,
        priority,
        dependency_text,
        ingest_event_id,
    )
    task_id = task_row["id"]

    # ----- INSERT task_businesses -----
    for biz in business_ids:
        await conn.execute(
            "INSERT INTO task_businesses (task_id, business_id, added_by) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid)",
            task_id,
            biz["id"],
            actor_user_id,
        )

    # ----- INSERT task_assignees (when owner_user_id supplied) -----
    if owner_user_id:
        await conn.execute(
            """
            INSERT INTO task_assignees
              (task_id, user_id, role, assigned_by)
            VALUES
              ($1::uuid, $2::uuid, 'assignee', $3::uuid)
            ON CONFLICT (task_id, user_id) DO NOTHING
            """,
            task_id,
            owner_user_id,
            actor_user_id,
        )

    # ----- INSERT task_field_versions baseline -----
    for field_name in _CREATE_FIELD_NAMES:
        await conn.execute(
            """
            INSERT INTO task_field_versions
              (task_id, field_name, version, updated_by, source_event_id)
            VALUES
              ($1::uuid, $2, 1, $3::uuid, $4::uuid)
            """,
            task_id,
            field_name,
            actor_user_id,
            ingest_event_id,
        )

    # ----- INSERT task_history (single 'create' row) -----
    # Preserve caller-supplied slug ORDER for audit consumers. The
    # pipeline path stored proposed_patch.businesses verbatim; if we
    # rebuild from business_ids we get DB-row order which can differ
    # for multi-business CREATE patches (Codex round-2 audit drift).
    history_slugs = (history_business_slugs
                     if history_business_slugs is not None
                     else [b["slug"] for b in business_ids])
    new_value = {
        "summary": summary_clean,
        "due_at": due_at_for_history,
        "category": category,
        "dependency_text": dependency_text,
        "businesses": history_slugs,
    }
    await conn.execute(
        """
        INSERT INTO task_history
          (task_id, mutation_id, field_name, change_type,
           old_value, new_value,
           actor_user_id, actor_service_account_id, actor_type,
           source_event_id, reason, confidence)
        VALUES
          ($1::uuid, $2, NULL, 'create',
           NULL, $3::jsonb,
           $4::uuid, $5::uuid, $6,
           $7::uuid, $8, $9)
        """,
        task_id,
        mutation_id,
        new_value,
        actor_user_id,
        actor_service_id,
        actor_type,
        ingest_event_id,
        history_reason,
        history_confidence,
    )

    # ----- INSERT task_state_transitions: one per business -----
    # NOTE: do not auto-augment transition_metadata here — caller
    # owns the exact shape so audit consumers see the same JSON
    # before and after the core_apply extraction.
    metadata = dict(transition_metadata or {})
    for biz in business_ids:
        await conn.execute(
            """
            INSERT INTO task_state_transitions
              (task_id, business_id, from_state, to_state,
               actor_kind, actor_user_id, actor_service_account_id,
               reason, metadata)
            VALUES
              ($1::uuid, $2::uuid, NULL, 'open',
               $3, $4::uuid, $5::uuid,
               $6, $7::jsonb)
            """,
            task_id,
            biz["id"],
            actor_type,
            actor_user_id,
            actor_service_id,
            transition_reason,
            metadata,
        )

    return task_id

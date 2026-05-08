"""Step 7 of the reconciliation pipeline: transactionally apply an
approved review_items row to the task graph.

This is the missing 7th step from Chunk 3 (which stopped at
`pending_review`). Lands in Chunk 4 alongside the review UI / approve /
reject endpoints.

Apply transaction shape (per Codex chunk-3-close plan):

  1. SELECT review_items WHERE id = $1 FOR UPDATE
  2. Reject unless status IN ('pending', 'needs_changes')
  3. Re-run reviewer authz against the proposed businesses (admin-only
     bypass for now; chunk 4 step-3 widens to owner reviewers if needed)
  4. Re-run deterministic validation inside the transaction
  5. For UPDATE_TASK / COMPLETE_TASK: lock target task FOR UPDATE,
     require active target, compare base_task_version + the touched
     base_field_versions
  6. Mutate the task graph (CREATE_TASK only in this commit; UPDATE +
     COMPLETE land in the next commit)
  7. Insert task_history with mutation_id = 'review:<review_item_id>'
  8. Insert task_state_transitions
  9. Update review_items: status='approved', reviewer_id, reviewed_at,
     applied_at, applied_task_id, apply_mutation_id, clear last_apply_error
  10. COMMIT — a single atomic unit. On any conflict, raise ApplyConflict
      and let the caller persist last_apply_error + status='needs_changes'.

Two SUCCESSFUL retries of the same review_item are idempotent: the
second SELECT FOR UPDATE waits behind the first, then sees status =
'approved' and returns the prior applied_task_id. No double-apply
possible — this is the chunk1.5 client_mutations idempotency guarantee
extended to the review-approve path.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .auth import Principal
from .reconciliation.validate import validate_decision


def _coerce_due_at(value: Any) -> datetime | None:
    """Coerce a due_at value (string from JSONB or already a datetime)
    into a tz-aware datetime suitable for asyncpg's ::timestamptz cast.

    Why: review_items.proposed_patch is jsonb; datetimes serialize to
    ISO 8601 strings on write, so on approve we read them back as
    strings. asyncpg rejects strings bound to ::timestamptz params
    with "expected datetime ... got str". Codex 2026-05-08 review
    flagged this as the same class of bug fixed in retrieve.py
    yesterday; it was latent until normalize._resolve_due started
    populating non-null due dates from "Friday"-style hints.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None

log = logging.getLogger("opsmemory.review_apply")


class ApplyConflict(Exception):
    """Raised inside the apply transaction when a 409-class condition
    occurs (e.g. target version moved, business authz fails, target
    deleted). The caller catches this, persists last_apply_error +
    status='needs_changes', and returns 409 to the client.
    """

    def __init__(self, code: str, detail: dict[str, Any]):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ApplyValidationError(Exception):
    """Raised when re-validation inside the apply txn finds errors.
    Caller persists last_apply_error + status='needs_changes' and
    returns 422.
    """

    def __init__(self, errors: list[dict]):
        super().__init__("validation failed")
        self.errors = errors


class ApplyNotImplemented(Exception):
    """Raised for actions the current commit doesn't yet support
    (UPDATE_TASK / COMPLETE_TASK in this first commit). Caller returns 501.
    """

    def __init__(self, action: str):
        super().__init__(f"action {action!r} not yet implemented")
        self.action = action


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

async def apply_review_item(
    conn,
    review_item_id: str,
    *,
    reviewer: Principal,
) -> dict:
    """Apply the proposed_patch of one review_item.

    Must be called inside an `async with conn.transaction()` block. Locks
    review_items row + (for UPDATE/COMPLETE) target task FOR UPDATE so
    concurrent approvals serialize.

    Returns:
      { "status": "approved",
        "review_item_id": ...,
        "applied_task_id": ...,
        "apply_mutation_id": "review:<id>" }

    Raises ApplyConflict, ApplyValidationError, ApplyNotImplemented as
    described in the docstring.
    """
    apply_mutation_id = f"review:{review_item_id}"

    # ----- 1. Lock + read -----
    row = await conn.fetchrow(
        """
        SELECT
          ri.id::text                 AS id,
          ri.ingest_event_id::text    AS ingest_event_id,
          ri.proposed_action          AS proposed_action,
          ri.target_task_id::text     AS target_task_id,
          ri.proposed_patch           AS proposed_patch,
          ri.candidate_facts          AS candidate_facts,
          ri.confidence               AS confidence,
          ri.base_task_version        AS base_task_version,
          ri.base_field_versions      AS base_field_versions,
          ri.status::text             AS status,
          ri.applied_task_id::text    AS applied_task_id,
          ri.apply_mutation_id        AS apply_mutation_id,
          ie.actor_user_id::text      AS event_actor_user_id,
          ie.actor_service_account_id::text AS event_actor_service_id
        FROM review_items ri
        JOIN ingest_events ie ON ie.id = ri.ingest_event_id
        WHERE ri.id = $1::uuid
        FOR UPDATE OF ri
        """,
        review_item_id,
    )
    if not row:
        raise ApplyConflict("review_item_not_found",
                            {"review_item_id": review_item_id})

    # Idempotent retry: already approved, return the prior result.
    if row["status"] == "approved":
        return {
            "status": "approved",
            "review_item_id": row["id"],
            "applied_task_id": row["applied_task_id"],
            "apply_mutation_id": row["apply_mutation_id"] or apply_mutation_id,
            "deduped": True,
        }

    # Already rejected / superseded — refuse to re-approve.
    if row["status"] not in ("pending", "needs_changes"):
        raise ApplyConflict("review_item_terminal_state",
                            {"current_status": row["status"]})

    # ----- 2. Re-run validation inside the txn -----
    proposed_patch = row["proposed_patch"]
    if isinstance(proposed_patch, str):
        proposed_patch = json.loads(proposed_patch)
    candidate_facts = row["candidate_facts"]
    if isinstance(candidate_facts, str):
        candidate_facts = json.loads(candidate_facts)

    decision = {
        "action": row["proposed_action"],
        "target_task_id": row["target_task_id"],
        "confidence": float(row["confidence"] or 0),
        "reason": "applied at review approve time",
    }

    # Reviewer authz: admin bypass; non-admin reviewers must have
    # business membership in every business the candidate touches. The
    # admin-only first pass uses require_admin upstream so this is
    # currently a tautology; left in place for the next commit when
    # owner reviewers are admitted.
    if reviewer.principal_type == "user" and reviewer.role == "admin":
        actor_business_slugs = None
    else:
        actor_business_slugs = [b["slug"] for b in reviewer.businesses]

    errors = await validate_decision(
        conn, candidate_facts, decision,
        actor_business_slugs=actor_business_slugs,
    )
    if errors:
        raise ApplyValidationError(errors)

    # ----- 3. Action-specific apply -----
    action = row["proposed_action"]
    base_field_versions = row["base_field_versions"]
    if isinstance(base_field_versions, str):
        base_field_versions = json.loads(base_field_versions)
    base_field_versions = base_field_versions or {}

    if action == "CREATE_TASK":
        applied_task_id = await _apply_create_task(
            conn,
            review_item_id=review_item_id,
            ingest_event_id=row["ingest_event_id"],
            proposed_patch=proposed_patch,
            confidence=row["confidence"],
            reviewer=reviewer,
        )
    elif action == "UPDATE_TASK":
        if not row["target_task_id"]:
            raise ApplyConflict("target_task_id_required",
                                {"proposed_action": action})
        applied_task_id = await _apply_update_task(
            conn,
            review_item_id=review_item_id,
            ingest_event_id=row["ingest_event_id"],
            target_task_id=row["target_task_id"],
            proposed_patch=proposed_patch,
            base_task_version=row["base_task_version"],
            base_field_versions=base_field_versions,
            confidence=row["confidence"],
            reviewer=reviewer,
        )
    elif action == "COMPLETE_TASK":
        if not row["target_task_id"]:
            raise ApplyConflict("target_task_id_required",
                                {"proposed_action": action})
        applied_task_id = await _apply_complete_task(
            conn,
            review_item_id=review_item_id,
            ingest_event_id=row["ingest_event_id"],
            target_task_id=row["target_task_id"],
            proposed_patch=proposed_patch,
            base_task_version=row["base_task_version"],
            confidence=row["confidence"],
            reviewer=reviewer,
        )
    elif action in ("IGNORE", "AMBIGUOUS"):
        # Reviewer can only "approve" actionable items; AMBIGUOUS/IGNORE
        # should be rejected, not approved.
        raise ApplyConflict("non_actionable_action",
                            {"proposed_action": action})
    else:
        raise ApplyConflict("unknown_action", {"proposed_action": action})

    # ----- 4. Mark review_item approved -----
    reviewer_id = reviewer.id if reviewer.principal_type == "user" else None
    await conn.execute(
        """
        UPDATE review_items
           SET status            = 'approved',
               reviewer_id       = $2::uuid,
               reviewed_at       = now(),
               applied_at        = now(),
               applied_task_id   = $3::uuid,
               apply_mutation_id = $4,
               last_apply_error  = '{}'::jsonb
         WHERE id = $1::uuid
        """,
        review_item_id,
        reviewer_id,
        applied_task_id,
        apply_mutation_id,
    )

    log.info(
        "review_item_applied",
        extra={
            "review_item_id": review_item_id,
            "applied_task_id": applied_task_id,
            "action": action,
            "reviewer_id": reviewer_id,
            "apply_mutation_id": apply_mutation_id,
        },
    )
    return {
        "status": "approved",
        "review_item_id": review_item_id,
        "applied_task_id": applied_task_id,
        "apply_mutation_id": apply_mutation_id,
        "deduped": False,
    }


# ---------------------------------------------------------------------------
# CREATE_TASK
# ---------------------------------------------------------------------------

# Fields whose values come from the proposed_patch.create payload. These
# get a row in task_field_versions on create so future per-field
# concurrency checks have a baseline to compare against.
_CREATE_FIELD_NAMES = ("summary", "due_at", "category", "dependency_text")


async def _apply_create_task(
    conn,
    *,
    review_item_id: str,
    ingest_event_id: str,
    proposed_patch: dict,
    confidence: float | None,
    reviewer: Principal,
) -> str:
    """Apply a CREATE_TASK proposal. Returns the new task id."""
    create = proposed_patch.get("create") or {}
    summary = (create.get("summary") or "").strip()
    if not summary:
        raise ApplyValidationError([{"code": "summary_required",
                                     "message": "create.summary missing"}])
    business_slugs = create.get("businesses") or []
    if not business_slugs:
        raise ApplyValidationError([{"code": "businesses_required",
                                     "message": "create.businesses empty"}])

    # Resolve businesses → ids; refuse on unknown slug.
    biz_rows = await conn.fetch(
        "SELECT id::text AS id, slug::text AS slug FROM businesses "
        "WHERE slug::text = ANY($1::text[]) AND deletion_state = 'active'",
        business_slugs,
    )
    found_slugs = {r["slug"] for r in biz_rows}
    missing = [s for s in business_slugs if s not in found_slugs]
    if missing:
        raise ApplyConflict("business_not_found",
                            {"missing_slugs": missing})

    reviewer_id = reviewer.id if reviewer.principal_type == "user" else None

    # ----- Insert tasks -----
    # Codex chunk-7-step3 close-fix: description + priority were in the
    # SOP authoring schema + read API but the apply path dropped them.
    # Pass through whenever the patch carries them; non-SOP review_items
    # (meeting_recap / slack_message) populate via .get() returning None.
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
        summary,
        create.get("description"),
        _coerce_due_at(create.get("due_at")),
        create.get("category"),
        create.get("priority"),
        create.get("dependency_text"),
        ingest_event_id,
    )
    task_id = task_row["id"]

    # ----- Insert task_businesses -----
    for biz in biz_rows:
        await conn.execute(
            "INSERT INTO task_businesses (task_id, business_id, added_by) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid)",
            task_id,
            biz["id"],
            reviewer_id,
        )

    # ----- Insert task_assignees (when slack_resolve gave us an owner) -----
    # Closes Codex chunk-5-close blocker: pipeline now carries
    # owner_user_id in proposed_patch.create; the apply path materializes
    # the assignment so the canonical user appears as the task's
    # assignee on the dashboard. None means "no resolved owner" — the
    # task is created with no assignee and the reviewer can add one
    # via PATCH later.
    owner_user_id = create.get("owner_user_id")
    if owner_user_id:
        # Verify the user still exists and is active before assigning;
        # a stale review_item could reference a removed user.
        user_row = await conn.fetchrow(
            "SELECT id::text AS id FROM users "
            "WHERE id = $1::uuid AND status = 'active'",
            owner_user_id,
        )
        if user_row:
            await conn.execute(
                """
                INSERT INTO task_assignees
                  (task_id, user_id, role, assigned_by)
                VALUES
                  ($1::uuid, $2::uuid, 'assignee', $3::uuid)
                ON CONFLICT (task_id, user_id) DO NOTHING
                """,
                task_id,
                user_row["id"],
                reviewer_id,
            )

    # ----- Insert task_field_versions baseline -----
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
            reviewer_id,
            ingest_event_id,
        )

    # ----- Insert task_history (single create row) -----
    actor_type = "user" if reviewer.principal_type == "user" else "service"
    actor_user_id = reviewer.id if actor_type == "user" else None
    actor_service_id = reviewer.id if actor_type == "service" else None
    new_value = {
        "summary": summary,
        "due_at": create.get("due_at"),
        "category": create.get("category"),
        "dependency_text": create.get("dependency_text"),
        "businesses": business_slugs,
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
        f"review:{review_item_id}",
        new_value,
        actor_user_id,
        actor_service_id,
        actor_type,
        ingest_event_id,
        f"approved review_item {review_item_id}",
        confidence,
    )

    # ----- Insert task_state_transitions: one per business -----
    for biz in biz_rows:
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
            f"create from review_item {review_item_id}",
            {"review_item_id": review_item_id,
             "ingest_event_id": ingest_event_id},
        )

    # ----- SOP linkage backfill (Chunk 7 step 3c) -----
    # If this review_item came from an SOP anchor fire, the
    # sop_generated_tasks junction has a row pointing at it with
    # task_id=NULL. Set task_id now so subsequent date-shift
    # propagation (chunk 7 follow-up) and audit pane queries can
    # find the materialized task. WHERE task_id IS NULL ensures we
    # don't clobber a backfill from a reapply / future re-bind path.
    await conn.execute(
        """
        UPDATE sop_generated_tasks
           SET task_id = $2::uuid
         WHERE review_item_id = $1::uuid
           AND task_id IS NULL
        """,
        review_item_id,
        task_id,
    )

    return task_id


# ---------------------------------------------------------------------------
# UPDATE_TASK
# ---------------------------------------------------------------------------

# Strict allowlist of fields the apply path will mutate via UPDATE_TASK.
# Anything outside this set in proposed_patch.update is a validation
# error — never silently ignored. Status-changing fields (status,
# completed_at, completed_by, completion_note, deletion_state) are
# reserved for COMPLETE_TASK / future delete flows so the audit trail
# stays interpretable. Provenance/concurrency columns (id, version,
# source_event_id, created_at, updated_at, last_activity_at) are
# managed by the apply path itself.
_UPDATE_MUTABLE_FIELDS = ("summary", "due_at", "category", "dependency_text")


async def _apply_update_task(
    conn,
    *,
    review_item_id: str,
    ingest_event_id: str,
    target_task_id: str,
    proposed_patch: dict,
    base_task_version: int | None,
    base_field_versions: dict,
    confidence: float | None,
    reviewer: Principal,
) -> str:
    """Apply an UPDATE_TASK proposal with version-vector concurrency.

    Returns target_task_id on success.

    Conflicts (raise ApplyConflict; demote to needs_changes):
      - target_not_found / target_deleted
      - task_version_moved (tasks.version != base_task_version)
      - field_version_moved (any touched field's version doesn't match)
      - update_field_unknown (proposed field outside the allowlist)
    """
    update = proposed_patch.get("update") or {}
    if not isinstance(update, dict) or not update:
        raise ApplyValidationError([{"code": "update_empty",
                                     "message": "proposed_patch.update missing or empty"}])

    bad_fields = [k for k in update.keys() if k not in _UPDATE_MUTABLE_FIELDS]
    if bad_fields:
        raise ApplyValidationError([{
            "code": "update_field_unknown",
            "message": f"fields {bad_fields} not in UPDATE allowlist {list(_UPDATE_MUTABLE_FIELDS)}",
        }])
    # Defensive: summary is NOT NULL in the tasks schema. PATCH already
    # rejects this in v1_review.py, but the apply path runs against
    # arbitrary historic review_items (including ones queued by the
    # pipeline before any of these guards were in place), so re-check.
    if "summary" in update and update["summary"] is None:
        raise ApplyValidationError([{
            "code": "summary_null_invalid",
            "message": "summary cannot be null on UPDATE_TASK",
        }])

    # ----- Lock target -----
    task_row = await conn.fetchrow(
        """
        SELECT id::text AS id, summary, due_at::text AS due_at,
               category, dependency_text,
               status::text AS status,
               deletion_state::text AS deletion_state,
               version
        FROM tasks WHERE id = $1::uuid FOR UPDATE
        """,
        target_task_id,
    )
    if not task_row:
        raise ApplyConflict("target_not_found", {"target_task_id": target_task_id})
    if task_row["deletion_state"] != "active":
        raise ApplyConflict("target_deleted",
                            {"target_task_id": target_task_id,
                             "deletion_state": task_row["deletion_state"]})

    # ----- Concurrency check: whole-task version -----
    if base_task_version is not None and task_row["version"] != base_task_version:
        raise ApplyConflict("task_version_moved", {
            "target_task_id": target_task_id,
            "base_task_version": base_task_version,
            "current_task_version": task_row["version"],
        })

    # ----- Concurrency check: per-field versions for touched fields -----
    fv_rows = await conn.fetch(
        "SELECT field_name, version FROM task_field_versions "
        "WHERE task_id = $1::uuid AND field_name = ANY($2::text[])",
        target_task_id,
        list(update.keys()),
    )
    current_fv = {r["field_name"]: r["version"] for r in fv_rows}
    for field_name in update.keys():
        base_v = base_field_versions.get(field_name)
        cur_v = current_fv.get(field_name)
        # Allow None base only when the field has no row yet (first
        # mutation of a field on an old task). Otherwise require equality.
        if base_v is None and cur_v is None:
            continue
        if base_v != cur_v:
            raise ApplyConflict("field_version_moved", {
                "target_task_id": target_task_id,
                "field_name": field_name,
                "base_version": base_v,
                "current_version": cur_v,
            })

    # ----- Mutate tasks (single UPDATE for all changed fields) -----
    # Build dynamic SET for the touched fields. The allowlist guarantees
    # field names are safe identifiers.
    set_fragments: list[str] = []
    params: list[Any] = [target_task_id]
    for fname, fval in update.items():
        # due_at comes through jsonb as a string; asyncpg::timestamptz
        # bind requires a datetime instance. _coerce_due_at handles both.
        if fname == "due_at":
            fval = _coerce_due_at(fval)
        params.append(fval)
        cast = "::timestamptz" if fname == "due_at" else ""
        set_fragments.append(f"{fname} = ${len(params)}{cast}")
    set_fragments.append("version = version + 1")
    set_fragments.append("last_activity_at = now()")
    set_fragments.append("updated_at = now()")
    sql = f"UPDATE tasks SET {', '.join(set_fragments)} WHERE id = $1::uuid"
    await conn.execute(sql, *params)

    reviewer_id = reviewer.id if reviewer.principal_type == "user" else None

    # ----- Bump per-field version counters (one row per touched field) -----
    for fname in update.keys():
        await conn.execute(
            """
            INSERT INTO task_field_versions
              (task_id, field_name, version, updated_by, source_event_id)
            VALUES ($1::uuid, $2, 1, $3::uuid, $4::uuid)
            ON CONFLICT (task_id, field_name) DO UPDATE
              SET version         = task_field_versions.version + 1,
                  updated_at      = now(),
                  updated_by      = EXCLUDED.updated_by,
                  source_event_id = EXCLUDED.source_event_id
            """,
            target_task_id,
            fname,
            reviewer_id,
            ingest_event_id,
        )

    # ----- One task_history row per changed field -----
    actor_type = "user" if reviewer.principal_type == "user" else "service"
    actor_user_id = reviewer.id if actor_type == "user" else None
    actor_service_id = reviewer.id if actor_type == "service" else None
    for fname, new_val in update.items():
        old_val = task_row[fname] if fname in task_row else None
        await conn.execute(
            """
            INSERT INTO task_history
              (task_id, mutation_id, field_name, change_type,
               old_value, new_value,
               actor_user_id, actor_service_account_id, actor_type,
               source_event_id, reason, confidence)
            VALUES
              ($1::uuid, $2, $3, 'update',
               $4::jsonb, $5::jsonb,
               $6::uuid, $7::uuid, $8,
               $9::uuid, $10, $11)
            """,
            target_task_id,
            f"review:{review_item_id}:{fname}",
            fname,
            old_val,
            new_val,
            actor_user_id,
            actor_service_id,
            actor_type,
            ingest_event_id,
            f"approved review_item {review_item_id} (field {fname})",
            confidence,
        )

    return target_task_id


# ---------------------------------------------------------------------------
# COMPLETE_TASK
# ---------------------------------------------------------------------------

# Fields that change on a complete. Per Codex chunk-4-step1 review:
# track these explicitly so the version vocabulary is documented.
_COMPLETE_FIELD_NAMES = ("status", "completed_at", "completed_by", "completion_note")


async def _apply_complete_task(
    conn,
    *,
    review_item_id: str,
    ingest_event_id: str,
    target_task_id: str,
    proposed_patch: dict,
    base_task_version: int | None,
    confidence: float | None,
    reviewer: Principal,
) -> str:
    """Apply a COMPLETE_TASK proposal. Returns target_task_id.

    COMPLETE uses whole-task version concurrency only (base_task_version
    must equal current tasks.version). Per-field comparison is overkill
    here — completion is a coherent transition; if anyone touched the
    task in any way since the review was queued, the reviewer should
    look again.

    Conflicts:
      - target_not_found / target_deleted
      - task_already_done (current status is 'done')
      - task_version_moved
    """
    # Strict patch-shape validation. Symmetric with UPDATE_TASK's allowlist.
    complete = proposed_patch.get("complete")
    if complete is None:
        complete = {}
    if not isinstance(complete, dict):
        raise ApplyValidationError([{
            "code": "complete_invalid",
            "message": "proposed_patch.complete must be an object",
        }])
    extra = [k for k in complete.keys() if k != "completion_note"]
    if extra:
        raise ApplyValidationError([{
            "code": "complete_field_unknown",
            "message": f"unknown complete keys: {extra}",
        }])
    completion_note = complete.get("completion_note")
    if completion_note is not None and not isinstance(completion_note, str):
        raise ApplyValidationError([{
            "code": "completion_note_invalid",
            "message": "completion_note must be a string or null",
        }])

    task_row = await conn.fetchrow(
        """
        SELECT id::text AS id, status::text AS status,
               deletion_state::text AS deletion_state,
               completion_note,
               completed_at::text AS completed_at,
               completed_by::text AS completed_by,
               version
        FROM tasks WHERE id = $1::uuid FOR UPDATE
        """,
        target_task_id,
    )
    if not task_row:
        raise ApplyConflict("target_not_found", {"target_task_id": target_task_id})
    if task_row["deletion_state"] != "active":
        raise ApplyConflict("target_deleted",
                            {"target_task_id": target_task_id,
                             "deletion_state": task_row["deletion_state"]})
    if task_row["status"] == "done":
        raise ApplyConflict("task_already_done",
                            {"target_task_id": target_task_id,
                             "current_status": task_row["status"]})
    if base_task_version is not None and task_row["version"] != base_task_version:
        raise ApplyConflict("task_version_moved", {
            "target_task_id": target_task_id,
            "base_task_version": base_task_version,
            "current_task_version": task_row["version"],
        })

    reviewer_id = reviewer.id if reviewer.principal_type == "user" else None

    # ----- Mutate tasks: status -> done, completed_at, completed_by, note -----
    completed_row = await conn.fetchrow(
        """
        UPDATE tasks
           SET status          = 'done',
               completed_at    = now(),
               completed_by    = $2::uuid,
               completion_note = $3,
               version         = version + 1,
               last_activity_at = now(),
               updated_at      = now()
         WHERE id = $1::uuid
        RETURNING completed_at::text AS completed_at
        """,
        target_task_id,
        reviewer_id,
        completion_note,
    )
    new_completed_at = completed_row["completed_at"] if completed_row else None

    # ----- Bump per-field versions (status / completed_at / completed_by / completion_note) -----
    for fname in _COMPLETE_FIELD_NAMES:
        await conn.execute(
            """
            INSERT INTO task_field_versions
              (task_id, field_name, version, updated_by, source_event_id)
            VALUES ($1::uuid, $2, 1, $3::uuid, $4::uuid)
            ON CONFLICT (task_id, field_name) DO UPDATE
              SET version         = task_field_versions.version + 1,
                  updated_at      = now(),
                  updated_by      = EXCLUDED.updated_by,
                  source_event_id = EXCLUDED.source_event_id
            """,
            target_task_id,
            fname,
            reviewer_id,
            ingest_event_id,
        )

    # ----- task_history: one row per mutated field -----
    actor_type = "user" if reviewer.principal_type == "user" else "service"
    actor_user_id = reviewer.id if actor_type == "user" else None
    actor_service_id = reviewer.id if actor_type == "service" else None

    history_rows = (
        ("status", task_row["status"], "done"),
        ("completed_at", task_row["completed_at"], new_completed_at),
        ("completed_by", task_row["completed_by"],
         str(reviewer_id) if reviewer_id else None),
        ("completion_note", task_row["completion_note"], completion_note),
    )
    for fname, old_val, new_val in history_rows:
        await conn.execute(
            """
            INSERT INTO task_history
              (task_id, mutation_id, field_name, change_type,
               old_value, new_value,
               actor_user_id, actor_service_account_id, actor_type,
               source_event_id, reason, confidence)
            VALUES
              ($1::uuid, $2, $3, 'complete',
               $4::jsonb, $5::jsonb,
               $6::uuid, $7::uuid, $8,
               $9::uuid, $10, $11)
            """,
            target_task_id,
            f"review:{review_item_id}:{fname}",
            fname,
            old_val,
            new_val,
            actor_user_id,
            actor_service_id,
            actor_type,
            ingest_event_id,
            f"approved complete from review_item {review_item_id} (field {fname})",
            confidence,
        )

    # ----- task_state_transitions: open -> done, one per business -----
    biz_rows = await conn.fetch(
        "SELECT business_id::text AS business_id "
        "FROM task_businesses WHERE task_id = $1::uuid",
        target_task_id,
    )
    for biz in biz_rows:
        await conn.execute(
            """
            INSERT INTO task_state_transitions
              (task_id, business_id, from_state, to_state,
               actor_kind, actor_user_id, actor_service_account_id,
               reason, metadata)
            VALUES
              ($1::uuid, $2::uuid, 'open', 'done',
               $3, $4::uuid, $5::uuid,
               $6, $7::jsonb)
            """,
            target_task_id,
            biz["business_id"],
            actor_type,
            actor_user_id,
            actor_service_id,
            f"complete from review_item {review_item_id}",
            {"review_item_id": review_item_id,
             "ingest_event_id": ingest_event_id},
        )

    return target_task_id

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
from typing import Any

from .auth import Principal
from .reconciliation.validate import validate_decision

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
    if action == "CREATE_TASK":
        applied_task_id = await _apply_create_task(
            conn,
            review_item_id=review_item_id,
            ingest_event_id=row["ingest_event_id"],
            proposed_patch=proposed_patch,
            confidence=row["confidence"],
            reviewer=reviewer,
        )
    elif action in ("UPDATE_TASK", "COMPLETE_TASK"):
        # Next commit lands UPDATE/COMPLETE with version-vector concurrency
        # checks against base_task_version + base_field_versions.
        raise ApplyNotImplemented(action)
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
    task_row = await conn.fetchrow(
        """
        INSERT INTO tasks
          (summary, due_at, category, dependency_text,
           source_event_id, version, last_activity_at,
           created_at, updated_at)
        VALUES
          ($1, $2::timestamptz, $3, $4,
           $5::uuid, 1, now(),
           now(), now())
        RETURNING id::text AS id
        """,
        summary,
        create.get("due_at"),
        create.get("category"),
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
        json.dumps(new_value),
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
            json.dumps({"review_item_id": review_item_id,
                        "ingest_event_id": ingest_event_id}),
        )

    return task_id

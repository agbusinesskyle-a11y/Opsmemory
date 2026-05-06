"""Step 5: validate the proposed mutation against schema + authz.

Deterministic. Output is a list of validation_error objects. Empty list
means "this proposal can proceed to queue review." Non-empty means the
review item is queued anyway, but with the errors recorded so the human
reviewer sees them and can edit/reject without applying.

Chunk 4 will use this same validator at apply time as the final gate.
"""

from __future__ import annotations

from typing import Any


async def validate_decision(
    conn,
    candidate: dict,
    decision: dict,
    *,
    actor_business_slugs: list[str] | None = None,
) -> list[dict]:
    """Return a list of validation errors (each {code, message, field?}).

    `actor_business_slugs` is the business slug list the ingest actor is
    scoped to (None = admin/system, no scoping). When the candidate
    references businesses outside that scope, an authz error is added so
    the reviewer sees that the actor was implicitly trying to write
    cross-business.
    """
    errors: list[dict] = []

    # Actor authz: the candidate's businesses must be a subset of what
    # the actor is allowed to see. Admin/system actors (None) bypass.
    if actor_business_slugs is not None:
        cand_biz = candidate.get("businesses") or []
        unauthorized = [b for b in cand_biz if b not in actor_business_slugs]
        if unauthorized:
            errors.append({
                "code": "actor_business_unauthorized",
                "message": (
                    f"actor lacks membership in business(es) {unauthorized}; "
                    f"candidate references {cand_biz}, actor scoped to "
                    f"{actor_business_slugs}"
                ),
            })

    action = decision.get("action")
    target = decision.get("target_task_id")

    if action == "CREATE_TASK":
        # Must have a summary and at least one valid business.
        summary = candidate.get("summary", "").strip()
        if not summary:
            errors.append({"code": "summary_required", "message": "candidate has no summary"})
        elif len(summary) > 4096:
            errors.append({"code": "summary_too_long", "message": f"summary is {len(summary)} chars (max 4096)"})
        if not candidate.get("businesses"):
            errors.append({"code": "businesses_required",
                           "message": "candidate must reference at least one business (redhot or borderline)"})

    elif action in {"UPDATE_TASK", "COMPLETE_TASK"}:
        if not target:
            errors.append({"code": "target_task_id_required",
                           "message": f"{action} requires target_task_id"})
        else:
            # Verify the target exists, isn't deleted, and matches the
            # candidate's businesses.
            row = await conn.fetchrow(
                """
                SELECT t.id::text AS id, t.deletion_state::text AS deletion_state,
                       array_agg(b.slug::text) AS businesses
                FROM tasks t
                LEFT JOIN task_businesses tb ON tb.task_id = t.id
                LEFT JOIN businesses b ON b.id = tb.business_id
                WHERE t.id = $1::uuid
                GROUP BY t.id
                """,
                target,
            )
            if not row:
                errors.append({"code": "target_not_found",
                               "message": f"target_task_id {target} does not exist"})
            elif row["deletion_state"] != "active":
                errors.append({"code": "target_deleted",
                               "message": f"target_task_id {target} is in deletion_state {row['deletion_state']}"})
            else:
                target_businesses = [b for b in (row["businesses"] or []) if b]
                candidate_businesses = candidate.get("businesses") or []
                if candidate_businesses and target_businesses and not (
                    set(target_businesses) & set(candidate_businesses)
                ):
                    errors.append({
                        "code": "business_mismatch",
                        "message": f"target task businesses {target_businesses} don't intersect candidate businesses {candidate_businesses}",
                    })

    elif action == "IGNORE":
        # Always valid — just drops the candidate.
        pass

    elif action == "AMBIGUOUS":
        # Always valid — explicit "I can't tell" outcome from the LLM.
        pass

    else:
        errors.append({"code": "unknown_action", "message": f"unrecognized action {action!r}"})

    return errors

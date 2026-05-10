"""Step orchestrator: run extract -> normalize -> retrieve -> choose
-> validate -> queue review for one ingest_event.

Step 7 (apply) is NOT in this module — that lands in Chunk 4 alongside
the review approval UI. Chunk 3 stops at "queue review" by INSERTing
review_items rows in status='pending'.

Workflow guarantees:
  - Every LLM call is recorded in llm_calls (success OR failure).
  - The ingest_event status moves received -> extracting -> pending_review
    (or -> failed if anything in extract/choose fails outside the
    fallback chain).
  - One review_item per candidate the extract step produced. If extract
    produced zero candidates, the event still moves to 'completed' with
    no review items.
  - Pipeline failures don't roll back the ingest_event row itself; they
    record an `error` field and bump retry_count. Ingest is durable.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .extract import extract
from .file_drop_resolve import resolve_file_drop_context
from .llm_client import BudgetExceeded, BudgetUnknown
from .normalize import normalize_candidates
from .retrieve import retrieve_candidates
from .choose import choose_action
from .slack_resolve import resolve_slack_context
from .sources import get_source_config
from .validate import validate_decision

log = logging.getLogger("opsmemory.reconciliation.pipeline")


def _build_budget_pre_check(conn) -> Callable[[], Awaitable[None]]:
    """Return a pre_check callable that raises BudgetExceeded if today's
    cumulative llm_calls.cost_usd has reached INGEST_LLM_DAILY_USD_CAP.

    The day boundary is UTC midnight (matches llm_calls.created_at::date
    semantics in postgres). This is best-effort: a tick that's already
    past the start of the choose loop can overshoot by up to one
    candidate's cost while concurrent ticks race the same SUM. For
    operator-scale volumes this is fine; if Slack ingest pushes
    >100 events/day we should add a row-locked counter.
    """
    cap_str = (os.environ.get("INGEST_LLM_DAILY_USD_CAP") or "").strip()
    cap: float | None
    if cap_str:
        try:
            cap = float(cap_str)
        except ValueError:
            log.warning("budget_cap_unparseable", extra={"value": cap_str})
            cap = None
    else:
        cap = None

    async def check() -> None:
        if cap is None:
            return
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(cost_usd), 0)::float8 AS spent "
            "FROM llm_calls "
            "WHERE created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') "
            "  AND cost_usd IS NOT NULL"
        )
        spent = float(row["spent"]) if row else 0.0
        if spent >= cap:
            # Format both sides with 4 decimals so sub-cent caps used
            # for testing (e.g. INGEST_LLM_DAILY_USD_CAP=0.001) don't
            # render as "$0.00" in the operator-visible error.
            raise BudgetExceeded(
                f"daily LLM cap reached: ${spent:.4f} of ${cap:.4f} "
                f"(INGEST_LLM_DAILY_USD_CAP)"
            )

    return check


async def _record_llm_call(conn, *, ingest_event_id: str, review_item_id: str | None, call) -> None:
    """Persist one llm_calls row from an LlmCall dataclass.

    Pool's jsonb codec handles encoding — pass raw Python dicts, not
    pre-serialized strings, or asyncpg double-encodes (chunk-4-step2
    Codex blocker).
    """
    await conn.execute(
        """
        INSERT INTO llm_calls
          (ingest_event_id, review_item_id, step, provider, model,
           prompt_template, prompt_hash, request_body, response,
           input_tokens, output_tokens, cost_usd, latency_ms,
           status, error, request_id, completed_at)
        VALUES
          ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb,
           $10, $11, $12, $13, $14, $15, NULL, now())
        """,
        ingest_event_id,
        review_item_id,
        call.step,
        call.provider,
        call.model,
        call.prompt_template,
        call.prompt_hash,
        call.request_body,
        call.response,
        call.input_tokens,
        call.output_tokens,
        call.cost_usd,
        call.latency_ms,
        call.status,
        call.error,
    )


async def _resolve_actor_business_ids(conn, event) -> list[str] | None:
    """Return the list of business UUIDs the ingest actor is scoped to.

    Result semantics match authz.visible_business_ids:
      - None  → no scoping (admin user OR system actor; sees everything)
      - []    → service principal with no business_memberships
      - [...] → owner / scoped-service: only these business ids
    """
    actor_type = event["actor_type"]
    if actor_type == "system":
        return None
    if actor_type == "user" and event["actor_user_id"]:
        row = await conn.fetchrow(
            "SELECT role::text AS role FROM users WHERE id = $1::uuid",
            event["actor_user_id"],
        )
        # MT-2: platform_admin = unrestricted. No 'admin' alias —
        # 0023 must apply first or pre-migration rows are scoped.
        if row and row["role"] == "platform_admin":
            return None
        # Owner: scope to their business_memberships.
        biz_rows = await conn.fetch(
            "SELECT business_id::text AS id "
            "FROM business_memberships WHERE user_id = $1::uuid",
            event["actor_user_id"],
        )
        return [r["id"] for r in biz_rows]
    if actor_type == "service" and event["actor_service_account_id"]:
        # Service accounts that hold `pipeline:read:all_businesses` see
        # all businesses for retrieval. Codex chunk-5-step2 split this
        # from `ingest:write` per least-privilege: a write-only ingest
        # key shouldn't implicitly cross-read business data. Operator
        # grants both scopes to the slack ingest service account.
        # `tasks:read:all` is honored for back-compat with existing
        # service accounts.
        row = await conn.fetchrow(
            "SELECT scopes FROM service_accounts WHERE id = $1::uuid",
            event["actor_service_account_id"],
        )
        scopes = (row["scopes"] if row else []) or []
        if "pipeline:read:all_businesses" in scopes or "tasks:read:all" in scopes:
            return None
        return []
    # Fallback: no scoping derivable; refuse to mass-leak — empty.
    return []


async def process_event(conn, event_id: str) -> dict:
    """Run the 6-step pipeline for one ingest_event. Returns a summary dict.

    Assumes the caller has already claimed the row (status='extracting',
    processing_started_at set). See scripts/run_pipeline.py for the
    atomic claim path.
    """
    log.info("pipeline_start", extra={"event_id": event_id})

    # ---- Read the event ----
    event = await conn.fetchrow(
        "SELECT id::text AS id, source, raw_content, status::text AS status, "
        "       retry_count, actor_type, "
        "       actor_user_id::text AS actor_user_id, "
        "       actor_service_account_id::text AS actor_service_account_id, "
        "       source_metadata, received_at "
        "FROM ingest_events WHERE id = $1::uuid",
        event_id,
    )
    if not event:
        return {"event_id": event_id, "status": "missing"}

    # Source registry: refuse anything not configured. This prevents a
    # forgotten source registration from quietly stranding ingest_events
    # in 'extracting' (the worker's stale-recovery would just re-claim
    # them forever). The event is marked failed with a clear error so
    # operator monitoring catches it.
    source_config = get_source_config(event["source"])
    if source_config is None:
        await conn.execute(
            "UPDATE ingest_events SET status = 'failed', failed_at = now(), "
            "error = $2, retry_count = retry_count + 1 WHERE id = $1::uuid",
            event_id,
            f"source {event['source']!r} not registered in reconciliation/sources.py",
        )
        log.warning("pipeline_unregistered_source",
                    extra={"event_id": event_id, "source": event["source"]})
        return {"event_id": event_id, "status": "failed",
                "stage": "source_registry",
                "reason": f"source={event['source']!r} not registered"}

    actor_business_ids = await _resolve_actor_business_ids(conn, event)

    budget_pre_check = _build_budget_pre_check(conn)

    candidates_raw: list[dict] = []
    try:
        async def on_extract_call(call):
            await _record_llm_call(conn, ingest_event_id=event_id, review_item_id=None, call=call)
        candidates_raw, _ = await extract(
            source_config=source_config,
            raw_content=event["raw_content"],
            source_metadata=event["source_metadata"],
            on_call=on_extract_call,
            pre_check=budget_pre_check,
        )
    except BudgetExceeded as exc:
        # Daily cap reached. Mark event failed but keep retry_count
        # bumped so tomorrow's first tick re-claims it (status='failed'
        # is in the worker's recovery WHERE clause). Operator can also
        # raise the cap and re-run sooner.
        await conn.execute(
            "UPDATE ingest_events SET status = 'failed', failed_at = now(), "
            "error = $2, retry_count = retry_count + 1 WHERE id = $1::uuid",
            event_id, f"budget_exceeded: {exc}"[:1024],
        )
        log.warning("pipeline_budget_exceeded",
                    extra={"event_id": event_id, "stage": "extract"})
        return {"event_id": event_id, "status": "failed", "stage": "budget", "error": str(exc)}
    except BudgetUnknown as exc:
        # Operator misconfig: a configured model can't be priced. Fail
        # loudly so it gets fixed rather than silently bypassing the cap.
        await conn.execute(
            "UPDATE ingest_events SET status = 'failed', failed_at = now(), "
            "error = $2, retry_count = retry_count + 1 WHERE id = $1::uuid",
            event_id, f"budget_unknown: {exc}"[:1024],
        )
        log.error("pipeline_budget_unknown",
                  extra={"event_id": event_id, "stage": "extract", "err": repr(exc)})
        return {"event_id": event_id, "status": "failed", "stage": "budget_unknown", "error": str(exc)}
    except Exception as exc:
        await conn.execute(
            "UPDATE ingest_events SET status = 'failed', failed_at = now(), "
            "error = $2, retry_count = retry_count + 1 WHERE id = $1::uuid",
            event_id, f"extract_failed: {exc!r}"[:1024],
        )
        log.warning("pipeline_extract_failed", extra={"event_id": event_id, "err": repr(exc)})
        return {"event_id": event_id, "status": "failed", "stage": "extract", "error": repr(exc)}

    # ---- Normalize ----
    # Anchor relative-date resolution ("Friday", "tomorrow") to the
    # message's source timestamp. For Slack the canonical time is the
    # message ts (epoch seconds in source_metadata.ts); for everything
    # else we fall back to the ingest received_at, which is "now from
    # OpsMemory's perspective." Codex 2026-05-08 review: anchoring to
    # worker-clock UTC instead of message-clock would skew due dates
    # by hours when the worker tick lags behind the post.
    message_ts = None
    src_meta = event["source_metadata"] or {}
    if event["source"] == "slack_message" and src_meta.get("ts"):
        try:
            message_ts = datetime.fromtimestamp(
                float(src_meta["ts"]), tz=timezone.utc
            )
        except (TypeError, ValueError):
            pass
    if message_ts is None:
        message_ts = event["received_at"] or datetime.now(timezone.utc)
    normalized = normalize_candidates(candidates_raw, now=message_ts)

    # ---- Source-specific resolvers (Slack channel + mention -> canonical) ----
    if event["source"] == "slack_message":
        for cand in normalized:
            await resolve_slack_context(
                conn, cand,
                source_metadata=event["source_metadata"],
            )
    # File-drop: deterministic business slug from ingest metadata
    # (Chunk 9 step 2). No LLM business inference; n8n's
    # folder->business mapping is the source of truth.
    elif event["source"] == "file_drop":
        for cand in normalized:
            resolve_file_drop_context(
                cand,
                source_metadata=event["source_metadata"],
            )

    # ---- Source-agnostic owner-name resolution -----------------------------
    # normalize.py mapped owner_hint (free-form name from the LLM) to a
    # canonical owner_display via OWNER_ALIASES_DISPLAY. We now look that
    # canonical name up in the users table to populate owner_user_id, so
    # the CREATE_TASK patch's _apply_create_task can write a
    # task_assignees row on approve. Slack mention-based resolution
    # (slack_resolve) takes priority — only run this for candidates
    # without an already-resolved owner_user_id. case-insensitive match
    # so "Joanna Noriega" / "joanna noriega" both resolve.
    for cand in normalized:
        if cand.get("owner_user_id"):
            continue
        display = cand.get("owner_display")
        if not display:
            continue
        row = await conn.fetchrow(
            "SELECT id::text AS id FROM users "
            "WHERE display_name ILIKE $1 AND status = 'active' "
            "LIMIT 1",
            display,
        )
        if row:
            cand["owner_user_id"] = row["id"]

    if not normalized:
        # No actionable candidates extracted — completed with zero review items.
        await conn.execute(
            "UPDATE ingest_events SET status = 'completed', processed_at = now() WHERE id = $1::uuid",
            event_id,
        )
        log.info("pipeline_completed_no_candidates", extra={"event_id": event_id})
        return {"event_id": event_id, "status": "completed", "review_items": 0}

    # ---- Retrieve + Choose + Validate per candidate ----
    review_count = 0
    # Set inside the choose-loop's BudgetExceeded handler. After the loop,
    # we short-circuit to a 'failed' event before the pending_review UPDATE
    # so the operator sees the budget reason.
    budget_breach: BudgetExceeded | BudgetUnknown | None = None
    biz_map_rows = await conn.fetch("SELECT id::text AS id, slug::text AS slug FROM businesses")
    business_id_by_slug = {r["slug"]: r["id"] for r in biz_map_rows}
    business_slug_by_id = {v: k for k, v in business_id_by_slug.items()}
    # Slug list the actor is scoped to (None = admin/system, no scoping).
    actor_business_slugs: list[str] | None
    if actor_business_ids is None:
        actor_business_slugs = None
    else:
        actor_business_slugs = [business_slug_by_id[bid] for bid in actor_business_ids
                                if bid in business_slug_by_id]

    for cand in normalized:
        retrieved, retrieval_skipped = await retrieve_candidates(
            conn, cand,
            business_id_by_slug=business_id_by_slug,
            actor_business_ids=actor_business_ids,
            due_window_days=source_config.retrieval_due_window_days,
            recency_fallback_days=source_config.retrieval_recency_fallback_days,
        )

        async def on_choose_call_for_cand(call):
            # We need the review_item_id to link, but it doesn't exist yet at this point.
            # Insert llm_call first without review_item_id; chunk 4+ can backfill if needed.
            await _record_llm_call(conn, ingest_event_id=event_id, review_item_id=None, call=call)

        try:
            decision, _ = await choose_action(
                cand, retrieved,
                retrieval_skipped=retrieval_skipped,
                prompt_name=source_config.choose_prompt,
                on_call=on_choose_call_for_cand,
                pre_check=budget_pre_check,
            )
        except (BudgetExceeded, BudgetUnknown) as exc:
            # Halt the whole event; remaining candidates would also fail
            # the budget check. Don't re-raise — set a sentinel so the
            # post-loop short-circuit can mark the event with the
            # specific budget reason. Already-inserted review_items for
            # earlier candidates remain visible (operator can approve
            # them); next-day retry re-runs extract from raw_content.
            budget_breach = exc
            break
        except Exception as exc:
            log.warning("pipeline_choose_failed", extra={"event_id": event_id, "err": repr(exc)})
            decision = {
                "action": "AMBIGUOUS",
                "target_task_id": None,
                "confidence": 0.0,
                "reason": f"choose step failed: {exc!r}"[:512],
            }

        validation_errors = await validate_decision(
            conn, cand, decision,
            actor_business_slugs=actor_business_slugs,
        )

        # Snapshot base versions for chunk 4's transactional recheck.
        base_task_version = None
        base_field_versions: dict = {}
        if decision.get("target_task_id"):
            row = await conn.fetchrow(
                "SELECT version FROM tasks WHERE id = $1::uuid",
                decision["target_task_id"],
            )
            if row:
                base_task_version = row["version"]
            fv_rows = await conn.fetch(
                "SELECT field_name, version FROM task_field_versions WHERE task_id = $1::uuid",
                decision["target_task_id"],
            )
            base_field_versions = {r["field_name"]: r["version"] for r in fv_rows}

        # Build the proposed_patch — chunk 4 will apply it. Concrete shape
        # depends on action; minimal here.
        if decision["action"] == "CREATE_TASK":
            proposed_patch = {
                "create": {
                    "summary": cand["summary"],
                    "due_at": cand.get("due_at"),
                    "category": cand.get("category"),
                    "dependency_text": cand.get("dependency_text"),
                    "businesses": cand.get("businesses") or [],
                    "owner_display_hint": cand.get("owner_display"),
                    # Resolved canonical user from slack_resolve (when
                    # source=slack_message). review_apply._apply_create_task
                    # uses this to insert task_assignees(role='assignee')
                    # on approve. None means "no resolved owner" — the
                    # task is created without an assignee, reviewer can
                    # assign manually post-approve.
                    "owner_user_id": cand.get("owner_user_id"),
                }
            }
        elif decision["action"] == "UPDATE_TASK":
            proposed_patch = {
                "update": {
                    k: v for k, v in {
                        "summary": cand["summary"],
                        "due_at": cand.get("due_at"),
                        "dependency_text": cand.get("dependency_text"),
                    }.items() if v is not None
                }
            }
        elif decision["action"] == "COMPLETE_TASK":
            proposed_patch = {"complete": {"completion_note": cand.get("source_quote")}}
        else:
            proposed_patch = {}

        await conn.execute(
            """
            INSERT INTO review_items
              (ingest_event_id, proposed_action, target_task_id,
               proposed_patch, candidate_facts, retrieved_candidates,
               confidence, reason,
               base_task_version, base_field_versions,
               validation_errors, status)
            VALUES
              ($1::uuid, $2, $3,
               $4::jsonb, $5::jsonb, $6::jsonb,
               $7, $8,
               $9, $10::jsonb,
               $11::jsonb, 'pending')
            """,
            event_id,
            decision["action"],
            decision.get("target_task_id"),
            proposed_patch,
            cand,
            retrieved,
            decision["confidence"],
            decision["reason"],
            base_task_version,
            base_field_versions,
            validation_errors,
        )
        review_count += 1

    # ---- Budget short-circuit (set inside the choose loop) ----
    if budget_breach is not None:
        kind = "budget_unknown" if isinstance(budget_breach, BudgetUnknown) else "budget_exceeded"
        await conn.execute(
            "UPDATE ingest_events SET status = 'failed', failed_at = now(), "
            "error = $2, retry_count = retry_count + 1 WHERE id = $1::uuid",
            event_id, f"{kind}: {budget_breach}"[:1024],
        )
        log_fn = log.error if isinstance(budget_breach, BudgetUnknown) else log.warning
        log_fn(
            f"pipeline_{kind}",
            extra={"event_id": event_id, "stage": "choose",
                   "partial_review_items": review_count},
        )
        return {"event_id": event_id, "status": "failed", "stage": kind,
                "error": str(budget_breach), "review_items": review_count}

    # ---- Mark pending_review ----
    await conn.execute(
        "UPDATE ingest_events SET status = 'pending_review', processed_at = now() WHERE id = $1::uuid",
        event_id,
    )
    log.info(
        "pipeline_completed",
        extra={"event_id": event_id, "review_items": review_count},
    )
    return {"event_id": event_id, "status": "pending_review", "review_items": review_count}

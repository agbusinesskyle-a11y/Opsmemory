"""Source registry for the reconciliation pipeline.

Each known ingest source maps to a SourceConfig: which extract prompt
to use, which choose prompt to use, retrieval window parameters, and
any source-specific normalize hints. The registry is the single place
that gets touched when a new source comes online (Excel drop, email,
SOP-generated, ...).

pipeline.process_event refuses to run extraction on a source not
registered here — better to surface the operator gap than to silently
queue review_items from an unconfigured source.

DIRECT_SOURCES is a separate set of sources whose review_items are
materialized SYNCHRONOUSLY by an admin endpoint (e.g. SOP anchor
fire), not by the LLM pipeline. They write ingest_events for
provenance + sop_generated_tasks junction rows but never go through
extract / normalize / retrieve / choose / validate. The reconciliation
worker (scripts/run_pipeline.py) ignores these sources by virtue of
not finding them in SOURCES; this constant exists so other modules
can check `source in DIRECT_SOURCES` without re-implementing the
list.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceConfig:
    source: str
    extract_prompt: str
    choose_prompt: str
    # ± days window applied around candidate.due_at when present.
    retrieval_due_window_days: int
    # Days threshold against tasks.last_activity_at when candidate has
    # no due_at hint. None disables the recency fallback (older sources
    # like meeting recaps can match against tasks of any age).
    retrieval_recency_fallback_days: int | None


SOURCES: dict[str, SourceConfig] = {
    "meeting_recap": SourceConfig(
        source="meeting_recap",
        extract_prompt="meeting_recap_extract.v1",
        # Source-neutral choose prompt (chunk5 step 2). The original
        # meeting_recap_choose.v1 file remains for historical
        # llm_calls audit reproduction; new pipeline runs use
        # task_choose.v1.
        choose_prompt="task_choose.v1",
        retrieval_due_window_days=30,
        retrieval_recency_fallback_days=None,
    ),
    "slack_message": SourceConfig(
        source="slack_message",
        extract_prompt="slack_message_extract.v2",
        choose_prompt="task_choose.v1",
        retrieval_due_window_days=30,
        # Slack messages are present-tense and noisy — bound retrieval
        # to recently-active tasks when the candidate has no due_at.
        retrieval_recency_fallback_days=14,
    ),
    "file_drop": SourceConfig(
        source="file_drop",
        # CSV-shaped files bypass this prompt (deterministic parse via
        # file_drop_parser.parse_csv_candidates). Free-form text uses
        # this prompt with metadata substitutions.
        extract_prompt="file_drop_extract.v1",
        choose_prompt="task_choose.v1",
        retrieval_due_window_days=30,
        # Codex chunk-9-step1 STEP 2 PLAN: 30, not 14. File drops are
        # structured task lists and can include stale-but-real work
        # (a year-old vendor checklist re-imported). 14d would create
        # duplicate tasks.
        retrieval_recency_fallback_days=30,
    ),
}


def get_source_config(source: str) -> SourceConfig | None:
    return SOURCES.get(source)


# Sources whose review_items get materialized directly by an admin
# endpoint instead of the LLM pipeline. The fire endpoint
# (POST /v1/anchor_events/{id}/fire) inserts ingest_events rows with
# source = 'sop_anchor' for provenance, but the worker never picks
# them up because they're not in SOURCES.
DIRECT_SOURCES: frozenset[str] = frozenset({"sop_anchor"})


def is_direct_source(source: str) -> bool:
    return source in DIRECT_SOURCES

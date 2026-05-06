"""Source registry for the reconciliation pipeline.

Each known ingest source maps to a SourceConfig: which extract prompt
to use, which choose prompt to use, retrieval window parameters, and
any source-specific normalize hints. The registry is the single place
that gets touched when a new source comes online (Excel drop, email,
SOP-generated, ...).

pipeline.process_event refuses to run extraction on a source not
registered here — better to surface the operator gap than to silently
queue review_items from an unconfigured source.
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
    # Whether the extract prompt expects per-message context fields
    # (channel/user/thread) substituted alongside the body.
    expects_context: bool = False


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
        expects_context=False,
    ),
    "slack_message": SourceConfig(
        source="slack_message",
        extract_prompt="slack_message_extract.v1",
        choose_prompt="task_choose.v1",
        retrieval_due_window_days=30,
        # Slack messages are present-tense and noisy — bound retrieval
        # to recently-active tasks when the candidate has no due_at.
        retrieval_recency_fallback_days=14,
        expects_context=True,
    ),
}


def get_source_config(source: str) -> SourceConfig | None:
    return SOURCES.get(source)

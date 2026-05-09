"""Step 1: extract candidate tasks from raw input.

LLM call. Narrow prompt — just "given this text, output candidate
task-shaped facts as JSON." No DB context. No reconciliation.

Source-aware: the source registry (sources.py) names which prompt to
load and which substitutions to make. Meeting recaps get
`{{RECAP_BODY}}`; Slack messages additionally substitute workspace /
channel / user / thread context.
"""

from __future__ import annotations

from typing import Any

from .file_drop_parser import looks_like_csv, parse_csv_candidates
from .llm_client import run_step
from .prompts import load_prompt
from .sources import SourceConfig


async def extract(
    *,
    source_config: SourceConfig,
    raw_content: str,
    source_metadata: dict | None = None,
    on_call=None,
    pre_check=None,
) -> tuple[list[dict], Any]:
    """Run the extract step for one ingest event. Returns
    (candidates_list, last_llm_call_record).

    `source_metadata` is the ingest_events.source_metadata column.
    For Slack we read team_domain / workspace_name / channel_id /
    channel_name / user_id / user_name / thread_ts to fill the prompt
    context fields. Missing fields are substituted as the literal
    string 'unknown' so the prompt template stays well-formed.
    """
    # File-drop CSV path (Chunk 9 step 2): if the content looks
    # CSV-shaped, parse deterministically and skip the LLM entirely.
    # Free-form file content falls through to the LLM extract using
    # file_drop_extract.v1 with metadata substitutions.
    if source_config.source == "file_drop":
        if looks_like_csv(raw_content):
            md = source_metadata or {}
            candidates, parse_kind = parse_csv_candidates(
                raw_content,
                filename=md.get("filename"),
            )
            if parse_kind == "parsed_candidates":
                # CSV parsed cleanly with usable rows. No LlmCall.
                return candidates, None
            if parse_kind == "parsed_empty":
                # CSV parsed cleanly with a recognized header but
                # every row was blank. Don't waste an LLM call on
                # known-empty content (Codex chunk-9-close fix).
                return [], None
            # parse_kind == "unrecognized": no summary column, or
            # csv.Error. Fall through to LLM extract — the file may
            # be free-form text that LLM can still parse.

    template, body, digest = load_prompt(source_config.extract_prompt)

    if source_config.source == "meeting_recap":
        prompt = body.replace("{{RECAP_BODY}}", raw_content)
    elif source_config.source == "file_drop":
        md = source_metadata or {}

        def ctx(key: str) -> str:
            v = md.get(key)
            return str(v) if v else "(not provided)"

        prompt = (body
                  .replace("{{FILE_BODY}}", raw_content)
                  .replace("{{FILENAME}}", ctx("filename"))
                  .replace("{{MIME_TYPE}}", ctx("mime_type"))
                  .replace("{{MODIFIED_TIME}}", ctx("modified_time"))
                  .replace("{{BUSINESS_SLUG}}", ctx("business_slug")))
    elif source_config.source == "slack_message":
        md = source_metadata or {}

        def ctx(key: str) -> str:
            # Codex chunk-5-step2: literal "unknown" reads as a real
            # token to the model. "(not provided)" disambiguates that
            # this is a missing context field, not a value.
            v = md.get(key)
            return str(v) if v else "(not provided)"

        # v2 prompt addition (Codex 2026-05-09): expose
        # extra.reaction_intent so the prompt can adapt for
        # explicitly-tagged-via-reaction messages. Falls back to
        # "(not provided)" for passive ingest / @-mention paths.
        extra = md.get("extra") or {}
        reaction_intent_raw = extra.get("reaction_intent")
        reaction_intent = (
            str(reaction_intent_raw)
            if reaction_intent_raw in ("strong", "weak")
            else "(not provided)"
        )

        prompt = (body
                  .replace("{{MESSAGE_BODY}}", raw_content)
                  .replace("{{WORKSPACE_NAME}}", ctx("workspace_name"))
                  .replace("{{TEAM_DOMAIN}}", ctx("team_domain"))
                  .replace("{{CHANNEL_NAME}}", ctx("channel_name"))
                  .replace("{{CHANNEL_ID}}", ctx("channel_id"))
                  .replace("{{USER_NAME}}", ctx("user_name"))
                  .replace("{{USER_ID}}", ctx("user_id"))
                  .replace("{{THREAD_TS}}", ctx("thread_ts"))
                  .replace("{{REACTION_INTENT}}", reaction_intent))
    else:
        # Should be unreachable — pipeline.process_event refuses
        # unregistered sources before reaching here.
        raise ValueError(f"extract: no prompt substitution for source {source_config.source!r}")

    response, call = await run_step(
        step="extract",
        prompt_template=template,
        prompt_body=prompt,
        prompt_hash=digest,
        on_call=on_call,
        pre_check=pre_check,
    )
    candidates = response.get("candidates", []) if isinstance(response, dict) else []
    if not isinstance(candidates, list):
        candidates = []
    return candidates, call

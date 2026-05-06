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

from .llm_client import run_step
from .prompts import load_prompt
from .sources import SourceConfig


async def extract(
    *,
    source_config: SourceConfig,
    raw_content: str,
    source_metadata: dict | None = None,
    on_call=None,
) -> tuple[list[dict], Any]:
    """Run the extract step for one ingest event. Returns
    (candidates_list, last_llm_call_record).

    `source_metadata` is the ingest_events.source_metadata column.
    For Slack we read team_domain / workspace_name / channel_id /
    channel_name / user_id / user_name / thread_ts to fill the prompt
    context fields. Missing fields are substituted as the literal
    string 'unknown' so the prompt template stays well-formed.
    """
    template, body, digest = load_prompt(source_config.extract_prompt)

    if source_config.source == "meeting_recap":
        prompt = body.replace("{{RECAP_BODY}}", raw_content)
    elif source_config.source == "slack_message":
        md = source_metadata or {}

        def ctx(key: str) -> str:
            v = md.get(key)
            return str(v) if v else "unknown"

        prompt = (body
                  .replace("{{MESSAGE_BODY}}", raw_content)
                  .replace("{{WORKSPACE_NAME}}", ctx("workspace_name"))
                  .replace("{{TEAM_DOMAIN}}", ctx("team_domain"))
                  .replace("{{CHANNEL_NAME}}", ctx("channel_name"))
                  .replace("{{CHANNEL_ID}}", ctx("channel_id"))
                  .replace("{{USER_NAME}}", ctx("user_name"))
                  .replace("{{USER_ID}}", ctx("user_id"))
                  .replace("{{THREAD_TS}}", ctx("thread_ts")))
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
    )
    candidates = response.get("candidates", []) if isinstance(response, dict) else []
    if not isinstance(candidates, list):
        candidates = []
    return candidates, call

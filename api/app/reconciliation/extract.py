"""Step 1: extract candidate tasks from raw input.

LLM call. Narrow prompt — just "given this text, output candidate
task-shaped facts as JSON." No DB context. No reconciliation.
"""

from __future__ import annotations

from typing import Any

from .llm_client import run_step
from .prompts import load_prompt


async def extract_meeting_recap(raw_content: str, *, on_call=None) -> tuple[list[dict], Any]:
    """Run the extract step against the meeting_recap prompt.

    Returns (candidates_list, llm_call_record).
    """
    template, body, digest = load_prompt("meeting_recap_extract.v1")
    prompt = body.replace("{{RECAP_BODY}}", raw_content)
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

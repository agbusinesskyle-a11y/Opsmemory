"""Step 4: choose CREATE / UPDATE / COMPLETE / IGNORE / AMBIGUOUS.

LLM call. Given a normalized candidate + the retrieved candidate tasks,
the model picks one action with confidence + reason.

If retrieval surfaced zero existing tasks AND the candidate is well-formed,
short-circuit to CREATE_TASK with a deterministic reason — saves an LLM
call. The "no candidates" case is unambiguous.
"""

from __future__ import annotations

import json
from typing import Any

from .llm_client import run_step
from .prompts import load_prompt


# Default confidence for the deterministic short-circuit.
SHORT_CIRCUIT_CREATE_CONFIDENCE = 0.85


async def choose_action(
    candidate: dict,
    retrieved: list[dict],
    *,
    on_call=None,
) -> tuple[dict, Any | None]:
    """Run the choose step. Returns (decision_dict, llm_call_record_or_None).

    Decision dict shape:
      { "action", "target_task_id", "confidence", "reason" }
    """
    # Short-circuit: no retrieved candidates -> CREATE_TASK at default confidence.
    if not retrieved:
        return (
            {
                "action": "CREATE_TASK",
                "target_task_id": None,
                "confidence": SHORT_CIRCUIT_CREATE_CONFIDENCE,
                "reason": "No retrieved candidates; new task by default.",
            },
            None,
        )

    template, body, digest = load_prompt("meeting_recap_choose.v1")
    candidate_json = json.dumps(candidate, indent=2, default=str)
    retrieved_json = json.dumps(retrieved, indent=2, default=str)
    prompt = body.replace("{{CANDIDATE_JSON}}", candidate_json) \
                 .replace("{{RETRIEVED_JSON}}", retrieved_json)

    response, call = await run_step(
        step="choose",
        prompt_template=template,
        prompt_body=prompt,
        prompt_hash=digest,
        on_call=on_call,
    )

    # Validate response shape; return a safe fallback if the LLM emitted
    # something unexpected. Better to AMBIGUOUS than to drop into an
    # unsafe state.
    if not isinstance(response, dict):
        return (
            {"action": "AMBIGUOUS", "target_task_id": None, "confidence": 0.0,
             "reason": "LLM response was not a JSON object"},
            call,
        )

    action = response.get("action", "AMBIGUOUS")
    if action not in {"CREATE_TASK", "UPDATE_TASK", "COMPLETE_TASK", "IGNORE", "AMBIGUOUS"}:
        action = "AMBIGUOUS"

    confidence_raw = response.get("confidence", 0)
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except (TypeError, ValueError):
        confidence = 0.0

    target = response.get("target_task_id")
    if target is not None and not isinstance(target, str):
        target = None
    if action == "CREATE_TASK":
        target = None  # by definition

    return (
        {
            "action": action,
            "target_task_id": target,
            "confidence": confidence,
            "reason": (response.get("reason") or "")[:1024],
        },
        call,
    )

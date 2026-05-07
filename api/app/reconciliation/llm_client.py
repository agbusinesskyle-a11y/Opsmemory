"""LLM client wrapper that talks to litellm via OpenAI-compatible API.

Every call records an `llm_calls` row with prompt_template, prompt_hash,
provider, model, tokens, latency, status, and error. This is the
prompt-injection defense audit trail required by the chunk1 design.

Mock mode: set the `mock` provider in the model fallback chain
(e.g., INGEST_LLM_EXTRACT_MODELS=mock) to get deterministic stub
output for dev/test without API access.

Provider rules:
  - The chunk1 design says local Llama is extract/summarize ONLY,
    never invoked at the choose step. Enforced here in code: a model
    with provider 'litellm-local' is rejected at choose-step time.

Cost / budget cap:
  - Every successful real-provider call records input_tokens,
    output_tokens, cost_usd from LiteLLM's response. cost_usd source
    preference: x-litellm-response-cost header (server-authoritative)
    > local PRICES_PER_MTOKEN table. Fail-closed: if neither path can
    price the model, raise BudgetUnknown so a misconfigured model name
    can't silently bypass the cap.
  - run_step accepts an optional pre_check coroutine. The orchestrator
    (pipeline.py) wires it to a SUM(cost_usd) budget query so each call
    is gated by the daily cap. Best-effort: concurrent ticks may
    overshoot at the boundary by up to one call's cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger("opsmemory.reconciliation.llm_client")


class BudgetExceeded(RuntimeError):
    """Daily LLM USD cap reached; defer the call until tomorrow."""


class BudgetUnknown(RuntimeError):
    """Can't compute cost — LiteLLM didn't return cost header AND the
    model is missing from PRICES_PER_MTOKEN. Fail-closed: refuse the
    call rather than silently bypass the cap."""


# Per-model rates in USD per 1M tokens, (input, output).
# Verified prices for the well-known set as of 2026-05. For models where
# this table has no entry, we rely on LiteLLM's x-litellm-response-cost
# header (server-authoritative). gpt-5.x family entries are intentionally
# absent so the LiteLLM header is the only authority for them — bumping
# OpsMemory on every OpenAI gpt-5.x price change would create silent
# drift between the pipeline and the proxy.
PRICES_PER_MTOKEN: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4.1":           (2.00,  8.00),
    # Anthropic
    "claude-opus-4-6":   (15.00, 75.00),
    "claude-sonnet-4-6": (3.00,  15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
    "claude-3-opus":     (15.00, 75.00),
    "claude-3-sonnet":   (3.00,  15.00),
    "claude-3-haiku":    (1.00,  5.00),
}


def _compute_cost(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    header_cost: float | None,
) -> float:
    """Return cost_usd for one call. See module docstring for source order.

    Raises BudgetUnknown if neither LiteLLM's response header nor the
    local table can price this call.
    """
    if header_cost is not None:
        return float(header_cost)
    if input_tokens is None or output_tokens is None:
        raise BudgetUnknown(
            f"model {model!r}: LiteLLM returned no x-litellm-response-cost "
            f"header AND no usage tokens — cannot compute cost"
        )
    rates = PRICES_PER_MTOKEN.get(model)
    if rates is None:
        raise BudgetUnknown(
            f"model {model!r}: LiteLLM returned no x-litellm-response-cost "
            f"header AND model is not in llm_client.PRICES_PER_MTOKEN. "
            f"Add the per-MTok (input, output) rate or upgrade LiteLLM "
            f"to a version that prices this model."
        )
    in_rate, out_rate = rates
    return (input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate


@dataclass
class LlmCall:
    """One attempted call. May be retried against a different model."""
    step: str
    provider: str
    model: str
    prompt_template: str
    prompt_hash: str
    request_body: dict
    response: dict | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    status: str = "pending"   # success | failed | timeout | rate_limited
    error: str | None = None


def _model_chain(env_var: str, default: str) -> list[str]:
    raw = os.environ.get(env_var, default).strip()
    return [m.strip() for m in raw.split(",") if m.strip()]


def _provider_for(model: str) -> str:
    if model == "mock":
        return "mock"
    if "claude" in model:
        return "anthropic"
    if "gpt" in model or "o1" in model or "o3" in model:
        return "openai"
    if "llama" in model or "qwen" in model or "mistral" in model:
        return "litellm-local"
    return "litellm"


def _is_production() -> bool:
    return os.environ.get("ENVIRONMENT", "").strip().lower() == "production"


def _strip_mock_in_production(step: str, chain: list[str]) -> list[str]:
    """Production fail-closed: mock provider is dev-only.

    A misconfigured production worker that defaults to mock would silently
    log fabricated review_items and llm_calls rows. Reject the chain
    instead — the caller surfaces a RuntimeError and the worker exits
    non-zero, which the operator notices.
    """
    if not _is_production():
        return chain
    real = [m for m in chain if _provider_for(m) != "mock"]
    if not real:
        raise RuntimeError(
            f"ENVIRONMENT=production but step {step!r} chain is mock-only "
            f"(set INGEST_LLM_{step.upper()}_MODELS to real providers)"
        )
    return real


def models_for_step(step: str) -> list[str]:
    if step == "extract":
        chain = _model_chain("INGEST_LLM_EXTRACT_MODELS", "mock")
        return _strip_mock_in_production(step, chain)
    if step == "choose":
        # Filter out local models; chunk1 design forbids local Llama at choose.
        chain = _model_chain("INGEST_LLM_CHOOSE_MODELS", "mock")
        chain = [m for m in chain if _provider_for(m) != "litellm-local"]
        return _strip_mock_in_production(step, chain)
    raise ValueError(f"unknown step {step!r}")


# ---------------------------------------------------------------------------
# Mock provider — deterministic; no network.
# ---------------------------------------------------------------------------

def _mock_response(step: str, prompt: str) -> dict:
    """Return a stub response matching what real models would emit.

    Used when INGEST_LLM_*_MODELS includes 'mock'. Lets the pipeline
    run end-to-end on Spark without API keys.
    """
    if step == "extract":
        return {
            "candidates": [
                {
                    "summary": "Test candidate from mock extractor",
                    "owner_hint": None,
                    "businesses_hint": ["redhot"],
                    "due_hint": None,
                    "dependency_hint": None,
                    "category_hint": "test",
                    "source_quote": "mock provider — replace with real LLM",
                    "source_timestamp": None,
                }
            ]
        }
    if step == "choose":
        return {
            "action": "CREATE_TASK",
            "target_task_id": None,
            "confidence": 0.5,
            "reason": "mock provider — replace with real LLM",
        }
    raise ValueError(f"unknown step {step!r}")


# ---------------------------------------------------------------------------
# Real LLM call via litellm OpenAI-compat API
# ---------------------------------------------------------------------------

async def _call_litellm(model: str, prompt: str, *, timeout_s: float = 60.0) -> tuple[dict, dict]:
    """One HTTP call to the litellm proxy. Returns (parsed_json, usage_meta).

    parsed_json is the assistant message's JSON content (after fence-strip).
    usage_meta is {"input_tokens", "output_tokens", "header_cost"} —
    populated from body.usage and the x-litellm-response-cost header
    when present, None when LiteLLM didn't return them.

    Raises Exception on any failure (caller decides whether to retry
    against a different model).
    """
    base_url = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("LITELLM_API_KEY", "")
    if not base_url:
        raise RuntimeError("LITELLM_BASE_URL not set")

    # Lazy import to keep module import cost low when only mock is used.
    import httpx

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    # gpt-5.x family rejects non-default temperature ("Unsupported value:
    # 'temperature' does not support 0 with this model. Only the default
    # (1) value is supported."). Omit the field for that family so OpenAI
    # uses its default; keep temperature=0 for other models where
    # determinism matters and is supported.
    if not model.startswith("gpt-5"):
        payload["temperature"] = 0

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        cost_header_raw = resp.headers.get("x-litellm-response-cost")

    text = body["choices"][0]["message"]["content"]
    # Models can wrap json in code fences; strip them.
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    parsed = json.loads(text)

    usage = body.get("usage") or {}
    header_cost: float | None = None
    if cost_header_raw is not None:
        try:
            header_cost = float(cost_header_raw)
        except (TypeError, ValueError):
            header_cost = None
    usage_meta = {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "header_cost": header_cost,
    }
    return parsed, usage_meta


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_step(
    *,
    step: str,
    prompt_template: str,
    prompt_body: str,
    prompt_hash: str,
    on_call: Callable[[LlmCall], Awaitable[None]] | None = None,
    pre_check: Callable[[], Awaitable[None]] | None = None,
) -> tuple[dict, LlmCall]:
    """Run a pipeline step against the configured model fallback chain.

    Returns (parsed_response, last_attempted_call). The response is the
    parsed JSON body. The call object describes the model that produced
    it (or the last attempt if all failed).

    `on_call` callback fires once per attempt with the LlmCall populated
    enough to write an llm_calls row. Lets the orchestrator persist
    audit rows even on failure.

    `pre_check` callback fires before each model attempt. If it raises
    BudgetExceeded, the loop bails immediately and re-raises (no further
    model is tried, no on_call audit row for the skipped attempt). Other
    exceptions from pre_check propagate and abort the step.
    """
    models = models_for_step(step)
    if not models:
        raise RuntimeError(f"no models configured for step {step!r}")

    last_call: LlmCall | None = None
    last_error: Exception | None = None

    for model in models:
        if pre_check is not None:
            # BudgetExceeded short-circuits the step. Do not catch it
            # in the per-model except blocks below — let the orchestrator
            # mark the event failed and stop processing.
            await pre_check()

        provider = _provider_for(model)
        request_body = {
            "step": step,
            "provider": provider,
            "model": model,
            "prompt_template": prompt_template,
            "prompt_bytes": len(prompt_body),
        }
        call = LlmCall(
            step=step,
            provider=provider,
            model=model,
            prompt_template=prompt_template,
            prompt_hash=prompt_hash,
            request_body=request_body,
        )

        started = time.perf_counter()
        try:
            if provider == "mock":
                parsed = _mock_response(step, prompt_body)
                # Mock has no real usage / cost.
            else:
                parsed, usage_meta = await _call_litellm(model, prompt_body)
                call.input_tokens = usage_meta["input_tokens"]
                call.output_tokens = usage_meta["output_tokens"]
                # _compute_cost raises BudgetUnknown if neither header
                # nor table can price the model — fail-closed by design.
                call.cost_usd = _compute_cost(
                    model,
                    call.input_tokens,
                    call.output_tokens,
                    usage_meta["header_cost"],
                )
            call.response = parsed
            call.status = "success"
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            if on_call:
                await on_call(call)
            return parsed, call
        except asyncio.TimeoutError as exc:
            call.status = "timeout"
            call.error = repr(exc)
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            log.warning("llm_timeout", extra={"model": model, "step": step})
            last_error = exc
        except BudgetUnknown:
            # Don't fall back to next model — every configured model has
            # the same pricing problem (it's our table that's stale, not
            # the upstream provider). Fail the step so operator notices.
            raise
        except Exception as exc:
            call.status = "failed"
            call.error = repr(exc)[:1024]
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            log.warning("llm_failed", extra={"model": model, "step": step, "err": repr(exc)})
            last_error = exc

        if on_call:
            await on_call(call)
        last_call = call
        # Try next model in chain.

    raise RuntimeError(f"all models failed for step {step!r}: {last_error!r}")

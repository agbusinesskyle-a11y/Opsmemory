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
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("opsmemory.reconciliation.llm_client")


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


def models_for_step(step: str) -> list[str]:
    if step == "extract":
        return _model_chain("INGEST_LLM_EXTRACT_MODELS", "mock")
    if step == "choose":
        # Filter out local models; chunk1 design forbids local Llama at choose.
        chain = _model_chain("INGEST_LLM_CHOOSE_MODELS", "mock")
        return [m for m in chain if _provider_for(m) != "litellm-local"]
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

async def _call_litellm(model: str, prompt: str, *, timeout_s: float = 60.0) -> dict:
    """One HTTP call to the litellm proxy. Returns the parsed JSON
    body of the assistant message.

    Raises Exception on any failure (caller decides whether to retry
    against a different model).
    """
    base_url = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("LITELLM_API_KEY", "")
    if not base_url:
        raise RuntimeError("LITELLM_BASE_URL not set")

    # Lazy import to keep module import cost low when only mock is used.
    import httpx

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

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

    text = body["choices"][0]["message"]["content"]
    # Models can wrap json in code fences; strip them.
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_step(
    *,
    step: str,
    prompt_template: str,
    prompt_body: str,
    prompt_hash: str,
    on_call: callable | None = None,
) -> tuple[dict, LlmCall]:
    """Run a pipeline step against the configured model fallback chain.

    Returns (parsed_response, last_attempted_call). The response is the
    parsed JSON body. The call object describes the model that produced
    it (or the last attempt if all failed).

    `on_call` callback fires once per attempt with the LlmCall populated
    enough to write an llm_calls row. Lets the orchestrator persist
    audit rows even on failure.
    """
    models = models_for_step(step)
    if not models:
        raise RuntimeError(f"no models configured for step {step!r}")

    last_call: LlmCall | None = None
    last_error: Exception | None = None

    for model in models:
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
            else:
                parsed = await _call_litellm(model, prompt_body)
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

"""One-shot LLM call to surface the real provider/network error.

Bypasses run_step's catch-and-suppress so the exception bubbles up.

Usage (inside container, with .env loaded):
    python3 /app/scripts/test_litellm_call.py gpt-5.4-mini
"""
from __future__ import annotations

import asyncio
import os
import sys
import traceback


async def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "gpt-5.4-mini"

    print(f"LITELLM_BASE_URL = {os.environ.get('LITELLM_BASE_URL', '<unset>')}")
    print(f"LITELLM_API_KEY  = {os.environ.get('LITELLM_API_KEY', '<unset>')[:20]}...")
    print(f"model            = {model}")
    print()

    try:
        from api.app.reconciliation.llm_client import _call_litellm
    except Exception:
        traceback.print_exc()
        return 1

    prompt = (
        'Return strictly the JSON object {"ok": true, "received": true}. '
        'No prose, no code fence.'
    )

    try:
        result = await _call_litellm(model, prompt, timeout_s=30.0)
        print(f"SUCCESS: {result!r}")
        return 0
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        print()
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

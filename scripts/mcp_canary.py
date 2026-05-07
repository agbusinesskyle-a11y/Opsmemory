#!/usr/bin/env python3
"""Smoke test the OpsMemory MCP server end to end.

Spawns the server as a subprocess, sends an initialize +
tools/list + tools/call(list_businesses), and asserts:
  - server returns a JSON-RPC result for each
  - tools/list contains list_tasks + get_task + list_businesses
  - list_businesses result text contains the sentinel fence on
    business names (proving prompt-injection sanitization is
    actually wired).

Env required:
  OPSMEMORY_API_BASE_URL
  OPSMEMORY_MCP_SERVICE_KEY

Exit codes:
  0  smoke passed
  1  config / startup error
  2  smoke failure (server returned wrong shape, or sanitizer
                    missing on business names)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


async def _send(proc: asyncio.subprocess.Process, msg: dict) -> None:
    line = (json.dumps(msg) + "\n").encode("utf-8")
    proc.stdin.write(line)
    await proc.stdin.drain()


async def _recv(proc: asyncio.subprocess.Process, timeout: float = 10.0) -> dict:
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    if not line:
        raise RuntimeError("MCP server closed stdout")
    return json.loads(line.decode("utf-8"))


async def main_async() -> int:
    if not os.environ.get("OPSMEMORY_API_BASE_URL"):
        print("ERROR: OPSMEMORY_API_BASE_URL must be set", file=sys.stderr)
        return 1
    if not os.environ.get("OPSMEMORY_MCP_SERVICE_KEY"):
        print("ERROR: OPSMEMORY_MCP_SERVICE_KEY must be set", file=sys.stderr)
        return 1

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "api.mcp.server",
        cwd=str(REPO_ROOT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    failures: list[str] = []
    try:
        await _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "clientInfo": {"name": "mcp_canary", "version": "0.1"}},
        })
        init = await _recv(proc)
        if "result" not in init or init["id"] != 1:
            failures.append(f"initialize bad response: {init!r}")

        await _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tlist = await _recv(proc)
        tools = (tlist.get("result") or {}).get("tools") or []
        names = [t.get("name") for t in tools]
        for required in ("list_tasks", "get_task", "list_businesses"):
            if required not in names:
                failures.append(f"tools/list missing {required!r}")

        await _send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_businesses", "arguments": {}},
        })
        bcall = await _recv(proc, timeout=20.0)
        result = bcall.get("result") or {}
        if result.get("isError"):
            failures.append(f"list_businesses returned isError: {result!r}")
        else:
            content = result.get("content") or []
            text = ""
            for c in content:
                if c.get("type") == "text":
                    text += c.get("text") or ""
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            items = (parsed or {}).get("items") or []
            if not items:
                failures.append(
                    "list_businesses returned no items — "
                    "expected at least one business in the deploy",
                )
            else:
                # Look for the sentinel fence on at least one
                # business name. Confirms sanitizer is wired.
                fenced = any(
                    isinstance(b.get("name"), str)
                    and "USER_DATA" in (b.get("name") or "")
                    for b in items
                )
                if not fenced:
                    failures.append(
                        "list_businesses items lack USER_DATA "
                        "sentinel on `name` — sanitizer not wired",
                    )
        await _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "shutdown"})
        try:
            await asyncio.wait_for(_recv(proc), timeout=5.0)
        except asyncio.TimeoutError:
            pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.terminate()
            await proc.wait()

    if failures:
        print("MCP CANARY FAIL", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 2
    print("MCP CANARY PASS")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())

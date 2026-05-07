#!/usr/bin/env python3
"""OpsMemory MCP read-only server (stdio JSON-RPC transport).

Listens on stdin/stdout for MCP JSON-RPC 2.0 requests, dispatches
tool calls to api.mcp.tools, and writes responses to stdout.

Per Codex chunk-12 plan-review: Option 1 — separate process,
stdio transport. The server runs under a service account
holding ONLY scope mcp:read.

Operator startup (typical):
  OPSMEMORY_API_BASE_URL=https://tracker.kyleconway.ai \
  OPSMEMORY_MCP_SERVICE_KEY=opsmem_live_... \
  python3 -m api.mcp.server

The MCP client (Kyle AI Assistant / Claude Desktop) configures
this script as a stdio MCP server in its config.

JSON-RPC contract (subset of MCP spec):
  initialize      handshake; returns server info + capabilities
  tools/list      lists TOOL_DEFINITIONS
  tools/call      runs a named tool with given args
  shutdown        graceful exit
  ping            heartbeat
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from api.mcp.tools import (
    McpClientConfig,
    McpToolError,
    TOOL_DEFINITIONS,
    dispatch,
    load_client_config,
)


log = logging.getLogger("opsmemory.mcp.server")


SERVER_NAME = "opsmemory-mcp"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


def _err(rid: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _ok(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


async def _handle(req: dict, cfg: McpClientConfig) -> dict | None:
    if req.get("jsonrpc") != "2.0":
        return _err(req.get("id"), -32600, "invalid jsonrpc version")
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        return _ok(rid, {"tools": TOOL_DEFINITIONS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str):
            return _err(rid, -32602, "tools/call requires `name` string")
        try:
            result = await dispatch(cfg, name, args)
        except McpToolError as exc:
            # MCP convention: tool errors come back as a result
            # with isError=true so the LLM can react. Server-
            # protocol errors use the error path.
            return _ok(rid, {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "error": {"code": exc.code, "message": exc.message},
                    }),
                }],
                "isError": True,
            })
        except Exception as exc:
            log.exception("mcp_tool_dispatch_error", extra={"tool": name})
            return _err(rid, -32000,
                        f"tool error: {type(exc).__name__}: {str(exc)[:200]}")
        return _ok(rid, {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": False,
        })
    if method == "shutdown":
        return _ok(rid, {})

    if method == "notifications/initialized":
        # Notification, not a request — no response.
        return None
    return _err(rid, -32601, f"method not found: {method!r}")


async def main_async() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        cfg = load_client_config()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    log.info("opsmemory_mcp_started",
             extra={"api_base": cfg.api_base_url, "version": SERVER_VERSION})

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    writer_transport, writer_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout,
    )
    writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, loop)

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                req = json.loads(text)
            except Exception:
                resp = _err(None, -32700, "parse error")
                writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                await writer.drain()
                continue
            if isinstance(req, list):
                # JSON-RPC batch — handle each.
                results = []
                for r in req:
                    if not isinstance(r, dict):
                        continue
                    out = await _handle(r, cfg)
                    if out is not None:
                        results.append(out)
                if results:
                    writer.write((json.dumps(results) + "\n").encode("utf-8"))
                    await writer.drain()
                continue
            if not isinstance(req, dict):
                resp = _err(None, -32600, "request must be object or array")
                writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                await writer.drain()
                continue
            out = await _handle(req, cfg)
            if out is not None:
                writer.write((json.dumps(out) + "\n").encode("utf-8"))
                await writer.drain()
    finally:
        log.info("opsmemory_mcp_stopped")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

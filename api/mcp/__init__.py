"""OpsMemory MCP read-only server (Chunk 12).

Separate stdio process. Acts as a service principal with the
narrow `mcp:read` scope. Proxies MCP tool calls to the
OpsMemory /v1 API and sanitizes all task text before returning
it to the calling LLM.

Per docs/02-architecture.md: tenant-scoped, read-only initially.
Prompt-injection defenses applied to all task text exposed via
MCP.

Modules:
  sanitize.py    Control/zero-width strip + byte cap + field
                  delimiter fencing.
  tools.py       Tool definitions: list_tasks, get_task,
                  list_businesses.
  server.py      stdio JSON-RPC transport entry point.
"""

"""OpsMemory notifications: pure digest payload builder.

Per Codex chunk-10-step3c-close STEP 4 PLAN: the builder is pure.
It takes already-fetched user + pref + tasks data and returns the
payload dict to encrypt and ship.

The DB queries (which tasks belong to this user, filtering by
include_stale / include_completed / stale_days) live in the
scheduler / sender — NOT here — so this module is trivially
testable and can't surprise an operator with hidden side effects.

Payload contract (matches sw.js _handlePush expectation):
    {
        "title":   str  (max 200 chars by sw.js trunc)
        "body":    str  (max 600 chars by sw.js trunc)
        "task_id": str | None  (deep-link target if a single task
                                 dominates the digest)
        "url":     None         (sw.js falls back to /?task=<id>)
        "items":   [{...}]      (rendered list, per-task metadata
                                 the future inbox UI can show)
        "scheduled_for": iso UTC string
        "pref_id":       str
        "channel":       str
    }
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


_MAX_BODY_TASKS = 5
_BODY_LINE_MAX = 80


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _format_due(due_iso: str | None) -> str:
    if not due_iso:
        return ""
    return f" (due {due_iso[:10]})"


def build_digest_payload(
    *,
    user: dict,
    pref: dict,
    tasks: list[dict],
    scheduled_for: datetime,
    total_count: int | None = None,
) -> dict[str, Any]:
    """Build the payload dict for one (user, pref, tasks) digest.

    Inputs are already filtered + ordered by the caller
    (scheduler.collect_tasks_for_user). This function does NOT
    re-filter. It only formats.

    user:    {id, email, display_name, timezone}
    pref:    {id, channel, schedule, settings}
              channel ∈ {'web_push', 'slack_dm', 'email_digest'}
    tasks:   ordered list. Each task: {id, summary, status,
                priority, due_iso, businesses[]}.
    scheduled_for: tz-aware UTC datetime.
    total_count: full match count BEFORE the scheduler's LIMIT
                 truncation. When provided and > len(tasks), the
                 title and body reflect the true total and the
                 'and N more' overflow line uses the truncation
                 delta honestly. Codex chunk-10-step4-close (d).

    Returns a payload dict per the module docstring contract.
    """
    if scheduled_for.tzinfo is None:
        raise ValueError("scheduled_for must be tz-aware")
    rendered = len(tasks)
    n = total_count if total_count is not None and total_count >= rendered else rendered
    channel = pref["channel"]
    pref_id = pref["id"]

    if n == 0:
        title = "OpsMemory: nothing pressing"
        body = "No open tasks need your attention right now."
        task_id = None
    else:
        title = f"OpsMemory: {n} task{'s' if n != 1 else ''} for you"
        # Body shows up to _MAX_BODY_TASKS lines, each truncated.
        body_lines: list[str] = []
        for t in tasks[:_MAX_BODY_TASKS]:
            line = "• " + _truncate(t.get("summary", ""), _BODY_LINE_MAX)
            line += _format_due(t.get("due_iso"))
            body_lines.append(line)
        # Overflow line is honest about the FULL total (n) minus
        # what the body has rendered, not just the slice we got.
        if n > _MAX_BODY_TASKS:
            body_lines.append(f"…and {n - _MAX_BODY_TASKS} more.")
        body = "\n".join(body_lines)
        # When there's exactly one task overall, surface its id so
        # the notification click deep-links straight to it.
        task_id = tasks[0]["id"] if n == 1 and rendered == 1 else None

    items = [
        {
            "id": t["id"],
            "summary": t.get("summary"),
            "status": t.get("status"),
            "priority": t.get("priority"),
            "due_iso": t.get("due_iso"),
            "businesses": t.get("businesses") or [],
        }
        for t in tasks
    ]

    return {
        "title": title,
        "body": body,
        "task_id": task_id,
        "url": None,
        "items": items,
        "scheduled_for": scheduled_for.astimezone(timezone.utc).isoformat(),
        "pref_id": pref_id,
        "channel": channel,
    }

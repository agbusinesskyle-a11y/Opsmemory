"""OpsMemory notifications: weekly Gmail digest builder.

Per-BUSINESS digest. Mirrors the chunk-10 digest.py shape: pure
builder, no DB, no network. The runner fetches tasks + allowlist
and hands them in; this module just renders the email.

Output payload contract (matches what the n8n bridge expects):
  {
    subject:   'OpsMemory weekly: <biz name> for week of Mon DD',
    html_body: rendered HTML email body
    text_body: plain-text fallback (Gmail multipart)
    to:        list of strings
    cc:        list of strings
    bcc:       list of strings
    counts:    {open, completed, stale}
    items:     {open: [...], completed: [...], stale: [...]}
    week_start_iso, week_end_iso
    business_slug, business_name
    generated_at: iso UTC
  }

Per docs/01-design.md: drafts only, recipient allowlist, never
auto-sends. The builder enforces the allowlist by simply not
including any addresses outside what the caller passed in.
"""

from __future__ import annotations

import html
from datetime import date, datetime, timezone
from typing import Any, Iterable


_PRIORITY_ORDER = {"p1": 0, "p2": 1, "p3": 2, None: 3, "": 3}


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _format_due(due_iso: str | None) -> str:
    if not due_iso:
        return ""
    return f" — due {due_iso[:10]}"


def _split_recipients(recipients: Iterable[dict]) -> tuple[list[str], list[str], list[str]]:
    """Group allowlist rows by role into (to, cc, bcc) lists.

    `recipients` is a list of {recipient_email, role} dicts (or
    asyncpg rows that quack like dicts). Empty groups stay empty —
    the builder doesn't synthesize defaults; the caller controls
    everything.
    """
    to: list[str] = []
    cc: list[str] = []
    bcc: list[str] = []
    for r in recipients:
        email = (r.get("recipient_email") or "").strip()
        role = (r.get("role") or "to").lower()
        if not email:
            continue
        if role == "cc":
            cc.append(email)
        elif role == "bcc":
            bcc.append(email)
        else:
            to.append(email)
    return to, cc, bcc


def _format_date_human(d: date) -> str:
    """'May 6' style, no year (the subject's week-of context makes
    it unambiguous; saves header bytes)."""
    return d.strftime("%b ").lstrip() + str(d.day)


def _render_task_html(task: dict) -> str:
    """One task as an <li> with summary, owner, due, priority. Safe
    against HTML injection in the summary."""
    summary = html.escape(_truncate(task.get("summary") or "", 200))
    due = ""
    if task.get("due_iso"):
        due = f' <span style="color:#888">— due {html.escape(task["due_iso"][:10])}</span>'
    owner = ""
    if task.get("owner_display_name"):
        owner = f' <span style="color:#888">[{html.escape(task["owner_display_name"])}]</span>'
    pri = ""
    if task.get("priority"):
        pri = f' <span style="font-size:11px;color:#666;border:1px solid #ccc;padding:1px 4px;border-radius:3px;">{html.escape(task["priority"].upper())}</span>'
    return f"<li>{summary}{owner}{due}{pri}</li>"


def _render_section_html(label: str, tasks: list[dict]) -> str:
    if not tasks:
        return f'<h3 style="margin:16px 0 4px 0;">{html.escape(label)} (0)</h3><p style="color:#888;margin:0;">No items.</p>'
    items_html = "\n".join(_render_task_html(t) for t in tasks)
    return (
        f'<h3 style="margin:16px 0 4px 0;">{html.escape(label)} ({len(tasks)})</h3>'
        f'<ul style="margin:0;padding-left:20px;">{items_html}</ul>'
    )


def _render_section_text(label: str, tasks: list[dict]) -> str:
    if not tasks:
        return f"{label} (0):\n  (no items)"
    lines = [f"{label} ({len(tasks)}):"]
    for t in tasks:
        summary = _truncate(t.get("summary") or "", 200)
        owner = f" [{t['owner_display_name']}]" if t.get("owner_display_name") else ""
        due = _format_due(t.get("due_iso"))
        pri = f" [{t['priority'].upper()}]" if t.get("priority") else ""
        lines.append(f"  • {summary}{owner}{due}{pri}")
    return "\n".join(lines)


def _sort_tasks(tasks: list[dict]) -> list[dict]:
    """Stable sort: due_at NULLS LAST, then priority p1 < p2 < p3
    < null, then summary alpha. Matches the PWA ordering.
    """
    def key(t: dict):
        due = t.get("due_iso") or "9999-12-31"
        pri = _PRIORITY_ORDER.get(t.get("priority"), 9)
        return (due, pri, (t.get("summary") or "").lower())
    return sorted(tasks, key=key)


def build_weekly_digest_payload(
    *,
    business: dict,
    tasks_open: list[dict],
    tasks_completed_this_week: list[dict],
    tasks_stale: list[dict],
    recipients: list[dict],
    week_start: date,
    week_end: date,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Render a per-business weekly digest into an email payload.

    business: {id, slug, name}
    tasks_*:  list of dicts with at minimum {id, summary, status,
               priority, due_iso, owner_display_name?}
    recipients: list of {recipient_email, role} from the allowlist
    week_start / week_end: date objects in the business's local tz
    generated_at: tz-aware UTC datetime (defaults to now)

    Returns the payload dict described in the module docstring.
    Tasks are sorted; HTML/text bodies are rendered; recipients
    are split by role. Empty businesses get a "No activity"
    template, not a blank email.
    """
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        raise ValueError("generated_at must be tz-aware")

    open_sorted = _sort_tasks(tasks_open or [])
    completed_sorted = _sort_tasks(tasks_completed_this_week or [])
    stale_sorted = _sort_tasks(tasks_stale or [])

    to, cc, bcc = _split_recipients(recipients or [])

    biz_name = business.get("name") or business.get("slug") or "(unknown)"
    biz_slug = business.get("slug") or ""
    week_label = f"week of {_format_date_human(week_start)}"
    counts = {
        "open": len(open_sorted),
        "completed": len(completed_sorted),
        "stale": len(stale_sorted),
    }
    total = counts["open"] + counts["completed"] + counts["stale"]

    subject = f"OpsMemory weekly: {biz_name} for {week_label}"

    if total == 0:
        text_body = (
            f"OpsMemory weekly digest for {biz_name}\n"
            f"{week_label} (week of {week_start.isoformat()} to "
            f"{week_end.isoformat()})\n\n"
            f"No activity this week.\n\n"
            f"Generated {generated_at.isoformat()}.\n"
            f"This is a draft — review and send manually if appropriate."
        )
        html_body = (
            f'<div style="font-family:system-ui,Segoe UI,Helvetica,Arial,sans-serif;'
            f'color:#222;font-size:14px;line-height:1.5;">'
            f'<h2 style="margin:0 0 4px 0;">OpsMemory weekly: {html.escape(biz_name)}</h2>'
            f'<p style="color:#888;margin:0 0 16px 0;">{html.escape(week_label)} '
            f'({week_start.isoformat()} to {week_end.isoformat()})</p>'
            f'<p style="color:#888;">No activity this week.</p>'
            f'<hr style="margin:20px 0;border:none;border-top:1px solid #eee;">'
            f'<p style="color:#aaa;font-size:11px;">'
            f'Generated {html.escape(generated_at.isoformat())}. '
            f'This is a draft — review and send manually if appropriate.'
            f'</p></div>'
        )
    else:
        text_body = (
            f"OpsMemory weekly digest for {biz_name}\n"
            f"{week_label} (week of {week_start.isoformat()} to "
            f"{week_end.isoformat()})\n\n"
            f"{_render_section_text('Open', open_sorted)}\n\n"
            f"{_render_section_text('Completed this week', completed_sorted)}\n\n"
            f"{_render_section_text('Stale (open, past due)', stale_sorted)}\n\n"
            f"Generated {generated_at.isoformat()}.\n"
            f"This is a draft — review and send manually if appropriate."
        )
        html_body = (
            f'<div style="font-family:system-ui,Segoe UI,Helvetica,Arial,sans-serif;'
            f'color:#222;font-size:14px;line-height:1.5;">'
            f'<h2 style="margin:0 0 4px 0;">OpsMemory weekly: {html.escape(biz_name)}</h2>'
            f'<p style="color:#888;margin:0 0 16px 0;">{html.escape(week_label)} '
            f'({week_start.isoformat()} to {week_end.isoformat()})</p>'
            f'{_render_section_html("Open", open_sorted)}'
            f'{_render_section_html("Completed this week", completed_sorted)}'
            f'{_render_section_html("Stale (open, past due)", stale_sorted)}'
            f'<hr style="margin:20px 0;border:none;border-top:1px solid #eee;">'
            f'<p style="color:#aaa;font-size:11px;">'
            f'Generated {html.escape(generated_at.isoformat())}. '
            f'This is a draft — review and send manually if appropriate.'
            f'</p></div>'
        )

    return {
        "subject": subject,
        "html_body": html_body,
        "text_body": text_body,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "counts": counts,
        "items": {
            "open": open_sorted,
            "completed": completed_sorted,
            "stale": stale_sorted,
        },
        "week_start_iso": week_start.isoformat(),
        "week_end_iso": week_end.isoformat(),
        "business_slug": biz_slug,
        "business_name": biz_name,
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
    }

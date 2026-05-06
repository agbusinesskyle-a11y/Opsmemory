"""OpsMemory v1 Slack /tasks slash-command bridge.

Endpoint (Chunk 8 step 1 — smallest first commit):

  POST /v1/slack/tasks                  service-auth, slack:query scope.

Request shape (n8n payload contract):

  n8n MUST verify the Slack signing secret, normalize the slash command
  payload, and POST this body. OpsMemory NEVER sees the raw Slack
  request — it trusts n8n's verification.

  body = {
    "team_id":      "T...",      # Slack workspace
    "user_id":      "U...",      # Slack user invoking the command
    "channel_id":   "C..." | "G..." | "D...",   # optional, for context
    "channel_name": "redhot-ops",                 # optional, for response
    "command":      "/tasks",                     # the literal slash command
    "text":         "<@U03ABC123>",               # everything after the command
    "response_url": "https://hooks.slack.com/...", # optional, for delayed responses
    "trigger_id":   "...",                         # Slack trigger
  }

Response shape: Slack Block Kit JSON with response_type=ephemeral.
n8n forwards this to Slack as the slash-command response.

Slash-command grammar (this commit ships only the first; stale +
category land in step 2/3):
  /tasks <owner>          # who's working on what (this commit)
  /tasks stale            # not touched in N days (step 2)
  /tasks <category>       # filter by tasks.category (step 3)
  /tasks                  # help text

Auth + identity model (per Codex chunk-7-close STEP 8 PLAN):
  1. Service principal carrying SCOPE_SLACK_QUERY admits the n8n forward.
  2. The (team_id, user_id) tuple is mapped via
     user_identities(provider='slack',
                     provider_subject='{team_id}:{slack_user_id}')
     to a canonical OpsMemory user.
  3. The endpoint applies THAT user's visible_business_ids to the
     query — the service principal itself doesn't see all tasks,
     it just authenticates the bridge.
  4. Slack users without a mapped identity get an ephemeral
     "Unknown user" response (do NOT leak data).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .auth import Principal, require_principal
from .authz import SCOPE_SLACK_QUERY, require_scope

log = logging.getLogger("opsmemory.v1_slack_tasks")

router = APIRouter(prefix="/v1/slack")


# ---------------------------------------------------------------------------
# Pydantic body
# ---------------------------------------------------------------------------

# Reuse the field-specific Slack regexes from the chunk-5 ingest endpoint.
_SLACK_TEAM_ID_PATTERN = r"^T[A-Z0-9]{2,30}$"
_SLACK_CHANNEL_ID_PATTERN = r"^[CGD][A-Z0-9]{2,30}$"
_SLACK_USER_ID_PATTERN = r"^[UW][A-Z0-9]{2,30}$"


class SlackTasksRequest(BaseModel):
    model_config = {"extra": "forbid"}

    team_id: str = Field(..., min_length=3, max_length=32, pattern=_SLACK_TEAM_ID_PATTERN)
    user_id: str = Field(..., min_length=3, max_length=32, pattern=_SLACK_USER_ID_PATTERN)
    channel_id: str | None = Field(default=None, max_length=32, pattern=_SLACK_CHANNEL_ID_PATTERN)
    channel_name: str | None = Field(default=None, max_length=128)
    command: str = Field(..., min_length=1, max_length=64)
    text: str = Field(default="", max_length=2048)
    response_url: str | None = Field(default=None, max_length=512)
    trigger_id: str | None = Field(default=None, max_length=128)


# ---------------------------------------------------------------------------
# Block Kit helpers
# ---------------------------------------------------------------------------

def _ephemeral(blocks: list[dict]) -> dict:
    return {"response_type": "ephemeral", "blocks": blocks}


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _help_blocks() -> dict:
    days = _stale_days()
    return _ephemeral([
        _section(
            "*OpsMemory tasks*\n"
            "• `/tasks <owner>` — list open tasks assigned to that owner.\n"
            "  Pass a Slack mention (`/tasks @kyle`) or a name substring "
            "(`/tasks Joanna`).\n"
            f"• `/tasks stale` — open tasks not touched in {days}+ days.\n"
            "• `/tasks <category>` — _coming soon (chunk 8 step 3)_."
        ),
    ])


def _format_task_line(t: Any) -> str:
    """One bullet per task. Slack mrkdwn."""
    parts = [f"• *{_md_escape(t['summary'])}*"]
    if t.get("due_at"):
        parts.append(f"_due {_md_escape(str(t['due_at'])[:10])}_")
    bizs = t.get("businesses") or []
    if bizs:
        parts.append("[" + ", ".join(_md_escape(b) for b in bizs) + "]")
    if t.get("dependency_text"):
        parts.append(f"⏸ {_md_escape(t['dependency_text'])}")
    return " ".join(parts)


def _md_escape(s: str) -> str:
    """Escape Slack mrkdwn metachars (lightweight; n8n strips
    markdown injection at the edge by virtue of structured forwarding)."""
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

async def _resolve_caller(conn, team_id: str, slack_user_id: str) -> dict | None:
    """Map a Slack (team, user) to a canonical OpsMemory user dict
    matching the Principal shape (id, role, businesses[]).
    """
    subject = f"{team_id}:{slack_user_id}"
    row = await conn.fetchrow(
        """
        SELECT u.id::text          AS id,
               u.email::text       AS email,
               u.display_name      AS display_name,
               u.role::text        AS role,
               u.status::text      AS status
        FROM user_identities ui
        JOIN users u ON u.id = ui.user_id
        WHERE ui.provider = 'slack'
          AND ui.provider_subject = $1
        """,
        subject,
    )
    if not row or row["status"] != "active":
        return None
    # Match auth.py's canonical membership filter exactly: a Slack
    # caller with a disabled business membership (bm.status != 'active')
    # or a soft-deleted business must NOT see those tasks (Codex
    # chunk-8-step1 blocker).
    biz_rows = await conn.fetch(
        """
        SELECT b.id::text   AS id,
               b.slug::text AS slug,
               b.name       AS name,
               bm.role::text AS role
        FROM business_memberships bm
        JOIN businesses b ON b.id = bm.business_id
        WHERE bm.user_id = $1::uuid
          AND bm.status = 'active'
          AND b.deletion_state = 'active'
        """,
        row["id"],
    )
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "businesses": [dict(b) for b in biz_rows],
    }


def _visible_business_ids(caller: dict) -> list[str] | None:
    """Mirror authz.visible_business_ids semantics for our caller dict.
    None = admin (unrestricted); list = owner-scoped business ids.
    """
    if caller["role"] == "admin":
        return None
    return [b["id"] for b in caller["businesses"]]


# ---------------------------------------------------------------------------
# /tasks <owner>
# ---------------------------------------------------------------------------

# Slack mention:  <@U03ABC123>  or  <@U03ABC123|kyle>
_SLACK_MENTION = re.compile(r"<@([UW][A-Z0-9]{2,30})(?:\|[^>]*)?>")


async def _resolve_owner_arg(
    conn,
    team_id: str,
    arg: str,
    caller_visible_biz: list[str] | None,
) -> tuple[dict | None, str | None]:
    """Resolve the <owner> argument to a single OpsMemory user.

    Returns (user_dict | None, error_msg | None). Exactly one of the
    pair is non-null on the happy path.

    Resolution order:
      1. Slack mention syntax -> user_identities(provider='slack').
      2. Substring match on users.display_name (case-insensitive),
         scoped to ACTIVE users.

    Caller's visibility doesn't gate the owner lookup itself — the
    task query is the final authz gate. We resolve to a canonical
    user freely; the join with task_assignees + task_businesses
    intersected with caller_visible_biz produces the safe result.
    """
    arg = (arg or "").strip()
    if not arg:
        return (None, "Specify an owner. Example: `/tasks @kyle` or `/tasks Joanna`.")

    # 1. Slack mention.
    m = _SLACK_MENTION.search(arg)
    if m:
        slack_uid = m.group(1)
        subject = f"{team_id}:{slack_uid}"
        row = await conn.fetchrow(
            """
            SELECT u.id::text       AS id,
                   u.display_name   AS display_name
            FROM user_identities ui
            JOIN users u ON u.id = ui.user_id
            WHERE ui.provider = 'slack'
              AND ui.provider_subject = $1
              AND u.status = 'active'
            """,
            subject,
        )
        if not row:
            return (None, f"Slack user `<@{slack_uid}>` is not mapped to "
                          "an OpsMemory user. Ask an admin to add the mapping.")
        return ({"id": row["id"], "display_name": row["display_name"]}, None)

    # 2. Display-name substring (case-insensitive).
    rows = await conn.fetch(
        """
        SELECT id::text       AS id,
               display_name   AS display_name
        FROM users
        WHERE status = 'active'
          AND display_name ILIKE '%' || $1 || '%'
        ORDER BY display_name
        LIMIT 5
        """,
        arg,
    )
    if not rows:
        return (None, f"No active OpsMemory user matches `{_md_escape(arg)}`.")
    if len(rows) > 1:
        names = ", ".join(f"`{r['display_name']}`" for r in rows)
        return (None, f"Multiple matches for `{_md_escape(arg)}`: {names}. "
                      "Be more specific.")
    return ({"id": rows[0]["id"], "display_name": rows[0]["display_name"]}, None)


def _stale_days() -> int:
    """SLACK_TASKS_STALE_DAYS env var. Default 14 per Codex chunk-8-step1
    STEP 2 PLAN. Bounded to 1..3650 so a typo can't run open queries
    against tasks that haven't been touched since the year 4754.
    """
    raw = os.environ.get("SLACK_TASKS_STALE_DAYS", "").strip()
    if not raw:
        return 14
    try:
        n = int(raw)
    except ValueError:
        return 14
    if n < 1:
        return 1
    if n > 3650:
        return 3650
    return n


async def _query_stale_tasks(
    conn,
    caller_visible_biz: list[str] | None,
    *,
    days: int,
    limit: int = 25,
) -> list[dict]:
    """Open active tasks not touched in `days` days, scoped to caller's
    visible businesses. Oldest first (Codex: stable ordering on id
    breaks ties). Owner with empty visible list returns no tasks.
    """
    where = [
        "t.status = 'open'",
        "t.deletion_state = 'active'",
        "t.last_activity_at < now() - make_interval(days => $1::int)",
    ]
    params: list[Any] = [days]
    if caller_visible_biz is not None:
        # Owner with no memberships gets no tasks (consistent with
        # the rest of the codebase).
        if not caller_visible_biz:
            return []
        params.append(caller_visible_biz)
        where.append(
            f"EXISTS (SELECT 1 FROM task_businesses tb "
            f"        WHERE tb.task_id = t.id "
            f"          AND tb.business_id::text = ANY(${len(params)}::text[]))"
        )
    sql = f"""
        SELECT t.id::text                AS id,
               t.summary                 AS summary,
               t.due_at::text            AS due_at,
               t.dependency_text         AS dependency_text,
               t.last_activity_at::text  AS last_activity_at,
               array_agg(DISTINCT b.slug::text) AS businesses
        FROM tasks t
        LEFT JOIN task_businesses tb2 ON tb2.task_id = t.id
        LEFT JOIN businesses b        ON b.id = tb2.business_id
        WHERE {' AND '.join(where)}
        GROUP BY t.id
        ORDER BY t.last_activity_at ASC, t.id
        LIMIT {limit}
    """
    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


def _format_stale_line(t: Any) -> str:
    """Stale list line — same shape as owner-tasks but leads with
    'last touched' instead of due."""
    parts = [f"• *{_md_escape(t['summary'])}*"]
    if t.get("last_activity_at"):
        parts.append(f"_last touched {_md_escape(str(t['last_activity_at'])[:10])}_")
    bizs = t.get("businesses") or []
    if bizs:
        parts.append("[" + ", ".join(_md_escape(b) for b in bizs) + "]")
    if t.get("dependency_text"):
        parts.append(f"⏸ {_md_escape(t['dependency_text'])}")
    return " ".join(parts)


async def _query_owner_tasks(
    conn,
    owner_user_id: str,
    caller_visible_biz: list[str] | None,
    *,
    limit: int = 25,
) -> list[dict]:
    """Open active tasks assigned to the owner, scoped to caller's
    visible businesses.
    """
    where = [
        "ta.user_id = $1::uuid",
        "ta.role = 'assignee'",
        "t.status = 'open'",
        "t.deletion_state = 'active'",
    ]
    params: list[Any] = [owner_user_id]
    if caller_visible_biz is not None:
        params.append(caller_visible_biz)
        where.append(
            f"EXISTS (SELECT 1 FROM task_businesses tb "
            f"        WHERE tb.task_id = t.id "
            f"          AND tb.business_id::text = ANY(${len(params)}::text[]))"
        )
    sql = f"""
        SELECT t.id::text                AS id,
               t.summary                 AS summary,
               t.due_at::text            AS due_at,
               t.dependency_text         AS dependency_text,
               t.last_activity_at::text  AS last_activity_at,
               array_agg(DISTINCT b.slug::text) AS businesses
        FROM tasks t
        JOIN task_assignees ta ON ta.task_id = t.id
        LEFT JOIN task_businesses tb2 ON tb2.task_id = t.id
        LEFT JOIN businesses b        ON b.id = tb2.business_id
        WHERE {' AND '.join(where)}
        GROUP BY t.id
        ORDER BY t.due_at NULLS LAST, t.last_activity_at DESC
        LIMIT {limit}
    """
    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/tasks")
async def slack_tasks(
    body: SlackTasksRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Service-only Slack /tasks dispatcher.

    Returns Slack Block Kit JSON. Always ephemeral so private task
    data never leaks to a public channel.

    A failure mode that returns 200 + an error block is preferred over
    raising HTTP errors, because Slack treats non-200 responses as
    "didn't work" without surfacing the message body. Operational
    errors (DB unreachable, etc.) DO raise so n8n can retry.
    """
    # n8n holds the bridge service account; admin users shouldn't
    # call this directly.
    if principal.principal_type != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="slack /tasks requires a service principal",
        )
    require_scope(principal, SCOPE_SLACK_QUERY)

    pool = request.app.state.db
    async with pool.acquire() as conn:
        caller = await _resolve_caller(conn, body.team_id, body.user_id)
        if not caller:
            log.info("slack_tasks_unknown_caller", extra={
                "team_id": body.team_id, "slack_user_id": body.user_id,
            })
            return _ephemeral([_section(
                "Your Slack account isn't linked to an OpsMemory user. "
                "Ask an admin to add the mapping."
            )])

        # Sub-command dispatch. Trim and split on whitespace so leading
        # spaces don't confuse the parser.
        text = (body.text or "").strip()
        if not text:
            return _help_blocks()

        # First token = sub-command (or owner argument).
        head = text.split(maxsplit=1)[0].lower()
        caller_visible_biz = _visible_business_ids(caller)

        if head == "stale":
            days = _stale_days()
            tasks = await _query_stale_tasks(
                conn, caller_visible_biz, days=days,
            )
            if not tasks:
                return _ephemeral([_section(
                    f"No open tasks have been untouched for "
                    f"{days}+ day{'s' if days != 1 else ''} in your "
                    "visible businesses."
                )])
            header = (
                f"*Stale tasks* — {len(tasks)} not touched in "
                f"{days}+ day{'s' if days != 1 else ''}, oldest first:"
            )
            body_block = "\n".join(_format_stale_line(t) for t in tasks)
            log.info("slack_tasks_stale_query", extra={
                "team_id": body.team_id,
                "caller_id": caller["id"],
                "days": days,
                "task_count": len(tasks),
            })
            return _ephemeral([
                _section(header),
                _section(body_block),
            ])

        if head in ("help", "?"):
            return _help_blocks()

        # Default: treat the entire text as the owner argument.
        owner, err = await _resolve_owner_arg(
            conn, body.team_id, text, caller_visible_biz,
        )
        if err:
            return _ephemeral([_section(err)])
        tasks = await _query_owner_tasks(
            conn, owner["id"], caller_visible_biz,
        )
        if not tasks:
            return _ephemeral([_section(
                f"*{_md_escape(owner['display_name'])}* has no open tasks "
                "in your visible businesses."
            )])
        header = (
            f"*{_md_escape(owner['display_name'])}* — "
            f"{len(tasks)} open task{'s' if len(tasks) != 1 else ''}:"
        )
        body_block = "\n".join(_format_task_line(t) for t in tasks)
        log.info("slack_tasks_owner_query", extra={
            "team_id": body.team_id,
            "caller_id": caller["id"],
            "owner_id": owner["id"],
            "task_count": len(tasks),
        })
        return _ephemeral([
            _section(header),
            _section(body_block),
        ])

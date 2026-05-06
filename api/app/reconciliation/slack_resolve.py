"""Step 2.5 (Slack only): deterministic channel + mention resolver.

Runs between normalize and retrieve for source='slack_message' events.
Two responsibilities:

1. Channel -> business resolution.
   When the extract step didn't surface an explicit business name in
   the message text, look up slack_channel_mappings(team_id, channel_id)
   and fill candidate.businesses with the mapped business slug.

2. Slack user mention -> canonical user resolution.
   The extract step emits owner_slack_user_ids = [list of '<@U...>' or
   bare 'U...' ids found in the message]. We map each to a canonical
   OpsMemory user via user_identities(provider='slack',
   provider_subject='{team_id}:{slack_user_id}'). The first resolved
   match wins for owner_display + owner_user_id.

Why deterministic and not an LLM step:
  Channel-name heuristics in the LLM prompt were too nondeterministic
  (Codex chunk-5-step2 review). Mapping is an operator decision that
  needs to be auditable and stable across runs.

Why module-separate from normalize.py:
  normalize.py is source-agnostic (owner aliases by display name,
  business slug validation, date parsing). slack_resolve hits the DB
  and is Slack-specific. Keeping them apart makes future per-source
  resolvers (email-from-domain, doc-from-folder) compose cleanly.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("opsmemory.reconciliation.slack_resolve")

# Match either bare 'U123ABC' or '<@U123ABC>' (Slack mention syntax).
_SLACK_USER_REF = re.compile(r"<@([UW][A-Z0-9]{2,30})>|^([UW][A-Z0-9]{2,30})$")


def _strip_user_id(raw: str | None) -> str | None:
    """Pull the bare U... id out of a mention or return as-is."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    m = _SLACK_USER_REF.match(s)
    if not m:
        return None
    return m.group(1) or m.group(2)


async def _resolve_channel_business(
    conn,
    team_id: str | None,
    channel_id: str | None,
) -> str | None:
    """Return the business slug for a (team_id, channel_id) mapping,
    or None when no active mapping exists.

    Reads slack_channel_mappings JOIN businesses. Filters on
    status='active' (paused/archived mappings don't resolve).
    """
    if not team_id or not channel_id:
        return None
    row = await conn.fetchrow(
        """
        SELECT b.slug::text AS slug
        FROM slack_channel_mappings m
        JOIN businesses b ON b.id = m.business_id
        WHERE m.team_id = $1
          AND m.channel_id = $2
          AND m.status = 'active'
          AND b.deletion_state = 'active'
        """,
        team_id,
        channel_id,
    )
    return row["slug"] if row else None


async def _resolve_user_mention(
    conn,
    team_id: str | None,
    slack_user_id: str | None,
) -> dict | None:
    """Return {user_id, display_name, email} for a Slack mention, or
    None when the mention can't be tied to a canonical user.

    provider_subject format: '{team_id}:{slack_user_id}'. Operator
    seeds these rows when adding a Slack workspace.
    """
    if not team_id or not slack_user_id:
        return None
    subject = f"{team_id}:{slack_user_id}"
    row = await conn.fetchrow(
        """
        SELECT u.id::text AS user_id,
               u.display_name AS display_name,
               u.email::text AS email
        FROM user_identities ui
        JOIN users u ON u.id = ui.user_id
        WHERE ui.provider = 'slack'
          AND ui.provider_subject = $1
          AND u.status = 'active'
        """,
        subject,
    )
    if not row:
        return None
    return {
        "user_id": row["user_id"],
        "display_name": row["display_name"],
        "email": row["email"],
    }


async def resolve_slack_context(
    conn,
    candidate: dict,
    *,
    source_metadata: dict | None,
) -> dict:
    """Mutate the candidate in-place with Slack-resolved fields.

    Reads:
      - source_metadata: ingest_events.source_metadata jsonb. Provides
        team_id and channel_id for channel->business lookup.
      - candidate['owner_slack_user_ids']: list passed through by
        normalize_candidate from the extract LLM output.

    Writes:
      - candidate['businesses'] gets the channel-mapped business slug
        IF not already populated by an explicit text hint.
      - candidate['owner_user_id'] / candidate['owner_display'] /
        candidate['owner_email'] get the first resolvable Slack mention
        (fallback to whatever normalize already set).

    Returns the (mutated) candidate for caller convenience.
    """
    md = source_metadata or {}
    team_id = md.get("team_id")
    channel_id = md.get("channel_id")

    # ---- Channel -> business ----
    if not candidate.get("businesses"):
        slug = await _resolve_channel_business(conn, team_id, channel_id)
        if slug:
            candidate["businesses"] = [slug]
            candidate.setdefault("business_resolution_source", "channel_mapping")
            log.info("slack_channel_business_resolved", extra={
                "team_id": team_id, "channel_id": channel_id, "business": slug,
            })

    # ---- Mentions -> canonical user ----
    raw_user_ids: list[str] = []
    extracted_ids = candidate.get("owner_slack_user_ids") or []
    if isinstance(extracted_ids, list):
        for entry in extracted_ids:
            resolved = _strip_user_id(entry)
            if resolved:
                raw_user_ids.append(resolved)
    # Fall back to source_metadata.user_id (the message poster) when
    # the LLM didn't surface any explicit mentions. Captures the
    # "Karen needs the permits" case — Karen unresolvable, but at
    # least records the poster as a candidate owner.
    if not raw_user_ids:
        poster = md.get("user_id")
        if poster:
            stripped = _strip_user_id(poster)
            if stripped:
                raw_user_ids.append(stripped)

    for slack_uid in raw_user_ids:
        resolved = await _resolve_user_mention(conn, team_id, slack_uid)
        if resolved:
            # First match wins. normalize.py may have set owner_display
            # from a name-only hint; the slack-mention-resolved value
            # is more specific and overrides.
            candidate["owner_user_id"] = resolved["user_id"]
            candidate["owner_display"] = resolved["display_name"]
            candidate["owner_email"] = resolved["email"]
            candidate.setdefault("owner_resolution_source", "slack_mention")
            log.info("slack_mention_resolved", extra={
                "team_id": team_id, "slack_user_id": slack_uid,
                "user_id": resolved["user_id"],
            })
            break

    return candidate

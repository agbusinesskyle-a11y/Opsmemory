"""Step 2: normalize candidate fields.

Deterministic. Resolves owner aliases to canonical user_ids, business
hints to business slugs (validated), date hints to ISO timestamps when
parseable. Adds a stable dedup_key for use by the retrieve step.

Date resolution anchors to the message's source timestamp when the
caller supplies one (`now=` kwarg). For Slack this is the Slack ts;
for meeting_recap it falls back to ingest received_at. The 17:00
"end-of-business" cutoff is in **Phoenix local time** (Kyle's ops tz,
year-round UTC-7), not UTC — emitting 17:00 UTC would land tasks at
10am Phoenix, which is wrong.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


PHOENIX = ZoneInfo("America/Phoenix")

# These match the seed in 0001_initial.sql + business_memberships.
KNOWN_BUSINESSES = {"redhot", "borderline"}

# Owner aliases. Mapping is best-effort; the retrieve step will
# fall back to a UUID-only path if no alias matches. The full canonical
# user records come from the users table — this dict is just for the
# extract LLM's free-form name strings.
OWNER_ALIASES_DISPLAY = {
    "kyle": "Kyle Conway",
    "kyle conway": "Kyle Conway",
    "joanna": "Joanna Noriega",
    "joanna noriega": "Joanna Noriega",
    "joanna mori": "Joanna Noriega",  # legacy yahoo address spelling
    "caleb": "Caleb Noriega",
    "caleb noriega": "Caleb Noriega",
    "sarah": "Sarah Conway",
    "sarah conway": "Sarah Conway",
}

_REL_DATE_PATTERNS = [
    (re.compile(r"^today$", re.I), 0),
    (re.compile(r"^tomorrow$", re.I), 1),
    (re.compile(r"^next\s+(monday|mon)$", re.I), None),  # day-of-week handled below
    (re.compile(r"^next\s+(tuesday|tue|tues)$", re.I), None),
    (re.compile(r"^next\s+(wednesday|wed)$", re.I), None),
    (re.compile(r"^next\s+(thursday|thu|thurs)$", re.I), None),
    (re.compile(r"^next\s+(friday|fri)$", re.I), None),
    (re.compile(r"^next\s+(saturday|sat)$", re.I), None),
    (re.compile(r"^next\s+(sunday|sun)$", re.I), None),
]

_DOW = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def _resolve_owner_display(hint: str | None) -> str | None:
    if not hint:
        return None
    return OWNER_ALIASES_DISPLAY.get(hint.strip().lower())


def _resolve_businesses(hints: list[str] | None) -> list[str]:
    if not hints:
        return []
    out: list[str] = []
    for h in hints:
        slug = h.strip().lower()
        if slug in KNOWN_BUSINESSES:
            out.append(slug)
    return list(dict.fromkeys(out))  # dedupe, preserve order


def _at_5pm_phoenix_to_utc(local_day: datetime) -> str:
    """Return the ISO 8601 UTC string for 17:00 Phoenix on the given day.

    Caller passes a tz-aware datetime expressed in any zone — we
    re-express it in Phoenix, replace H/M/S to 17:00 local, then
    convert back to UTC. End-of-business in Phoenix is UTC-7
    year-round (no DST) so this is unambiguous.
    """
    local = local_day.astimezone(PHOENIX).replace(
        hour=17, minute=0, second=0, microsecond=0
    )
    return local.astimezone(timezone.utc).isoformat()


def _resolve_due(hint: str | None, *, now: datetime | None = None) -> str | None:
    """Parse the LLM's due_hint into an ISO 8601 UTC timestamp.

    Returns None when the hint is unparseable (operator fills in at
    review time). All relative-date arithmetic is done in Phoenix local
    so "Friday by EOD" resolves to Friday 17:00 Phoenix, not 17:00 UTC.
    """
    if not hint:
        return None
    s = hint.strip()
    if not s:
        return None
    base = (now or datetime.now(timezone.utc))
    # Force tz awareness so .astimezone() works downstream.
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    base_local = base.astimezone(PHOENIX)

    # Try strict ISO first. Trust whatever tz the LLM emitted.
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass

    # Today / tomorrow.
    if re.fullmatch(r"today", s, flags=re.I):
        return _at_5pm_phoenix_to_utc(base_local)
    if re.fullmatch(r"tomorrow", s, flags=re.I):
        return _at_5pm_phoenix_to_utc(base_local + timedelta(days=1))

    # "next <day-of-week>" — explicit-next-week phrasing.
    m = re.fullmatch(r"next\s+(\w+)", s, flags=re.I)
    if m:
        target_dow = _DOW.get(m.group(1).lower())
        if target_dow is not None:
            current = base_local.weekday()
            delta = (target_dow - current) % 7 or 7
            return _at_5pm_phoenix_to_utc(base_local + timedelta(days=delta))

    # Bare day-of-week ("Friday", "Tuesday"). Same-day cutoff at 17:00
    # Phoenix: "Friday" said Friday morning → same day; said Friday after
    # 5pm → next Friday. Codex 2026-05-08 review.
    m = re.fullmatch(r"(monday|mon|tuesday|tue|tues|wednesday|wed|"
                     r"thursday|thu|thurs|friday|fri|saturday|sat|"
                     r"sunday|sun)", s, flags=re.I)
    if m:
        target_dow = _DOW.get(m.group(1).lower())
        if target_dow is not None:
            current = base_local.weekday()
            if target_dow == current:
                delta = 0 if base_local.hour < 17 else 7
            else:
                delta = (target_dow - current) % 7
            return _at_5pm_phoenix_to_utc(base_local + timedelta(days=delta))

    # Give up — leave as null. Reviewer can set the date manually.
    return None


def _dedup_key(summary: str, businesses: list[str], owner_display: str | None) -> str:
    """Stable key for "this candidate refers to the same logical task."""
    norm = " ".join(summary.lower().split())
    biz = ",".join(sorted(businesses))
    own = (owner_display or "").lower()
    return hashlib.sha256(f"{norm}|{biz}|{own}".encode("utf-8")).hexdigest()[:16]


def normalize_candidate(raw: dict, *, now: datetime | None = None) -> dict:
    """Take one extract-step candidate dict, return a normalized form.

    The shape is intentionally similar to the extract output — the
    retrieve and choose steps consume a list of these.
    """
    summary = (raw.get("summary") or "").strip()
    if not summary:
        return {}
    owner_display = _resolve_owner_display(raw.get("owner_hint"))
    businesses = _resolve_businesses(raw.get("businesses_hint") or [])
    due = _resolve_due(raw.get("due_hint"), now=now)
    # Pass through Slack mention ids unchanged (deduped, capped). The
    # cap protects the resolver from a pathological "paging everyone"
    # message yielding 100+ DB lookups, and dedupe protects from a
    # repeated mention. Filter to plain strings so a hostile/buggy LLM
    # payload can't smuggle nested objects into the candidate.
    raw_slack_ids = raw.get("owner_slack_user_ids")
    owner_slack_user_ids: list[str] = []
    if isinstance(raw_slack_ids, list):
        seen: set[str] = set()
        for entry in raw_slack_ids:
            if isinstance(entry, str) and 0 < len(entry) <= 64:
                if entry not in seen:
                    seen.add(entry)
                    owner_slack_user_ids.append(entry)
                    if len(owner_slack_user_ids) >= 10:
                        break
    # Pass through the boolean owner_is_poster flag. False if not a bool
    # to keep downstream consumers simple.
    raw_is_poster = raw.get("owner_is_poster")
    owner_is_poster = bool(raw_is_poster) if isinstance(raw_is_poster, bool) else False
    out = {
        "summary": summary,
        "owner_display": owner_display,
        "owner_slack_user_ids": owner_slack_user_ids,
        "owner_is_poster": owner_is_poster,
        "businesses": businesses,
        "due_at": due,
        "dependency_text": (raw.get("dependency_hint") or None),
        "category": (raw.get("category_hint") or None),
        "source_quote": raw.get("source_quote"),
        "source_timestamp": raw.get("source_timestamp"),
        "dedup_key": _dedup_key(summary, businesses, owner_display),
    }
    # File-drop CSV provenance (Chunk 9 step 2): pass parser_kind +
    # row metadata through so review_items.candidate_facts shows
    # which row a candidate came from. Only set when the upstream
    # extractor populated them; non-CSV sources don't carry these.
    for prov_key in ("parser_kind", "row_number", "raw_owner",
                      "raw_due", "raw_priority", "filename"):
        if prov_key in raw and raw[prov_key] is not None:
            out[prov_key] = raw[prov_key]
    return out


def normalize_candidates(candidates: list[dict], *, now: datetime | None = None) -> list[dict]:
    out: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        n = normalize_candidate(c, now=now)
        if n:
            out.append(n)
    return out

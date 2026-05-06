"""Step 2: normalize candidate fields.

Deterministic. Resolves owner aliases to canonical user_ids, business
hints to business slugs (validated), date hints to ISO timestamps when
parseable. Adds a stable dedup_key for use by the retrieve step.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

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


def _resolve_due(hint: str | None, *, now: datetime | None = None) -> str | None:
    if not hint:
        return None
    s = hint.strip()
    if not s:
        return None
    base = (now or datetime.now(timezone.utc))
    # Try strict ISO first.
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    # Today / tomorrow.
    if re.fullmatch(r"today", s, flags=re.I):
        return base.replace(hour=17, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    if re.fullmatch(r"tomorrow", s, flags=re.I):
        return (base + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0).isoformat()
    # "next <day-of-week>".
    m = re.fullmatch(r"next\s+(\w+)", s, flags=re.I)
    if m:
        target_dow = _DOW.get(m.group(1).lower())
        if target_dow is not None:
            current = base.weekday()
            delta = (target_dow - current) % 7 or 7
            target = base + timedelta(days=delta)
            return target.replace(hour=17, minute=0, second=0, microsecond=0).isoformat()
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
    # Pass through Slack mention ids unchanged. Source-specific resolvers
    # (slack_resolve.py) read them after this step. Filter to a list of
    # plain strings so a hostile/buggy LLM payload can't smuggle nested
    # objects into the candidate.
    raw_slack_ids = raw.get("owner_slack_user_ids")
    owner_slack_user_ids: list[str] = []
    if isinstance(raw_slack_ids, list):
        for entry in raw_slack_ids:
            if isinstance(entry, str) and 0 < len(entry) <= 64:
                owner_slack_user_ids.append(entry)
    return {
        "summary": summary,
        "owner_display": owner_display,
        "owner_slack_user_ids": owner_slack_user_ids,
        "businesses": businesses,
        "due_at": due,
        "dependency_text": (raw.get("dependency_hint") or None),
        "category": (raw.get("category_hint") or None),
        "source_quote": raw.get("source_quote"),
        "source_timestamp": raw.get("source_timestamp"),
        "dedup_key": _dedup_key(summary, businesses, owner_display),
    }


def normalize_candidates(candidates: list[dict], *, now: datetime | None = None) -> list[dict]:
    out: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        n = normalize_candidate(c, now=now)
        if n:
            out.append(n)
    return out

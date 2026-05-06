"""OpsMemory notifications: schedule shape contract.

Single source of truth for the notification_prefs.schedule jsonb
shape. Imported by:
  - api/app/v1_notifications.py    (PrefPatchBody field validator)
  - api/app/notifications/scheduler.py  (fire-time validation +
                                          next-fire computation)

Per Codex chunk-10-step3b1 plan-review: the API and the scheduler
must validate against the same contract or a saved row could fail
the scheduler at fire time, leaving silent gaps in delivery.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Allowed schedule.kind values. 'on_event' is reserved for event-
# triggered pushes (per migration 0013 comment) but the digest
# scheduler doesn't materialize event prefs; only daily / weekly.
SCHEDULE_KINDS = frozenset({"daily", "weekly"})

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_SET = frozenset(WEEKDAYS)
WEEKDAY_INDEX = {name: i for i, name in enumerate(WEEKDAYS)}

# Best-effort timezone whitelist regex. We DO use zoneinfo at fire
# time (validate_schedule_object does) so a malformed tz fails fast,
# but the regex catches obvious garbage before we even import zoneinfo.
_TIMEZONE_RE = re.compile(
    r"^(UTC|[A-Z][A-Za-z_]+/[A-Z][A-Za-z0-9_+\-]+(/[A-Z][A-Za-z0-9_+\-]+)?)$"
)


def validate_schedule_object(schedule: dict) -> None:
    """Raise ValueError on bad schedule shape.

    Strict: type(int) checks reject bool sneaking through (Codex
    chunk-10-step3b1-close blocker 2). Timezone is resolved against
    zoneinfo so a typo like 'America/Pheonix' fails immediately.
    """
    kind = schedule.get("kind")
    if kind not in SCHEDULE_KINDS:
        raise ValueError(
            f"schedule.kind must be one of {sorted(SCHEDULE_KINDS)}; got {kind!r}"
        )
    hour = schedule.get("hour")
    if type(hour) is not int or not (0 <= hour <= 23):
        raise ValueError(f"schedule.hour must be int 0..23; got {hour!r}")
    minute = schedule.get("minute")
    if type(minute) is not int or not (0 <= minute <= 59):
        raise ValueError(f"schedule.minute must be int 0..59; got {minute!r}")
    tz = schedule.get("timezone")
    if not isinstance(tz, str) or not _TIMEZONE_RE.match(tz):
        raise ValueError(
            "schedule.timezone must be a string IANA tz id "
            "(e.g. 'America/Phoenix' or 'UTC')"
        )
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        raise ValueError(f"schedule.timezone {tz!r} is not a known IANA zone")
    if kind == "weekly":
        weekday = schedule.get("weekday")
        if weekday not in WEEKDAY_SET:
            raise ValueError(
                f"schedule.weekday required for weekly kind; "
                f"must be one of {sorted(WEEKDAY_SET)}; got {weekday!r}"
            )


def compute_next_fire(schedule: dict, *, after: datetime) -> datetime:
    """Given a validated schedule and a UTC timestamp, return the next
    fire moment as a UTC-aware datetime.

    Caller must have already passed ``schedule`` through
    ``validate_schedule_object`` so this function may assume the shape
    is valid.

    The 'after' boundary is exclusive: the next fire is strictly later
    than ``after``. So if ``after`` is exactly the schedule's local
    fire moment, this returns the next day's (or week's) fire.
    """
    if after.tzinfo is None:
        raise ValueError("compute_next_fire requires a tz-aware 'after'")
    tz = ZoneInfo(schedule["timezone"])
    after_local = after.astimezone(tz)
    kind = schedule["kind"]
    hour = schedule["hour"]
    minute = schedule["minute"]
    candidate = after_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if kind == "daily":
        if candidate <= after_local:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(timezone.utc)
    if kind == "weekly":
        target_idx = WEEKDAY_INDEX[schedule["weekday"]]
        current_idx = candidate.weekday()  # Mon=0..Sun=6
        delta = (target_idx - current_idx) % 7
        candidate = candidate + timedelta(days=delta)
        if candidate <= after_local:
            candidate = candidate + timedelta(days=7)
        return candidate.astimezone(timezone.utc)
    # Should be unreachable given validate_schedule_object kinds.
    raise ValueError(f"unknown schedule.kind {kind!r}")

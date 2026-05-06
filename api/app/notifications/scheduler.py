"""OpsMemory notifications: schedule walker.

Per Codex chunk-10-step3c-close STEP 4 PLAN:
  - collect_due_prefs(conn, now)  yields (pref, user, scheduled_for)
  - For each due pref: validate the schedule (skip+log on failure),
    compute scheduled_for via compute_next_fire on the prior fire
    boundary, surface the row.
  - Caller (run_notification_scheduler.py) builds the digest payload
    via digest.build_digest_payload, then either logs (dry-run) or
    inserts a notification_deliveries row (sender mode, step 4
    commit 3 + step 5).

This module is intentionally read-only against the DB. Insert-side
work lives in the runner script so that dry-run mode never touches
notification_deliveries (Codex STEP 4 PLAN (4)).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from .schedule import compute_next_fire, validate_schedule_object


log = logging.getLogger("opsmemory.notifications.scheduler")


@dataclass
class DuePref:
    pref_id: str
    user_id: str
    user_email: str | None
    user_display_name: str | None
    user_timezone: str | None
    channel: str
    schedule: dict
    settings: dict
    scheduled_for: datetime  # tz-aware UTC


# How far back the walker will accept a missed-fire moment. If a
# scheduler invocation is delayed (server reboot, cron miss), we
# still want to send the digest as long as it's within this window.
# Beyond it, the moment has passed and the next regular fire takes
# over.
DEFAULT_LOOKBACK_MINUTES = 60


async def collect_due_prefs(
    conn,
    *,
    now: datetime,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
) -> AsyncIterator[DuePref]:
    """Yield DuePref rows whose scheduled_for is in (now-lookback, now].

    A pref is "due" if compute_next_fire(schedule, after=lookback_floor)
    falls at or before now. The walker queries every enabled pref;
    that's small (one row per user per channel) so the full scan is
    cheap. If we ever grow a million users we add a DB index by
    next_fire_at maintained by trigger; until then this is fine.
    """
    if now.tzinfo is None:
        raise ValueError("collect_due_prefs requires a tz-aware now")
    lookback_floor = now - timedelta(minutes=lookback_minutes)

    rows = await conn.fetch(
        """
        SELECT p.id::text         AS pref_id,
               p.user_id::text    AS user_id,
               p.channel          AS channel,
               p.schedule         AS schedule,
               p.settings         AS settings,
               u.email            AS user_email,
               u.display_name     AS user_display_name,
               u.timezone         AS user_timezone
          FROM notification_prefs p
          JOIN users u ON u.id = p.user_id
         WHERE p.enabled = true
        """
    )
    for row in rows:
        schedule = dict(row["schedule"] or {})
        try:
            validate_schedule_object(schedule)
        except ValueError as exc:
            log.warning(
                "scheduler_pref_invalid_schedule",
                extra={
                    "pref_id": row["pref_id"],
                    "user_id": row["user_id"],
                    "channel": row["channel"],
                    "reason": str(exc),
                },
            )
            continue
        try:
            scheduled_for = compute_next_fire(schedule, after=lookback_floor)
        except ValueError as exc:
            log.warning(
                "scheduler_pref_compute_failed",
                extra={
                    "pref_id": row["pref_id"],
                    "user_id": row["user_id"],
                    "channel": row["channel"],
                    "reason": str(exc),
                },
            )
            continue
        if scheduled_for > now:
            # Not yet — next fire is still ahead.
            continue
        yield DuePref(
            pref_id=row["pref_id"],
            user_id=row["user_id"],
            user_email=row["user_email"],
            user_display_name=row["user_display_name"],
            user_timezone=row["user_timezone"],
            channel=row["channel"],
            schedule=schedule,
            settings=dict(row["settings"] or {}),
            scheduled_for=scheduled_for.astimezone(timezone.utc),
        )


async def collect_tasks_for_user(
    conn,
    *,
    user_id: str,
    settings: dict,
    now: datetime,
) -> list[dict]:
    """Fetch the tasks the digest should mention for this user.

    Honors per-pref settings:
      include_completed: bool   include status='done' rows? (default false)
      include_stale:     bool   include rows whose due_at is older than now?
                                  (default true)
      stale_days:        int    only show stale rows within this window
                                  (default 7; ignored when include_stale=false)

    Visibility scoping mirrors auth.py: the user sees tasks for any
    business they have an active membership in. Owner role isn't a
    factor here — owners and admins both see what they have access
    to.
    """
    if now.tzinfo is None:
        raise ValueError("collect_tasks_for_user requires a tz-aware now")

    include_completed = bool(settings.get("include_completed", False))
    include_stale = bool(settings.get("include_stale", True))
    stale_days = int(settings.get("stale_days", 7))
    stale_floor = now - timedelta(days=stale_days)

    # Build the status filter inline per user. Default to open.
    status_clause = "t.status = 'open'"
    if include_completed:
        status_clause = "t.status IN ('open', 'done')"

    # When include_stale is false, drop tasks whose due_at is in
    # the past. When true, keep them but cap the lookback at
    # stale_floor. Single $2 threshold parameter either way; the
    # value differs.
    threshold = stale_floor if include_stale else now

    sql = f"""
        SELECT t.id::text                     AS id,
               t.summary                      AS summary,
               t.status                       AS status,
               t.priority                     AS priority,
               COALESCE(t.due_at::text, '')   AS due_iso,
               COALESCE(
                   ARRAY(
                       SELECT b.slug FROM businesses b
                        JOIN task_businesses tb ON tb.business_id = b.id
                        WHERE tb.task_id = t.id
                          AND b.deletion_state = 'active'
                       ORDER BY b.slug
                   ),
                   ARRAY[]::text[]
               )                              AS businesses
          FROM tasks t
         WHERE EXISTS (
                   SELECT 1
                     FROM task_businesses tb
                     JOIN business_memberships bm
                       ON bm.business_id = tb.business_id
                    WHERE tb.task_id = t.id
                      AND bm.user_id = $1::uuid
                      AND bm.status = 'active'
               )
           AND t.deletion_state = 'active'
           AND {status_clause}
           AND (t.due_at IS NULL OR t.due_at >= $2::timestamptz)
         ORDER BY t.due_at NULLS LAST, t.priority NULLS LAST, t.created_at DESC
         LIMIT 50
    """

    rows = await conn.fetch(sql, user_id, threshold)

    return [
        {
            "id": r["id"],
            "summary": r["summary"],
            "status": r["status"],
            "priority": r["priority"],
            "due_iso": r["due_iso"] or None,
            "businesses": list(r["businesses"]) if r["businesses"] else [],
        }
        for r in rows
    ]


def idempotency_key(pref_id: str, scheduled_for: datetime) -> str:
    """Per Codex chunk-10-step3c-close STEP 4 PLAN (5):
    '<pref_id>:<scheduled_for_iso>'.
    """
    if scheduled_for.tzinfo is None:
        raise ValueError("scheduled_for must be tz-aware")
    iso = scheduled_for.astimezone(timezone.utc).isoformat()
    return f"{pref_id}:{iso}"

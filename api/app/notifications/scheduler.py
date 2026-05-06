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

The walker (collect_due_prefs / collect_tasks_for_user) is read-
only. claim_delivery owns the per-(pref, fire, subscription) row
INSERT used by the runner's --claim mode; dry-run never touches
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

    # Codex chunk-10-step4-close BLOCKER 1: a disabled user with an
    # enabled pref would otherwise still receive a digest. Mirror
    # the auth.py user-status filter at the walker level.
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
           AND u.status = 'active'
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


def _normalize_settings(settings: dict) -> tuple[bool, bool, int]:
    """Validate + normalize per-pref settings to typed locals.

    Codex chunk-10-step4-close BLOCKER 3: bool(stranger truthy) is
    almost always True (e.g. bool('false') is True), and int('abc')
    crashes mid-run. Use strict type identity, fall back to the
    default whenever the saved value is the wrong shape, and log
    so an operator can see the bad row.
    """
    raw_completed = settings.get("include_completed", False)
    raw_stale = settings.get("include_stale", True)
    raw_stale_days = settings.get("stale_days", 7)
    if type(raw_completed) is not bool:
        log.warning(
            "scheduler_settings_include_completed_bad_shape",
            extra={"got": repr(raw_completed)},
        )
        raw_completed = False
    if type(raw_stale) is not bool:
        log.warning(
            "scheduler_settings_include_stale_bad_shape",
            extra={"got": repr(raw_stale)},
        )
        raw_stale = True
    if type(raw_stale_days) is not int or raw_stale_days < 0 or raw_stale_days > 3650:
        log.warning(
            "scheduler_settings_stale_days_bad_shape",
            extra={"got": repr(raw_stale_days)},
        )
        raw_stale_days = 7
    return raw_completed, raw_stale, raw_stale_days


async def collect_tasks_for_user(
    conn,
    *,
    user_id: str,
    settings: dict,
    now: datetime,
    fetch_limit: int = 50,
) -> tuple[list[dict], bool, int]:
    """Fetch the tasks the digest should mention for this user.

    Returns (tasks, has_more, total_count). Codex chunk-10-step4-
    close (d): the digest builder needs to know whether the list
    was truncated so it can render an honest 'and N more' line
    instead of a silent cut. We do COUNT(*) OVER() to get the
    full match count, but cap the rendered list at fetch_limit.

    Honors per-pref settings (validated + normalized):
      include_completed: bool   include status='done' rows? (default false)
      include_stale:     bool   include rows whose due_at is older than now?
                                  (default true)
      stale_days:        int    only show stale rows within this window
                                  (default 7; ignored when include_stale=false)

    Visibility scoping mirrors auth.py: the user sees tasks for any
    business they have an active membership in AND the business
    itself is still active.
    """
    if now.tzinfo is None:
        raise ValueError("collect_tasks_for_user requires a tz-aware now")

    include_completed, include_stale, stale_days = _normalize_settings(settings)
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

    # Codex chunk-10-step4-close BLOCKER 2: visibility EXISTS must
    # join through `businesses` and require deletion_state='active'.
    # Otherwise a task linked only to a soft-deleted business leaks
    # into the digest. Mirrors the auth.py shape.
    # Codex (d): COUNT(*) OVER() carries the full match count past
    # the LIMIT so the digest builder can show 'and N more' honestly.
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
               )                              AS businesses,
               COUNT(*) OVER()                AS total_count
          FROM tasks t
         WHERE EXISTS (
                   SELECT 1
                     FROM task_businesses tb
                     JOIN business_memberships bm
                       ON bm.business_id = tb.business_id
                     JOIN businesses b
                       ON b.id = tb.business_id
                    WHERE tb.task_id = t.id
                      AND bm.user_id = $1::uuid
                      AND bm.status = 'active'
                      AND b.deletion_state = 'active'
               )
           AND t.deletion_state = 'active'
           AND {status_clause}
           AND (t.due_at IS NULL OR t.due_at >= $2::timestamptz)
         ORDER BY t.due_at NULLS LAST, t.priority NULLS LAST, t.created_at DESC
         LIMIT $3::int
    """

    rows = await conn.fetch(sql, user_id, threshold, fetch_limit)
    total_count = int(rows[0]["total_count"]) if rows else 0
    has_more = total_count > fetch_limit

    items = [
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
    return items, has_more, total_count


def idempotency_key(
    pref_id: str,
    scheduled_for: datetime,
    web_push_subscription_id: str | None = None,
) -> str:
    """Per Codex chunk-10-step5 plan-review:
       web_push:                    '<pref_id>:<iso>:sub:<sub_id>'
       slack_dm / email_digest:     '<pref_id>:<iso>'    (unchanged)
    """
    if scheduled_for.tzinfo is None:
        raise ValueError("scheduled_for must be tz-aware")
    iso = scheduled_for.astimezone(timezone.utc).isoformat()
    if web_push_subscription_id is not None:
        return f"{pref_id}:{iso}:sub:{web_push_subscription_id}"
    return f"{pref_id}:{iso}"


async def list_active_subscriptions(conn, *, user_id: str) -> list[dict]:
    """List a user's active Web Push subscriptions, deterministic
    order. Codex chunk-10-step5 plan-review: order matches the
    API's list endpoint (created_at DESC, id DESC) so claim
    ordering is stable across runs.
    """
    rows = await conn.fetch(
        """
        SELECT id::text       AS id,
               endpoint       AS endpoint
          FROM web_push_subscriptions
         WHERE user_id = $1::uuid
           AND status = 'active'
         ORDER BY created_at DESC, id DESC
        """,
        user_id,
    )
    return [{"id": r["id"], "endpoint": r["endpoint"]} for r in rows]


async def claim_delivery(
    conn,
    *,
    due: DuePref,
    payload: dict,
    web_push_subscription_id: str | None = None,
) -> str | None:
    """Atomically claim a notification_deliveries row for this
    (pref_id, scheduled_for [, web_push_subscription_id]) tuple.

    Returns the new row's id when this caller won the race, None
    on conflict (another worker already inserted one).

    Codex chunk-10-step5 plan-review (3) invariants:
      - due.channel == 'web_push'  REQUIRES web_push_subscription_id.
      - other channels MUST NOT pass web_push_subscription_id.
    These prevent silent miscoded rows from sneaking past review.

    The caller (run_notification_scheduler.py + step 5 sender) is
    responsible for updating the row's status to 'sent' / 'failed'
    after the dispatch.

    Migrations:
      0015 grants opsmemory_app INSERT + UPDATE on
      notification_deliveries; SELECT was already in 0013.
      0016 adds the web_push_subscription_id column + FK + index.
    """
    if due.channel == "web_push":
        if web_push_subscription_id is None:
            raise ValueError(
                "claim_delivery for web_push requires web_push_subscription_id"
            )
    else:
        if web_push_subscription_id is not None:
            raise ValueError(
                f"claim_delivery for channel={due.channel!r} must not pass "
                "web_push_subscription_id"
            )
    key = idempotency_key(due.pref_id, due.scheduled_for, web_push_subscription_id)
    row = await conn.fetchrow(
        """
        INSERT INTO notification_deliveries
          (idempotency_key, user_id, pref_id, channel,
           status, scheduled_for, payload, web_push_subscription_id)
        VALUES
          ($1::text, $2::uuid, $3::uuid, $4::text,
           'scheduled', $5::timestamptz, $6::jsonb, $7)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id::text AS id
        """,
        key,
        due.user_id,
        due.pref_id,
        due.channel,
        due.scheduled_for,
        payload,
        web_push_subscription_id,  # asyncpg handles None -> NULL
    )
    return row["id"] if row else None

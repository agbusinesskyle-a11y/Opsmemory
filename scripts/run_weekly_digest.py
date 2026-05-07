#!/usr/bin/env python3
"""Run the OpsMemory per-business weekly digest builder once.

Walks businesses with at least one weekly_digest_allowlist entry,
fetches their tasks for the target week, builds the rendered Gmail
draft payload, and either logs it (dry-run) or hands it to n8n via
the gmail_sender (commit 2 of chunk 11).

Step 11 commit 1 ships dry-run only. The n8n send + admin API land
in commits 2 + 3.

Usage:
    python3 scripts/run_weekly_digest.py
        [--business <slug>]
        [--week YYYY-MM-DD]    # any date in the target week
        [--send]               # commit 2: actually fire n8n
        [--limit N]

Environment:
    DATABASE_URL                          runtime DSN
    NOTIFICATIONS_DRY_RUN                 '1' forces dry-run
    WEEKLY_DIGEST_DEFAULT_TZ              IANA tz for week boundary
                                           computation when a
                                           business has no own tz
                                           (default: America/Phoenix)
    WEEKLY_DIGEST_STALE_DAYS              days a task can be open
                                           past due before we count
                                           it 'stale' (default 14)

Exit codes:
    0  success
    1  configuration error
    2  partial failure (one or more businesses errored)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import asyncpg  # noqa: E402

from api.app.db import register_jsonb_codec  # noqa: E402
from api.app.notifications.weekly_digest import (  # noqa: E402
    build_weekly_digest_payload,
)


log = logging.getLogger("opsmemory.run_weekly_digest")


def _resolve_default_tz() -> ZoneInfo:
    name = os.environ.get("WEEKLY_DIGEST_DEFAULT_TZ", "America/Phoenix")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            f"WEEKLY_DIGEST_DEFAULT_TZ={name!r} is not a known IANA "
            "timezone. Install tzdata (apt or pip) or correct the env."
        ) from exc


def _week_bounds_for_date(d: date, tz: ZoneInfo) -> tuple[date, date]:
    """Return (mon, sun) bounds (inclusive) for the local week
    containing `d`. Mon=0 in datetime.weekday().
    """
    # Build a tz-aware datetime at midnight local for d, then back
    # off to Monday.
    dt = datetime.combine(d, time.min).replace(tzinfo=tz)
    monday_local = dt - timedelta(days=dt.weekday())
    monday = monday_local.date()
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _last_completed_week(now_utc: datetime, tz: ZoneInfo) -> tuple[date, date]:
    """The most recent completed Mon..Sun in `tz`. If today is
    Tuesday, returns last week (Mon..Sun preceding today's Mon).
    """
    today_local = now_utc.astimezone(tz).date()
    this_monday, _ = _week_bounds_for_date(today_local, tz)
    last_monday = this_monday - timedelta(days=7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


async def _fetch_businesses_with_allowlist(conn) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT b.id::text   AS id,
               b.slug::text AS slug,
               b.name       AS name
          FROM businesses b
         WHERE b.deletion_state = 'active'
           AND EXISTS (
                 SELECT 1 FROM weekly_digest_allowlist a
                  WHERE a.business_id = b.id
               )
         ORDER BY b.slug
        """
    )
    return [{"id": r["id"], "slug": r["slug"], "name": r["name"]} for r in rows]


async def _fetch_business_by_slug(conn, slug: str) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT b.id::text   AS id,
               b.slug::text AS slug,
               b.name       AS name
          FROM businesses b
         WHERE b.slug = $1
           AND b.deletion_state = 'active'
        """,
        slug,
    )
    if row is None:
        return None
    return {"id": row["id"], "slug": row["slug"], "name": row["name"]}


async def _fetch_allowlist(conn, business_id: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT recipient_email::text AS recipient_email,
               role                  AS role
          FROM weekly_digest_allowlist
         WHERE business_id = $1::uuid
         ORDER BY role, recipient_email
        """,
        business_id,
    )
    return [
        {"recipient_email": r["recipient_email"], "role": r["role"]}
        for r in rows
    ]


_TASK_BASE_SELECT = """
    SELECT t.id::text                      AS id,
           t.summary                       AS summary,
           t.status::text                  AS status,
           t.priority::text                AS priority,
           COALESCE(t.due_at::text, '')    AS due_iso,
           COALESCE(t.completed_at::text, '') AS completed_iso,
           u.display_name                  AS owner_display_name
      FROM tasks t
      JOIN task_businesses tb ON tb.task_id = t.id
      LEFT JOIN task_assignees ta ON ta.task_id = t.id
      LEFT JOIN users u ON u.id = ta.user_id
     WHERE tb.business_id = $1::uuid
       AND t.deletion_state = 'active'
"""


async def _fetch_tasks_open(conn, business_id: str) -> list[dict]:
    rows = await conn.fetch(
        _TASK_BASE_SELECT
        + " AND t.status = 'open' "
        + " ORDER BY t.due_at NULLS LAST, t.priority NULLS LAST, t.created_at DESC "
        + " LIMIT 200 ",
        business_id,
    )
    return [
        {
            "id": r["id"], "summary": r["summary"], "status": r["status"],
            "priority": r["priority"], "due_iso": r["due_iso"] or None,
            "owner_display_name": r["owner_display_name"],
        } for r in rows
    ]


async def _fetch_tasks_completed_in_window(
    conn, business_id: str, start_utc: datetime, end_utc: datetime,
) -> list[dict]:
    rows = await conn.fetch(
        _TASK_BASE_SELECT
        + " AND t.status = 'done' "
        + "       AND t.completed_at >= $2::timestamptz "
        + "       AND t.completed_at <  $3::timestamptz "
        + " ORDER BY t.completed_at DESC "
        + " LIMIT 200 ",
        business_id, start_utc, end_utc,
    )
    return [
        {
            "id": r["id"], "summary": r["summary"], "status": r["status"],
            "priority": r["priority"], "due_iso": r["due_iso"] or None,
            "completed_iso": r["completed_iso"] or None,
            "owner_display_name": r["owner_display_name"],
        } for r in rows
    ]


async def _fetch_tasks_stale(
    conn, business_id: str, stale_floor_utc: datetime, now_utc: datetime,
) -> list[dict]:
    """Open tasks whose due_at is between stale_floor and now (i.e.
    overdue but not so old we drop them)."""
    rows = await conn.fetch(
        _TASK_BASE_SELECT
        + " AND t.status = 'open' "
        + "       AND t.due_at IS NOT NULL "
        + "       AND t.due_at < $3::timestamptz "
        + "       AND t.due_at >= $2::timestamptz "
        + " ORDER BY t.due_at ASC "
        + " LIMIT 200 ",
        business_id, stale_floor_utc, now_utc,
    )
    return [
        {
            "id": r["id"], "summary": r["summary"], "status": r["status"],
            "priority": r["priority"], "due_iso": r["due_iso"] or None,
            "owner_display_name": r["owner_display_name"],
        } for r in rows
    ]


def _idempotency_key(business_slug: str, week_start: date) -> str:
    return f"weekly_digest:{business_slug}:{week_start.isoformat()}"


async def main_async(args: argparse.Namespace) -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL must be set", file=sys.stderr)
        return 1

    if args.send:
        # Step 11 commit 1 only ships dry-run; n8n send is commit 2.
        print(
            "ERROR: --send is not yet implemented (chunk 11 commit 2 "
            "adds the gmail_sender + n8n bridge). Re-run without --send.",
            file=sys.stderr,
        )
        return 1

    env_dry = os.environ.get("NOTIFICATIONS_DRY_RUN", "").strip() == "1"
    dry_run = True or env_dry  # forced dry-run in this commit

    try:
        tz = _resolve_default_tz()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    stale_days = int(os.environ.get("WEEKLY_DIGEST_STALE_DAYS", "14"))

    now_utc = datetime.now(timezone.utc)

    # Determine target week.
    if args.week:
        try:
            d = date.fromisoformat(args.week)
        except ValueError:
            print(f"ERROR: --week must be YYYY-MM-DD; got {args.week!r}",
                  file=sys.stderr)
            return 1
        week_start, week_end = _week_bounds_for_date(d, tz)
    else:
        week_start, week_end = _last_completed_week(now_utc, tz)

    # UTC bounds for the local week (used for completed_at filter
    # on tasks).
    week_start_utc = datetime.combine(week_start, time.min).replace(tzinfo=tz).astimezone(timezone.utc)
    week_end_utc = datetime.combine(week_end + timedelta(days=1), time.min).replace(tzinfo=tz).astimezone(timezone.utc)
    stale_floor_utc = now_utc - timedelta(days=stale_days)

    pool = await asyncpg.create_pool(
        dsn=dsn, min_size=1, max_size=2, setup=register_jsonb_codec,
    )
    considered = 0
    emitted = 0
    errors = 0
    try:
        async with pool.acquire() as conn:
            if args.business:
                biz = await _fetch_business_by_slug(conn, args.business)
                if biz is None:
                    print(
                        f"ERROR: business slug {args.business!r} not found "
                        "(or deleted).",
                        file=sys.stderr,
                    )
                    return 1
                businesses = [biz]
            else:
                businesses = await _fetch_businesses_with_allowlist(conn)

            for biz in businesses:
                considered += 1
                if args.limit and emitted >= args.limit:
                    break
                try:
                    allowlist = await _fetch_allowlist(conn, biz["id"])
                    if not allowlist:
                        print(
                            f"[DRY-RUN] business={biz['slug']} "
                            f"reason=allowlist_empty"
                        )
                        continue
                    open_tasks = await _fetch_tasks_open(conn, biz["id"])
                    completed_tasks = await _fetch_tasks_completed_in_window(
                        conn, biz["id"], week_start_utc, week_end_utc,
                    )
                    stale_tasks = await _fetch_tasks_stale(
                        conn, biz["id"], stale_floor_utc, now_utc,
                    )
                    payload = build_weekly_digest_payload(
                        business=biz,
                        tasks_open=open_tasks,
                        tasks_completed_this_week=completed_tasks,
                        tasks_stale=stale_tasks,
                        recipients=allowlist,
                        week_start=week_start,
                        week_end=week_end,
                        generated_at=now_utc,
                    )
                    key = _idempotency_key(biz["slug"], week_start)
                    print(
                        f"[DRY-RUN] week={week_start.isoformat()}..{week_end.isoformat()} "
                        f"business={biz['slug']} "
                        f"to={len(payload['to'])} cc={len(payload['cc'])} "
                        f"bcc={len(payload['bcc'])} "
                        f"open={payload['counts']['open']} "
                        f"completed={payload['counts']['completed']} "
                        f"stale={payload['counts']['stale']} "
                        f"key={key} "
                        f"subject={payload['subject']!r}"
                    )
                    emitted += 1
                except Exception:
                    errors += 1
                    log.exception(
                        "weekly_digest_business_error",
                        extra={"business_slug": biz.get("slug"),
                                "business_id": biz.get("id")},
                    )
                    continue
        print(
            f"[DRY-RUN] considered={considered} emitted={emitted} "
            f"errors={errors} week={week_start.isoformat()}..{week_end.isoformat()}"
        )
        if errors > 0:
            return 2
        return 0
    finally:
        await pool.close()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--business", type=str, default=None,
        help="restrict to one business slug",
    )
    parser.add_argument(
        "--week", type=str, default=None,
        help="any YYYY-MM-DD inside the target week (defaults to "
             "the last completed week)",
    )
    parser.add_argument(
        "--send", action="store_true", default=False,
        help="(reserved for chunk 11 commit 2) ship via n8n; "
             "currently rejected with exit 1.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="max businesses to emit per run",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Run the OpsMemory notification scheduler once.

Walks notification_prefs for users whose schedule.next_fire is at or
behind now (within DEFAULT_LOOKBACK_MINUTES), builds the digest
payload via api.app.notifications.digest, and either logs it
(dry-run) or hands it to the sender (step 5).

This commit (step 4 commit 2) ships dry-run only. The
notification_deliveries row write + sender wiring land in step 4
commit 3 + step 5.

Usage:
    python3 scripts/run_notification_scheduler.py
        [--limit N]
        [--channel web_push|slack_dm|email_digest]
        [--dry-run]                # default true until step 4 commit 3

Environment:
    DATABASE_URL                  runtime DSN
    NOTIFICATIONS_DRY_RUN         '1' forces dry-run regardless of flag
    NOTIFICATIONS_LOOKBACK_MINUTES override DEFAULT_LOOKBACK_MINUTES (60)

Exit codes:
    0  success
    1  configuration error
    2  partial failure (one or more prefs errored; details in journal)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import asyncpg  # noqa: E402

from api.app.db import register_jsonb_codec  # noqa: E402
from api.app.notifications.digest import build_digest_payload  # noqa: E402
from api.app.notifications.scheduler import (  # noqa: E402
    DEFAULT_LOOKBACK_MINUTES,
    collect_due_prefs,
    collect_tasks_for_user,
    idempotency_key,
)


log = logging.getLogger("opsmemory.run_notification_scheduler")


async def main_async(args: argparse.Namespace) -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL must be set", file=sys.stderr)
        return 1

    # Honor env override even when the operator forgot the flag.
    dry_run = args.dry_run or os.environ.get("NOTIFICATIONS_DRY_RUN", "").strip() == "1"
    if not dry_run:
        # Step 4 commit 2 only ships dry-run; row-acquisition is
        # commit 3. Refuse to proceed in non-dry-run mode rather
        # than silently no-op.
        print(
            "ERROR: non-dry-run scheduler is not yet implemented "
            "(step 4 commit 3 adds notification_deliveries grants and "
            "row acquisition). Re-run with --dry-run or "
            "NOTIFICATIONS_DRY_RUN=1.",
            file=sys.stderr,
        )
        return 1

    lookback = int(
        os.environ.get("NOTIFICATIONS_LOOKBACK_MINUTES", str(DEFAULT_LOOKBACK_MINUTES))
    )
    now = datetime.now(timezone.utc)

    pool = await asyncpg.create_pool(
        dsn=dsn, min_size=1, max_size=2, setup=register_jsonb_codec,
    )
    try:
        considered = 0
        emitted = 0
        skipped_channel = 0
        async with pool.acquire() as conn:
            async for due in collect_due_prefs(
                conn, now=now, lookback_minutes=lookback
            ):
                considered += 1
                if args.channel and due.channel != args.channel:
                    skipped_channel += 1
                    continue
                if args.limit and emitted >= args.limit:
                    break
                tasks = await collect_tasks_for_user(
                    conn,
                    user_id=due.user_id,
                    settings=due.settings,
                    now=now,
                )
                user_dict = {
                    "id": due.user_id,
                    "email": due.user_email,
                    "display_name": due.user_display_name,
                    "timezone": due.user_timezone,
                }
                pref_dict = {
                    "id": due.pref_id,
                    "channel": due.channel,
                    "schedule": due.schedule,
                    "settings": due.settings,
                }
                payload = build_digest_payload(
                    user=user_dict,
                    pref=pref_dict,
                    tasks=tasks,
                    scheduled_for=due.scheduled_for,
                )
                key = idempotency_key(due.pref_id, due.scheduled_for)
                # Single-line summary so each emit is greppable in
                # journalctl.
                print(
                    f"[DRY-RUN] {due.scheduled_for.isoformat()} "
                    f"channel={due.channel} "
                    f"user={due.user_display_name or due.user_email or due.user_id} "
                    f"tasks={len(tasks)} "
                    f"key={key} "
                    f"title={payload['title']!r}"
                )
                emitted += 1
        print(
            f"[DRY-RUN] considered={considered} emitted={emitted} "
            f"skipped_channel={skipped_channel}"
        )
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
        "--limit", type=int, default=None,
        help="max prefs to emit per run",
    )
    parser.add_argument(
        "--channel", type=str, default=None,
        choices=["web_push", "slack_dm", "email_digest"],
        help="restrict to one channel",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="(default) compute payloads, log them, no DB writes",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

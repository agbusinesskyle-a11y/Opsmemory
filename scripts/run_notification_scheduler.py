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
    claim_delivery,
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

    # Default to dry-run for safety. NOTIFICATIONS_DRY_RUN=1 forces
    # dry-run regardless of --claim. Claim mode requires explicit
    # --claim AND no env override.
    env_dry = os.environ.get("NOTIFICATIONS_DRY_RUN", "").strip() == "1"
    claim_mode = args.claim and not env_dry
    dry_run = not claim_mode

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
        errors = 0
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
                # Codex chunk-10-step4-close (i): per-pref try /
                # except / continue so one user's bad row doesn't
                # crash the whole scheduler run. Exit 2 escalates
                # at the end if any per-pref errors occurred.
                try:
                    tasks, has_more, total_count = await collect_tasks_for_user(
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
                        total_count=total_count,
                    )
                    key = idempotency_key(due.pref_id, due.scheduled_for)
                    tag = "[DRY-RUN]" if not claim_mode else "[CLAIM]"
                    delivery_id = None
                    if claim_mode:
                        delivery_id = await claim_delivery(
                            conn, due=due, payload=payload,
                        )
                        if delivery_id is None:
                            print(
                                f"[CLAIM-SKIP] {due.scheduled_for.isoformat()} "
                                f"channel={due.channel} "
                                f"user={due.user_display_name or due.user_email or due.user_id} "
                                f"key={key} "
                                f"reason=already_claimed"
                            )
                            continue
                    print(
                        f"{tag} {due.scheduled_for.isoformat()} "
                        f"channel={due.channel} "
                        f"user={due.user_display_name or due.user_email or due.user_id} "
                        f"tasks={len(tasks)} "
                        f"total={total_count} "
                        f"key={key} "
                        f"delivery_id={delivery_id or '-'} "
                        f"title={payload['title']!r}"
                    )
                    emitted += 1
                except Exception:
                    errors += 1
                    log.exception(
                        "scheduler_pref_error",
                        extra={
                            "pref_id": due.pref_id,
                            "user_id": due.user_id,
                            "channel": due.channel,
                        },
                    )
                    continue
        tag = "[DRY-RUN]" if not claim_mode else "[CLAIM]"
        print(
            f"{tag} considered={considered} emitted={emitted} "
            f"skipped_channel={skipped_channel} errors={errors}"
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
        "--limit", type=int, default=None,
        help="max prefs to emit per run",
    )
    parser.add_argument(
        "--channel", type=str, default=None,
        choices=["web_push", "slack_dm", "email_digest"],
        help="restrict to one channel",
    )
    parser.add_argument(
        "--claim", action="store_true", default=False,
        help="claim a notification_deliveries row per due pref. "
             "Without this flag the runner stays in dry-run "
             "(payloads logged, no DB writes). NOTIFICATIONS_DRY_RUN=1 "
             "forces dry-run even when --claim is set.",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

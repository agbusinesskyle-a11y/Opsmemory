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
    list_active_subscriptions,
)
from api.app.notifications.sender import (  # noqa: E402
    DEFAULT_SEND_TIMEOUT_SECONDS,
    preflight_sender,
    send_one,
)


log = logging.getLogger("opsmemory.run_notification_scheduler")


async def main_async(args: argparse.Namespace) -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL must be set", file=sys.stderr)
        return 1

    # Default to dry-run for safety. NOTIFICATIONS_DRY_RUN=1 forces
    # dry-run regardless of --claim/--send.
    # Codex chunk-10-step5c2-close COMMIT 3 PLAN:
    #   --send implies claim AND invokes send_one for each row.
    #   --send and --claim are mutually exclusive at the CLI;
    #   either way NOTIFICATIONS_DRY_RUN=1 wins.
    env_dry = os.environ.get("NOTIFICATIONS_DRY_RUN", "").strip() == "1"
    if args.send and args.claim:
        print(
            "ERROR: --send and --claim are mutually exclusive. "
            "--send implies --claim plus actually shipping the push.",
            file=sys.stderr,
        )
        return 1
    send_mode = args.send and not env_dry
    claim_mode = (args.claim or args.send) and not env_dry
    dry_run = not claim_mode

    # Preflight before opening the pool so a misconfigured deploy
    # exits 1 fast without DB churn (Codex COMMIT 3 PLAN).
    vapid = None
    if send_mode:
        try:
            vapid = preflight_sender()
        except RuntimeError as exc:
            print(f"ERROR: sender preflight failed: {exc}", file=sys.stderr)
            return 1

    lookback = int(
        os.environ.get("NOTIFICATIONS_LOOKBACK_MINUTES", str(DEFAULT_LOOKBACK_MINUTES))
    )
    now = datetime.now(timezone.utc)

    pool = await asyncpg.create_pool(
        dsn=dsn, min_size=1, max_size=2, setup=register_jsonb_codec,
    )
    try:
        # Codex chunk-10-step5 plan-review (5): --limit applies per
        # due pref, NOT per delivery row. A web_push pref with N
        # active devices counts as 1 toward --limit. Track delivery
        # rows separately.
        considered = 0
        emitted = 0           # prefs that produced ≥1 row
        delivered = 0         # delivery rows actually claimed/logged
        skipped_channel = 0
        skipped_no_subs = 0   # web_push pref with 0 active devices
        skipped_claim = 0     # claim conflict (already claimed)
        skipped_unsupported = 0  # send mode encountered slack_dm/email_digest
        sent = 0              # send_one returned status='sent'
        send_failed = 0       # send_one returned status='failed'
        send_errors = 0       # send_one itself raised
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
                    user_label = (
                        due.user_display_name or due.user_email or due.user_id
                    )
                    if send_mode:
                        tag = "[SEND]"
                    elif claim_mode:
                        tag = "[CLAIM]"
                    else:
                        tag = "[DRY-RUN]"

                    if due.channel != "web_push" and send_mode:
                        # Codex chunk-10-step5c2-close COMMIT 3 PLAN:
                        # send mode handles only web_push for now.
                        # slack_dm / email_digest senders land in
                        # step 6 / a future module.
                        print(
                            f"[SEND-SKIP] {due.scheduled_for.isoformat()} "
                            f"channel={due.channel} user={user_label} "
                            f"reason=channel_unsupported_in_send_mode"
                        )
                        skipped_unsupported += 1
                        continue

                    if due.channel == "web_push":
                        # Codex chunk-10-step5 plan-review: one row
                        # per (pref, fire, active subscription).
                        subs = await list_active_subscriptions(
                            conn, user_id=due.user_id,
                        )
                        if not subs:
                            print(
                                f"[CLAIM-SKIP] {due.scheduled_for.isoformat()} "
                                f"channel=web_push user={user_label} "
                                f"reason=no_active_subscriptions"
                            )
                            skipped_no_subs += 1
                            continue
                        any_delivered = False
                        for sub in subs:
                            key = idempotency_key(
                                due.pref_id, due.scheduled_for, sub["id"],
                            )
                            delivery_id = None
                            if claim_mode:
                                delivery_id = await claim_delivery(
                                    conn, due=due, payload=payload,
                                    web_push_subscription_id=sub["id"],
                                )
                                if delivery_id is None:
                                    print(
                                        f"[CLAIM-SKIP] {due.scheduled_for.isoformat()} "
                                        f"channel=web_push user={user_label} "
                                        f"sub={sub['id']} key={key} "
                                        f"reason=already_claimed"
                                    )
                                    skipped_claim += 1
                                    continue
                            send_status = None
                            send_http = None
                            send_code = None
                            if send_mode and delivery_id is not None:
                                # Codex COMMIT 3 PLAN: per-row
                                # try/except inside the subscription
                                # loop. One bad device must not
                                # break the rest of the user's
                                # devices.
                                try:
                                    result = await send_one(
                                        conn,
                                        delivery_id=delivery_id,
                                        user_id=due.user_id,
                                        pref_id=due.pref_id,
                                        web_push_subscription_id=sub["id"],
                                        payload=payload,
                                        vapid=vapid,
                                    )
                                    send_status = result.status
                                    send_http = result.http_status
                                    send_code = result.code
                                    if result.status == "sent":
                                        sent += 1
                                    else:
                                        send_failed += 1
                                except Exception:
                                    send_errors += 1
                                    send_status = "error"
                                    log.exception(
                                        "scheduler_send_error",
                                        extra={
                                            "delivery_id": delivery_id,
                                            "subscription_id": sub["id"],
                                            "pref_id": due.pref_id,
                                        },
                                    )
                            print(
                                f"{tag} {due.scheduled_for.isoformat()} "
                                f"channel=web_push user={user_label} "
                                f"sub={sub['id']} tasks={len(tasks)} "
                                f"total={total_count} key={key} "
                                f"delivery_id={delivery_id or '-'} "
                                f"send={send_status or '-'} "
                                f"http={send_http or '-'} "
                                f"code={send_code or '-'} "
                                f"title={payload['title']!r}"
                            )
                            delivered += 1
                            any_delivered = True
                        if any_delivered:
                            emitted += 1
                    else:
                        # slack_dm / email_digest: one row per pref+
                        # fire, web_push_subscription_id stays NULL.
                        key = idempotency_key(due.pref_id, due.scheduled_for)
                        delivery_id = None
                        if claim_mode:
                            delivery_id = await claim_delivery(
                                conn, due=due, payload=payload,
                            )
                            if delivery_id is None:
                                print(
                                    f"[CLAIM-SKIP] {due.scheduled_for.isoformat()} "
                                    f"channel={due.channel} user={user_label} "
                                    f"key={key} reason=already_claimed"
                                )
                                skipped_claim += 1
                                continue
                        print(
                            f"{tag} {due.scheduled_for.isoformat()} "
                            f"channel={due.channel} user={user_label} "
                            f"tasks={len(tasks)} total={total_count} "
                            f"key={key} delivery_id={delivery_id or '-'} "
                            f"title={payload['title']!r}"
                        )
                        delivered += 1
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
        if send_mode:
            tag = "[SEND]"
        elif claim_mode:
            tag = "[CLAIM]"
        else:
            tag = "[DRY-RUN]"
        print(
            f"{tag} considered={considered} emitted={emitted} "
            f"delivered={delivered} sent={sent} send_failed={send_failed} "
            f"send_errors={send_errors} "
            f"skipped_channel={skipped_channel} "
            f"skipped_no_subs={skipped_no_subs} "
            f"skipped_claim={skipped_claim} "
            f"skipped_unsupported={skipped_unsupported} "
            f"errors={errors}"
        )
        if errors > 0 or send_errors > 0:
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
        help="max prefs to emit per run. NOTE: limit applies per "
             "due pref, NOT per delivery row. A web_push pref with "
             "N active devices counts as 1 toward this limit "
             "(Codex chunk-10-step5 plan-review). Use the final "
             "summary's delivered=<N> counter to see actual row "
             "count.",
    )
    parser.add_argument(
        "--channel", type=str, default=None,
        choices=["web_push", "slack_dm", "email_digest"],
        help="restrict to one channel",
    )
    parser.add_argument(
        "--claim", action="store_true", default=False,
        help="claim a notification_deliveries row per due pref but "
             "do NOT actually send. Use this to rehearse claim "
             "behavior without invoking pywebpush. "
             "NOTIFICATIONS_DRY_RUN=1 still forces dry-run.",
    )
    parser.add_argument(
        "--send", action="store_true", default=False,
        help="claim AND ship each web_push delivery via pywebpush. "
             "Mutually exclusive with --claim. Requires VAPID env "
             "(VAPID_PUBLIC_KEY/PRIVATE_KEY/SUBJECT) — preflight "
             "fails fast otherwise. slack_dm / email_digest are "
             "skipped in send mode (channel_unsupported_in_send_mode); "
             "they ship in step 6.",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

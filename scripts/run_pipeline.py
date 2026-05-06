#!/usr/bin/env python3
"""Run the OpsMemory reconciliation pipeline once over pending events.

Atomically claims rows from `ingest_events` whose status is `received`,
`failed`, or `extracting` (and stale, i.e. processing_started_at older
than INGEST_PIPELINE_STALE_MINUTES). Each claim flips status to
`extracting` in the same statement under FOR UPDATE SKIP LOCKED, so two
concurrent workers cannot double-process the same event.

Pipeline steps (extract -> normalize -> retrieve -> choose -> validate
-> queue review) run per claimed event.

Designed to be invoked manually (one-shot) or via a systemd timer.
NOT a long-running daemon.

Usage:
    python3 scripts/run_pipeline.py [--limit N] [--source meeting_recap]

Environment:
    DATABASE_URL                  runtime DSN (use opsmemory_owner for now;
                                  pipeline writes ingest_events.status,
                                  inserts review_items + llm_calls)
    INGEST_LLM_EXTRACT_MODELS     ordered fallback chain (default: 'mock')
    INGEST_LLM_CHOOSE_MODELS      same shape; local llama excluded by code
    LITELLM_BASE_URL              litellm proxy URL
    LITELLM_API_KEY               litellm api key
    INGEST_PIPELINE_LIMIT         max events per invocation (default: 50)
    INGEST_PIPELINE_STALE_MINUTES re-claim threshold for stuck `extracting`
                                  rows (default: 10)
    ENVIRONMENT                   when 'production', mock LLM models are
                                  refused (fail-closed; see llm_client.py)

Exit codes:
    0  success — N events processed (may be 0)
    1  configuration error
    2  partial failure (some events errored; details in journal)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Run from repo root so api.app imports resolve.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import asyncpg  # noqa: E402

from api.app.db import register_jsonb_codec  # noqa: E402
from api.app.reconciliation.pipeline import process_event  # noqa: E402
from api.app.reconciliation.sources import SOURCES  # noqa: E402

log = logging.getLogger("opsmemory.run_pipeline")


async def main_async(args: argparse.Namespace) -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("ACTION_TRACKER_DATABASE_URL")
    if not dsn:
        # Fallback: build from POSTGRES_CONTAINER + role + db env vars used elsewhere.
        # Worker prefers a high-privilege role since it writes ingest_events status,
        # review_items, and llm_calls.
        print("ERROR: DATABASE_URL or ACTION_TRACKER_DATABASE_URL must be set", file=sys.stderr)
        return 1

    stale_minutes = int(os.environ.get("INGEST_PIPELINE_STALE_MINUTES", "10"))

    # Worker pool MUST install the same jsonb codec the API pool uses,
    # otherwise pipeline.py's raw-dict params to ::jsonb fail (Codex
    # chunk-4-close blocker).
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=2,
        setup=register_jsonb_codec,
    )
    # Source allowlist for unfiltered claims. Without this, an event
    # whose source isn't registered in reconciliation/sources.py would
    # be marked 'failed' by pipeline.process_event and then re-claimed
    # by the next worker run forever, with retry_count climbing
    # unbounded (Codex chunk-5-step2 close blocker).
    if args.source is not None:
        # Operator explicitly asked for one source; let them, even if
        # it's unregistered — they'll see the per-event failure reason.
        allowed_sources: list[str] | None = [args.source]
    else:
        allowed_sources = sorted(SOURCES.keys())

    try:
        # Atomic claim: pick up to `limit` rows whose status is received,
        # failed, or stale-extracting; flip them to extracting in the same
        # statement; return their ids. SKIP LOCKED stops two workers from
        # racing for the same row.
        claimed_rows: list[dict] = []
        async with pool.acquire() as conn:
            for _ in range(args.limit):
                row = await conn.fetchrow(
                    """
                    UPDATE ingest_events
                       SET status = 'extracting',
                           processing_started_at = now()
                     WHERE id = (
                         SELECT id FROM ingest_events
                          WHERE source = ANY($1::text[])
                            AND (
                                  status IN ('received', 'failed')
                               OR (status = 'extracting'
                                   AND processing_started_at < now() - ($2::int * interval '1 minute'))
                            )
                          ORDER BY received_at
                          FOR UPDATE SKIP LOCKED
                          LIMIT 1
                       )
                    RETURNING id::text AS id, source::text AS source
                    """,
                    allowed_sources,
                    stale_minutes,
                )
                if not row:
                    break
                claimed_rows.append({"id": row["id"], "source": row["source"]})

        if not claimed_rows:
            print("No pending ingest events.")
            return 0

        print(f"Processing {len(claimed_rows)} claimed event(s)...")
        any_failures = False
        for row in claimed_rows:
            async with pool.acquire() as conn:
                try:
                    result = await process_event(conn, row["id"])
                    print(f"  {row['id']}  source={row['source']}  -> {result.get('status')}  "
                          f"(review_items={result.get('review_items', 0)})")
                    if result.get("status") in ("failed", "skipped"):
                        any_failures = True
                except Exception as exc:
                    log.exception("pipeline_unhandled_error")
                    # Mark the event failed so it gets picked up next run
                    # rather than being stranded in 'extracting'. The claim
                    # loop's stale-recovery is a safety net; this is the
                    # primary path.
                    try:
                        async with pool.acquire() as failconn:
                            await failconn.execute(
                                "UPDATE ingest_events "
                                "SET status = 'failed', failed_at = now(), "
                                "    error = $2, retry_count = retry_count + 1 "
                                "WHERE id = $1::uuid",
                                row["id"],
                                f"worker_unhandled: {exc!r}"[:1024],
                            )
                    except Exception:
                        log.exception("pipeline_failmark_failed")
                    print(f"  {row['id']}  source={row['source']}  -> CRASHED: {exc!r}", file=sys.stderr)
                    any_failures = True

        return 2 if any_failures else 0
    finally:
        await pool.close()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int,
                        default=int(os.environ.get("INGEST_PIPELINE_LIMIT", "50")),
                        help="Max events per invocation (default: 50)")
    parser.add_argument("--source", default=None,
                        help="Filter to one source (e.g. 'meeting_recap'). Default: all.")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Run the OpsMemory reconciliation pipeline once over pending events.

Polls `ingest_events` for rows with `status IN ('received', 'failed')`
and processes each through the 6-step pipeline (extract -> normalize ->
retrieve -> choose -> validate -> queue review).

Designed to be invoked manually (one-shot) or via a systemd timer
(future). NOT a long-running daemon.

Usage:
    python3 scripts/run_pipeline.py [--limit N] [--source meeting_recap]

Environment:
    DATABASE_URL                runtime DSN (use opsmemory_owner for now;
                                pipeline writes ingest_events.status,
                                inserts review_items + llm_calls)
    INGEST_LLM_EXTRACT_MODELS   ordered fallback chain (default: 'mock')
    INGEST_LLM_CHOOSE_MODELS    same shape; local llama excluded by code
    LITELLM_BASE_URL            litellm proxy URL
    LITELLM_API_KEY             litellm api key
    INGEST_PIPELINE_LIMIT       max events per invocation (default: 50)

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

from api.app.reconciliation.pipeline import process_event  # noqa: E402

log = logging.getLogger("opsmemory.run_pipeline")


async def main_async(args: argparse.Namespace) -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("ACTION_TRACKER_DATABASE_URL")
    if not dsn:
        # Fallback: build from POSTGRES_CONTAINER + role + db env vars used elsewhere.
        # Worker prefers a high-privilege role since it writes ingest_events status,
        # review_items, and llm_calls.
        print("ERROR: DATABASE_URL or ACTION_TRACKER_DATABASE_URL must be set", file=sys.stderr)
        return 1

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text AS id, source FROM ingest_events
                WHERE status IN ('received', 'failed')
                  AND ($1::text IS NULL OR source = $1)
                ORDER BY received_at
                LIMIT $2
                """,
                args.source,
                args.limit,
            )
        if not rows:
            print("No pending ingest events.")
            return 0

        print(f"Processing {len(rows)} pending event(s)...")
        any_failures = False
        for row in rows:
            async with pool.acquire() as conn:
                try:
                    result = await process_event(conn, row["id"])
                    print(f"  {row['id']}  source={row['source']}  -> {result.get('status')}  "
                          f"(review_items={result.get('review_items', 0)})")
                    if result.get("status") in ("failed", "skipped"):
                        any_failures = True
                except Exception as exc:
                    log.exception("pipeline_unhandled_error")
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

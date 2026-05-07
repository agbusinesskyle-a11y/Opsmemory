#!/usr/bin/env python3
"""Run the OpsMemory SOP suggestion detector once.

Walks completed tasks across the lookback window, clusters
similar tasks per business by Jaccard over normalized summary
tokens, and either logs the candidate suggestions (dry-run)
or persists them into sop_suggestions (commit mode).

Chunk 13 commit 1 ships dry-run + commit modes (the latter is
gated on --commit because Codex's smallest-first-commit shape
explicitly listed "include dry-run output good enough to judge
false positives" — operators read the dry-run before flipping
the switch).

Usage:
    python3 scripts/run_sop_suggester.py
        [--business <slug>]
        [--lookback-months N]   (default 24)
        [--threshold 0.55]      (Jaccard cutoff)
        [--commit]              (write rows; requires non-empty
                                  results from dry-run pass)

Environment:
    DATABASE_URL                runtime DSN

Exit codes:
    0  success (clusters discovered or none found)
    1  configuration error
    2  partial failure (per-cluster errors)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import asyncpg  # noqa: E402

from api.app.db import register_jsonb_codec  # noqa: E402
from api.app.sop_suggester import (  # noqa: E402
    DEFAULT_JACCARD_THRESHOLD,
    DEFAULT_LOOKBACK_MONTHS,
    DEFAULT_MIN_CLUSTER_SIZE,
    DEFAULT_MIN_DISTINCT_YEARS,
    TaskRecord,
    build_suggestion_payload,
    discover_clusters,
)


log = logging.getLogger("opsmemory.run_sop_suggester")


async def _fetch_candidate_tasks(
    conn,
    *,
    business_slug: str | None,
    since: datetime,
) -> list[TaskRecord]:
    """Fetch completed tasks across the lookback window.

    Filters:
      - status = 'done'
      - deletion_state = 'active'
      - completed_at >= since
      - business slug filter when provided
    Multi-business tasks: each task surfaces once per business
    via UNION-equivalent (we GROUP BY t.id and take the first
    business). For chunk 13 commit 1 we want one suggestion
    per business, so we expand the row when a task is wired to
    multiple businesses.
    """
    where = [
        "t.deletion_state = 'active'",
        "t.status = 'done'",
        "t.completed_at IS NOT NULL",
        "t.completed_at >= $1::timestamptz",
        "b.deletion_state = 'active'",
    ]
    params: list = [since]
    if business_slug:
        params.append(business_slug)
        where.append(f"b.slug = ${len(params)}")

    sql = f"""
        SELECT t.id::text          AS id,
               t.summary            AS summary,
               t.completed_at       AS completed_at,
               b.id::text           AS business_id,
               b.slug::text         AS business_slug,
               b.name               AS business_name
          FROM tasks t
          JOIN task_businesses tb ON tb.task_id = t.id
          JOIN businesses b      ON b.id = tb.business_id
         WHERE {' AND '.join(where)}
         ORDER BY b.slug, t.completed_at DESC
         LIMIT 5000
    """
    rows = await conn.fetch(sql, *params)
    out: list[TaskRecord] = []
    for r in rows:
        rec = TaskRecord(
            id=r["id"],
            summary=r["summary"] or "",
            completed_at=r["completed_at"],
            business_id=r["business_id"],
            business_slug=r["business_slug"],
            business_name=r["business_name"],
        )
        rec.post_init()
        if rec.tokens:
            out.append(rec)
    return out


async def _claim_run(conn, business_filter: str | None) -> str:
    """Insert a new sop_suggestion_runs row and return its id.
    """
    row = await conn.fetchrow(
        """
        INSERT INTO sop_suggestion_runs (status, business_filter)
        VALUES ('running', $1::text)
        RETURNING id::text AS id
        """,
        business_filter,
    )
    return row["id"]


async def _finalize_run(
    conn, *, run_id: str, status: str,
    candidates: int, created: int, skipped: int,
    error: dict | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE sop_suggestion_runs
           SET status = $2,
               completed_at = now(),
               candidates_evaluated = $3,
               suggestions_created = $4,
               suggestions_skipped_existing = $5,
               error = COALESCE($6::jsonb, error)
         WHERE id = $1::uuid
        """,
        run_id, status, candidates, created, skipped, error or {},
    )


async def main_async(args: argparse.Namespace) -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL must be set", file=sys.stderr)
        return 1

    since = datetime.now(timezone.utc) - timedelta(days=30 * args.lookback_months)

    pool = await asyncpg.create_pool(
        dsn=dsn, min_size=1, max_size=2, setup=register_jsonb_codec,
    )
    candidates_evaluated = 0
    suggestions_created = 0
    suggestions_skipped_existing = 0
    errors = 0
    try:
        async with pool.acquire() as conn:
            run_id = await _claim_run(conn, args.business) if args.commit else None

            tasks = await _fetch_candidate_tasks(
                conn, business_slug=args.business, since=since,
            )
            if not tasks:
                print(f"[DRY-RUN] no completed tasks in lookback window "
                      f"({args.lookback_months} months); business="
                      f"{args.business or 'all'}")
                if run_id:
                    await _finalize_run(
                        conn, run_id=run_id, status="completed",
                        candidates=0, created=0, skipped=0,
                    )
                return 0

            clusters = discover_clusters(
                tasks,
                jaccard_threshold=args.threshold,
                min_cluster_size=DEFAULT_MIN_CLUSTER_SIZE,
                min_distinct_years=DEFAULT_MIN_DISTINCT_YEARS,
            )
            candidates_evaluated = len(clusters)

            if not clusters:
                print(f"[DRY-RUN] no clusters detected from "
                      f"{len(tasks)} candidate tasks "
                      f"(threshold={args.threshold}, "
                      f"min_cluster_size={DEFAULT_MIN_CLUSTER_SIZE}, "
                      f"min_distinct_years={DEFAULT_MIN_DISTINCT_YEARS})")
                if run_id:
                    await _finalize_run(
                        conn, run_id=run_id, status="completed",
                        candidates=0, created=0, skipped=0,
                    )
                return 0

            tag = "[COMMIT]" if args.commit else "[DRY-RUN]"
            for cluster in clusters:
                try:
                    payload = build_suggestion_payload(cluster)
                except Exception:
                    errors += 1
                    log.exception("sop_suggester_render_error",
                                  extra={"business_slug": cluster.business_slug,
                                          "month_bucket": cluster.month_bucket})
                    continue

                if args.commit and run_id:
                    try:
                        result = await conn.fetchrow(
                            """
                            INSERT INTO sop_suggestions
                              (business_id, proposed_name,
                               proposed_description,
                               seed_task_ids, proposed_template,
                               cluster_signature, rationale,
                               suggestion_run_id)
                            VALUES
                              ($1::uuid, $2::text, $3::text,
                               $4::uuid[], $5::jsonb,
                               $6::text, $7::jsonb, $8::uuid)
                            ON CONFLICT (cluster_signature)
                              DO NOTHING
                            RETURNING id::text AS id
                            """,
                            payload["business_id"],
                            payload["proposed_name"],
                            payload["proposed_description"],
                            payload["seed_task_ids"],
                            payload["proposed_template"],
                            payload["cluster_signature"],
                            payload["rationale"],
                            run_id,
                        )
                        if result is None:
                            suggestions_skipped_existing += 1
                            print(
                                f"[CLAIM-SKIP] business={cluster.business_slug} "
                                f"month={cluster.month_bucket} "
                                f"signature={payload['cluster_signature'][:16]}... "
                                f"reason=already_exists"
                            )
                            continue
                        suggestions_created += 1
                        suggestion_id = result["id"]
                    except Exception:
                        errors += 1
                        log.exception("sop_suggester_insert_error", extra={
                            "cluster_signature": payload["cluster_signature"],
                            "business_slug": cluster.business_slug,
                        })
                        continue
                else:
                    suggestion_id = "-"

                # Greppable per-cluster line.
                tokens_preview = ",".join(
                    sorted(cluster.representative_tokens)
                )[:60]
                print(
                    f"{tag} business={cluster.business_slug} "
                    f"month={cluster.month_bucket:02d} "
                    f"tasks={len(cluster.tasks)} "
                    f"avg_jaccard={cluster.avg_jaccard:.2f} "
                    f"id={suggestion_id} "
                    f"signature={payload['cluster_signature'][:16]}... "
                    f"name={payload['proposed_name']!r} "
                    f"tokens={tokens_preview!r}"
                )

            if run_id:
                await _finalize_run(
                    conn, run_id=run_id,
                    status="completed" if errors == 0 else "failed",
                    candidates=candidates_evaluated,
                    created=suggestions_created,
                    skipped=suggestions_skipped_existing,
                )
        tag = "[COMMIT]" if args.commit else "[DRY-RUN]"
        print(
            f"{tag} candidates={candidates_evaluated} "
            f"created={suggestions_created} "
            f"skipped_existing={suggestions_skipped_existing} "
            f"errors={errors}"
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
        "--lookback-months", type=int, default=DEFAULT_LOOKBACK_MONTHS,
        help=f"how far back to scan completed tasks (default {DEFAULT_LOOKBACK_MONTHS})",
    )
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_JACCARD_THRESHOLD,
        help=f"Jaccard similarity cutoff (0..1; default {DEFAULT_JACCARD_THRESHOLD})",
    )
    parser.add_argument(
        "--commit", action="store_true", default=False,
        help="persist suggestions into sop_suggestions and create a "
             "sop_suggestion_runs audit row. Without this flag the "
             "runner stays in dry-run.",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

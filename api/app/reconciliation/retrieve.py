"""Step 3: retrieve candidate matches from the existing tasks table.

Deterministic. Given a normalized candidate (business + owner + due +
summary), surface the top-N tasks that COULD be the same logical task.
The choose step (LLM) then picks among them.

Ranking signals:
    business filter    must match at least one of candidate.businesses
    owner filter       prefer tasks assigned to the same display_name
    time window        +/- 30 days from candidate.due_at, if both have one
    lexical similarity Postgres trigram on summary text

We deliberately do NOT use embedding similarity here — chunk 3 lands
without pgvector. Lexical match + structured filters is a strong
baseline. Embedding retrieval slots in later as an additional ranker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# Top-N candidates to surface to the choose step. Higher numbers cost
# more LLM tokens; lower numbers may miss the right match.
DEFAULT_TOP_N = 5


async def retrieve_candidates(
    conn,
    candidate: dict,
    *,
    top_n: int = DEFAULT_TOP_N,
    business_id_by_slug: dict[str, str] | None = None,
    actor_business_ids: list[str] | None = None,
    due_window_days: int = 30,
    recency_fallback_days: int | None = None,
) -> tuple[list[dict], bool]:
    """Return (matches, retrieval_skipped).

    `actor_business_ids` is the list of business UUIDs the ingest actor
    is allowed to see (None = admin/system, no scoping; [] = no
    visibility). Retrieval intersects candidate.businesses with the
    actor's visible set so an owner cannot surface tasks from a business
    they don't belong to. The intersection is enforced even though the
    candidate hint came from the actor's own content — the LLM may
    extract business names that the actor lacks membership for.

    Returns:
      ([...], False) — retrieval ran (with valid business filter); the
                       list may legitimately be empty (no matches).
      ([],   True)  — retrieval was skipped (no business hint, or actor
                       has no visibility into the candidate's businesses).
                       The choose step uses this to distinguish "no match"
                       from "couldn't search."
    """
    business_slugs = candidate.get("businesses") or []
    if not business_slugs:
        # No business hint extracted; skip retrieval (choose step treats
        # this as AMBIGUOUS, not CREATE — see choose.py).
        return [], True

    # Map slugs -> ids if not given. This prepared map is what the
    # pipeline orchestrator caches across candidates.
    if business_id_by_slug is None:
        rows = await conn.fetch("SELECT id::text AS id, slug::text AS slug FROM businesses")
        business_id_by_slug = {r["slug"]: r["id"] for r in rows}
    biz_ids = [business_id_by_slug[s] for s in business_slugs if s in business_id_by_slug]
    if not biz_ids:
        return [], True

    # Actor scoping: intersect with what the actor can see.
    if actor_business_ids is not None:
        visible = set(actor_business_ids)
        biz_ids = [bid for bid in biz_ids if bid in visible]
        if not biz_ids:
            # Actor cannot see any business referenced by this candidate.
            # Treat as skipped — validation will surface the authz error
            # so the reviewer (admin) sees it.
            return [], True

    # Optional time window: ± due_window_days from candidate.due_at, if set.
    # When candidate has no due_at, fall back to a recency window on
    # tasks.last_activity_at (used by Slack — present-tense messages
    # shouldn't match against tasks dormant for years).
    due_iso = candidate.get("due_at")
    time_low: datetime | None = None
    time_high: datetime | None = None
    recency_low: datetime | None = None
    if due_iso:
        try:
            due_dt = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
            time_low = due_dt - timedelta(days=due_window_days)
            time_high = due_dt + timedelta(days=due_window_days)
        except (ValueError, TypeError):
            pass
    elif recency_fallback_days is not None:
        recency_low = datetime.now(timezone.utc) - timedelta(days=recency_fallback_days)

    # Lexical match. Use simple `ILIKE %word%` per token to keep this
    # working without a trigram extension. When pg_trgm is added later,
    # swap to similarity().
    summary = (candidate.get("summary") or "").strip()
    tokens = [t for t in summary.lower().split() if len(t) >= 3][:8]

    where: list[str] = ["t.deletion_state = 'active'"]
    params: list[Any] = []
    pidx = 0

    def p(value: Any) -> str:
        nonlocal pidx
        pidx += 1
        params.append(value)
        return f"${pidx}"

    where.append(
        f"EXISTS (SELECT 1 FROM task_businesses tb WHERE tb.task_id = t.id "
        f"AND tb.business_id::text = ANY({p(biz_ids)}::text[]))"
    )

    if time_low and time_high:
        where.append(
            f"(t.due_at IS NULL OR (t.due_at >= {p(time_low.isoformat())}::timestamptz "
            f"AND t.due_at <= {p(time_high.isoformat())}::timestamptz))"
        )
    elif recency_low is not None:
        # No due hint, source set a recency fallback (Slack 14d).
        # last_activity_at is non-null on every task, so no IS NULL escape.
        where.append(
            f"t.last_activity_at >= {p(recency_low.isoformat())}::timestamptz"
        )

    if tokens:
        # Score by token-match count; rank highest first.
        token_score_clauses = " + ".join(
            f"CASE WHEN lower(t.summary) LIKE {p('%' + tok + '%')} THEN 1 ELSE 0 END"
            for tok in tokens
        )
        score_expr = f"({token_score_clauses})"
    else:
        score_expr = "0"

    sql = f"""
        SELECT t.id::text AS id, t.summary, t.status::text AS status,
               t.due_at::text AS due_at, t.dependency_text,
               t.last_activity_at::text AS last_activity_at,
               t.version, {score_expr} AS lex_score,
               array_agg(DISTINCT b.slug::text) AS businesses
        FROM tasks t
        LEFT JOIN task_businesses tb ON tb.task_id = t.id
        LEFT JOIN businesses b ON b.id = tb.business_id
        WHERE {' AND '.join(where)}
        GROUP BY t.id
        HAVING {score_expr} > 0 OR {len(tokens)} = 0
        ORDER BY lex_score DESC, t.last_activity_at DESC NULLS LAST
        LIMIT {p(top_n)}
    """

    rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows], False

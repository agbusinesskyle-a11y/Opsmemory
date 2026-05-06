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
) -> list[dict]:
    """Return up to `top_n` existing tasks the candidate might match."""
    business_slugs = candidate.get("businesses") or []
    if not business_slugs:
        # Without any business hint, the candidate could be in any of our
        # businesses. Skip retrieval (let the choose step decide CREATE).
        return []

    # Map slugs -> ids if not given. This prepared map is what the
    # pipeline orchestrator caches across candidates.
    if business_id_by_slug is None:
        rows = await conn.fetch("SELECT id::text AS id, slug::text AS slug FROM businesses")
        business_id_by_slug = {r["slug"]: r["id"] for r in rows}
    biz_ids = [business_id_by_slug[s] for s in business_slugs if s in business_id_by_slug]
    if not biz_ids:
        return []

    # Optional time window: +/- 30 days from candidate.due_at, if set.
    due_iso = candidate.get("due_at")
    time_low: datetime | None = None
    time_high: datetime | None = None
    if due_iso:
        try:
            due_dt = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
            time_low = due_dt - timedelta(days=30)
            time_high = due_dt + timedelta(days=30)
        except (ValueError, TypeError):
            pass

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
    return [dict(r) for r in rows]

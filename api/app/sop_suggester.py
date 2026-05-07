"""OpsMemory SOP suggestion engine.

Year-over-year pattern detection on completed tasks. Surfaces
clusters as draft SOP suggestions for operator review.

Strategy (per Codex chunk-13 plan-review):
  - Jaccard similarity over normalized summary tokens. Cheap,
    no LLM cost, no embedding round-trip. Cosine over
    task_embeddings is a future optimization if recall is bad.
  - Cluster condition: ≥2 tasks in different calendar years,
    same month bucket, same business, Jaccard > threshold.
  - Idempotent re-runs via cluster_signature (UNIQUE column on
    sop_suggestions).

This module is the detector + cluster-render pipeline. The
runner (scripts/run_sop_suggester.py) calls discover_clusters
+ build_suggestion_payload, then writes notification rows.

NOTE on auto-promote: this module NEVER creates an sops row.
The operator promotes via the API + PWA in commit 2.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Iterable


log = logging.getLogger("opsmemory.sop_suggester")


# Token normalization. Trim punctuation, lowercase, drop very
# common English stopwords that don't carry task-specific
# meaning. Kept minimal — adding too many stopwords reduces
# recall.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "in", "is", "it", "of", "on", "or", "the", "to",
    "with",
})

# Default similarity + cluster knobs. Override per-run via the
# runner's CLI / env if a deploy needs tuning.
DEFAULT_JACCARD_THRESHOLD = 0.55
DEFAULT_LOOKBACK_MONTHS = 24
DEFAULT_MIN_CLUSTER_SIZE = 2
DEFAULT_MIN_DISTINCT_YEARS = 2


def _normalize_tokens(text: str | None) -> frozenset[str]:
    """Lowercase, strip punctuation, drop stopwords, dedupe."""
    if not text:
        return frozenset()
    raw = _TOKEN_RE.findall(text.lower())
    return frozenset(t for t in raw if t and t not in _STOPWORDS and len(t) >= 2)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


@dataclass
class TaskRecord:
    """Lightweight in-memory task representation. The runner
    populates these from a SQL fetch.
    """
    id: str
    summary: str
    completed_at: datetime
    business_id: str
    business_slug: str
    business_name: str
    due_offset_days: int | None = None  # filled by detector if available
    tokens: frozenset[str] = field(default_factory=frozenset)
    month: int = 0
    year: int = 0

    def post_init(self) -> None:
        self.tokens = _normalize_tokens(self.summary)
        self.month = self.completed_at.month
        self.year = self.completed_at.year


@dataclass
class Cluster:
    business_id: str
    business_slug: str
    business_name: str
    month_bucket: int            # 1..12
    tasks: list[TaskRecord]
    avg_jaccard: float
    representative_tokens: frozenset[str]


def _cluster_signature(business_id: str, month_bucket: int, tokens: frozenset[str]) -> str:
    """Codex chunk-13 plan-review: deterministic hash of
    (business + month + token signature). Re-detection finds
    the same row via UNIQUE(cluster_signature).
    """
    sorted_tokens = ",".join(sorted(tokens))
    raw = f"{business_id}|{month_bucket:02d}|{sorted_tokens}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:64]  # 64 hex chars; CHECK length 16..128 in schema


def _summary_to_template_summary(t: TaskRecord) -> str:
    """Best-effort canonical summary for the SOP template. Picks
    the longest summary in the cluster as the representative.
    Caller passes the cluster; this helper expects one task.
    """
    return (t.summary or "").strip()[:256]


def _propose_name(cluster: Cluster) -> str:
    """Generate a draft SOP name from the cluster's representative
    tokens. Operator can edit before promotion.
    """
    # Pick the longest task summary as the name basis (operators
    # tend to write more descriptive summaries when the task is
    # the first instance of a recurring pattern).
    by_len = sorted(cluster.tasks, key=lambda t: len(t.summary or ""), reverse=True)
    base = by_len[0].summary if by_len else "Recurring task"
    return base.strip()[:256] or "Recurring task"


def _propose_description(cluster: Cluster) -> str:
    """Generate a draft description summarizing why we surfaced
    the cluster.
    """
    years = sorted({t.year for t in cluster.tasks})
    years_str = ", ".join(str(y) for y in years)
    return (
        f"Auto-suggested from {len(cluster.tasks)} similar tasks "
        f"completed in {cluster.business_name} during month "
        f"{cluster.month_bucket} of {years_str}. Edit before "
        f"promoting."
    )


def _propose_template(cluster: Cluster, anchor_day: int = 1) -> list[dict]:
    """Render the cluster into a draft sop_template_tasks shape.

    Each cluster member becomes one template task. Per Codex
    plan-review: due_offset_days = median day-of-month - anchor.
    """
    days = [t.completed_at.day for t in cluster.tasks]
    if not days:
        return []
    median_day = int(median(days))
    offset = median_day - anchor_day

    # Deduplicate template summaries — if every task has the
    # exact same summary, we still want ONE template entry,
    # not N.
    seen: set[str] = set()
    items: list[dict] = []
    for t in sorted(cluster.tasks, key=lambda x: (x.year, x.month, x.completed_at.day)):
        summary = _summary_to_template_summary(t)
        if summary.lower() in seen:
            continue
        seen.add(summary.lower())
        items.append({
            "summary": summary,
            "description": None,
            "due_offset_days": offset,
            "dependency_text": None,
            "category": None,
            "owner_role": None,
        })
    return items


def discover_clusters(
    tasks: list[TaskRecord],
    *,
    jaccard_threshold: float = DEFAULT_JACCARD_THRESHOLD,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    min_distinct_years: int = DEFAULT_MIN_DISTINCT_YEARS,
) -> list[Cluster]:
    """Group `tasks` into clusters that satisfy:
      - same business
      - same calendar month
      - ≥ min_cluster_size members
      - members span ≥ min_distinct_years
      - average pairwise Jaccard > jaccard_threshold

    Caller must populate task.tokens / month / year before
    calling (TaskRecord.post_init helper).

    Returns clusters sorted by (business_slug, month). The
    grouping is greedy: each task ends up in at most one
    cluster (the first it joins). Good enough for chunk 13;
    swap for hierarchical clustering if recall demands it.
    """
    # Bucket by (business, month). Within a bucket, do a
    # greedy single-link join using Jaccard.
    bucketed: dict[tuple[str, int], list[TaskRecord]] = {}
    for t in tasks:
        bucketed.setdefault((t.business_id, t.month), []).append(t)

    clusters: list[Cluster] = []
    for (biz_id, month), members in bucketed.items():
        if len(members) < min_cluster_size:
            continue
        used: set[int] = set()
        for i, seed in enumerate(members):
            if i in used:
                continue
            group = [seed]
            group_idx = {i}
            for j in range(i + 1, len(members)):
                if j in used:
                    continue
                cand = members[j]
                avg = sum(
                    _jaccard(g.tokens, cand.tokens) for g in group
                ) / len(group)
                if avg >= jaccard_threshold:
                    group.append(cand)
                    group_idx.add(j)
            if len(group) < min_cluster_size:
                continue
            distinct_years = {t.year for t in group}
            if len(distinct_years) < min_distinct_years:
                continue
            avg_pairwise = _avg_pairwise_jaccard(group)
            rep_tokens = _representative_tokens(group)
            clusters.append(Cluster(
                business_id=biz_id,
                business_slug=group[0].business_slug,
                business_name=group[0].business_name,
                month_bucket=month,
                tasks=group,
                avg_jaccard=avg_pairwise,
                representative_tokens=rep_tokens,
            ))
            used |= group_idx

    clusters.sort(key=lambda c: (c.business_slug, c.month_bucket))
    return clusters


def _avg_pairwise_jaccard(group: list[TaskRecord]) -> float:
    if len(group) < 2:
        return 0.0
    total = 0.0
    pairs = 0
    for i in range(len(group)):
        for j in range(i + 1, len(group)):
            total += _jaccard(group[i].tokens, group[j].tokens)
            pairs += 1
    return total / pairs if pairs else 0.0


def _representative_tokens(group: list[TaskRecord]) -> frozenset[str]:
    """Tokens that appear in MOST cluster members. Used by
    cluster_signature so re-detection finds the same cluster
    even when a new noise word slips into one task summary.
    """
    counts: dict[str, int] = {}
    for t in group:
        for tok in t.tokens:
            counts[tok] = counts.get(tok, 0) + 1
    threshold = max(2, len(group) // 2 + 1)  # majority
    return frozenset(t for t, c in counts.items() if c >= threshold)


def build_suggestion_payload(cluster: Cluster) -> dict:
    """Render a cluster into the dict shape the runner inserts
    into sop_suggestions.
    """
    tokens = cluster.representative_tokens or _representative_tokens(cluster.tasks)
    signature = _cluster_signature(
        cluster.business_id, cluster.month_bucket, tokens,
    )
    return {
        "business_id": cluster.business_id,
        "proposed_name": _propose_name(cluster),
        "proposed_description": _propose_description(cluster),
        "seed_task_ids": [t.id for t in cluster.tasks],
        "proposed_template": _propose_template(cluster),
        "cluster_signature": signature,
        "rationale": {
            "strategy": "jaccard_v1",
            "month_bucket": cluster.month_bucket,
            "task_count": len(cluster.tasks),
            "distinct_years": sorted({t.year for t in cluster.tasks}),
            "avg_jaccard": round(cluster.avg_jaccard, 3),
            "representative_tokens": sorted(tokens),
        },
    }

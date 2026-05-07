# SOP suggestions runbook (Chunk 13)

> Operator contract for the year-over-year pattern detector.
> Surfaces clusters of similar completed tasks across years
> as DRAFT SOP suggestions. Operator promotes to a real
> draft SOP via the admin API; the existing chunk-7 publish
> path owns activation. Detector NEVER auto-promotes.

## Overview

```
                       ┌──────────────────────────────────┐
                       │ scripts/run_sop_suggester.py     │
                       │ (operator-triggered, --commit)   │
                       └───────────────┬──────────────────┘
                                       │ 1. fetch completed tasks
                                       │ 2. cluster by Jaccard /
                                       │    business / month / year
                                       │ 3. build draft template
                                       │ 4. INSERT INTO sop_suggestions
                                       │    ON CONFLICT DO NOTHING
                                       ▼
                            ┌──────────────────────────┐
                            │ sop_suggestions table    │
                            │  status='pending'        │
                            └────────────┬─────────────┘
                                         │ admin reviews via API
                                         ▼
                            ┌──────────────────────────┐
                            │ POST /accept             │
                            │   creates fresh sops +   │
                            │   sop_versions(draft) +  │
                            │   sop_template_tasks     │
                            └────────────┬─────────────┘
                                         │
                                         ▼
                            ┌──────────────────────────┐
                            │ Existing chunk-7 admin   │
                            │ SOPs tab: edit + publish │
                            └──────────────────────────┘
```

## Detection model

**Strategy**: Jaccard over normalized summary tokens. No LLM,
no embedding round-trip. The detector groups completed tasks
by `(business, calendar month)`; within each group it does
greedy single-link clustering. A cluster ships as a suggestion
when it has:

- ≥ 2 members
- Members spanning ≥ 2 distinct years
- Average pairwise Jaccard > threshold (default 0.55)

**Idempotency**: every cluster is hashed into a
`cluster_signature` (SHA-256 of `business_id | month |
sorted_representative_tokens`). The `sop_suggestions` UNIQUE
constraint on this column means re-running the detector is a
no-op for already-known clusters. Dismissed-cluster signatures
remain in the table so re-detection skips them.

## One-time setup

### 1. Migration

```bash
python3 scripts/migrate.py
```

Migration `0018_sop_suggestions.sql` creates `sop_suggestions`
+ `sop_suggestion_runs`.

### 2. (Optional) Tune detector knobs

CLI flags override defaults per-run:

```bash
python3 scripts/run_sop_suggester.py \
  --lookback-months 24 \
  --threshold 0.55 \
  --business redhot
```

For a small dataset, drop `--threshold` to 0.45 to widen recall.

## Operating

### Dry-run (default)

```bash
python3 scripts/run_sop_suggester.py
```

Prints one line per detected cluster. NO DB writes. Read these
to judge false-positive rate before flipping to commit mode.

```
[DRY-RUN] business=redhot month=05 tasks=3 avg_jaccard=0.78 \
          id=- signature=27ab69d3cd21... \
          name='Order containers May opening' \
          tokens='containers,may,opening,order'
```

### Commit (writes rows)

```bash
python3 scripts/run_sop_suggester.py --commit
```

For each cluster:
- INSERT INTO sop_suggestions ON CONFLICT DO NOTHING.
- Skipped clusters log `[CLAIM-SKIP] reason=already_exists`.
- A `sop_suggestion_runs` audit row is created with rollup
  counts.

Re-running `--commit` immediately after another commit run
produces only `[CLAIM-SKIP]` lines (idempotent).

### Per-run summary

```
[COMMIT] candidates=8 created=5 skipped_existing=3 errors=0
```

## Admin API

All endpoints require admin user principal. Service principals
get 403.

```
GET    /v1/sop_suggestions?business_slug=X&status=pending
GET    /v1/sop_suggestions/{id}              (full payload incl.
                                                proposed_template)
POST   /v1/sop_suggestions/{id}/accept
POST   /v1/sop_suggestions/{id}/dismiss
```

### Accept a suggestion

```bash
curl -X POST https://tracker.kyleconway.ai/v1/sop_suggestions/<id>/accept \
  -H "Content-Type: application/json" \
  -d '{
    "edited_name": "RedHot May opening prep",
    "edited_description": "Annual May opening checklist.",
    "edited_template": [
      {"summary": "Order containers", "due_offset_days": 0},
      {"summary": "Sweep parking lot", "due_offset_days": 5}
    ]
  }'
```

Response:

```json
{
  "id": "<suggestion id>",
  "status": "accepted",
  "promoted_sop_id": "<new sops.id>",
  "draft_sop_version_id": "<sop_versions.id>"
}
```

The draft SOP is now visible in the chunk-7 SOPs admin tab.
Edit + publish from there to activate.

### Dismiss a suggestion

```bash
curl -X POST https://tracker.kyleconway.ai/v1/sop_suggestions/<id>/dismiss \
  -H "Content-Type: application/json" \
  -d '{"reason": "These were one-offs, not a recurring SOP."}'
```

Dismissed signatures stay in the table so the detector skips
the same cluster on re-run.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `[DRY-RUN] no completed tasks` | No tasks done in lookback window | Increase `--lookback-months` or seed data. |
| `[DRY-RUN] no clusters detected` | Threshold too high | Drop `--threshold` to 0.45 and re-run. |
| Same cluster surfaces every run with `[CLAIM-SKIP] already_exists` | Operator hasn't accepted or dismissed yet | Use the API to act on the pending row. |
| accept returns 422 `template_required` | edited_template empty AND proposed_template empty | Manually craft a template via `edited_template`. |
| accept returns 409 `suggestion_not_pending` | Already accepted or dismissed | List suggestions with `?status=all` to confirm. |

## Decommissioning

Detector is operator-triggered (no systemd timer in chunk 13),
so decommissioning is just "stop running the script." Existing
suggestions remain; admin can dismiss in bulk:

```sql
UPDATE sop_suggestions
   SET status = 'dismissed',
       dismissed_reason = 'system retired'
 WHERE status = 'pending';
```

## Code paths

- `api/migrations/0018_sop_suggestions.sql` — schema
- `api/app/sop_suggester.py` — pure detector + cluster helpers
- `api/app/v1_sop_suggestions.py` — admin API
- `scripts/run_sop_suggester.py` — operator CLI entry point

## Future extensions (deferred from chunk 13)

- PWA "Suggestions" drawer in the SOPs admin tab (currently
  operator uses curl). Plumbing already exists in `web/app.js`
  for SOPs admin; adding a suggestions section is a localized
  edit.
- LLM cluster scoring + re-ranking (only if Jaccard recall
  proves insufficient).
- Cosine similarity over `task_embeddings` (chunk 2 schema)
  for semantic clustering.
- systemd timer for nightly runs.
- Promote-into-existing-SOP-version flow (currently only
  promote-into-new-SOP).

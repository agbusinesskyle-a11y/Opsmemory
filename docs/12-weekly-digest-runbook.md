# Weekly Gmail digest runbook (Chunk 11)

> Operator contract for the per-business weekly Gmail digest. One
> Gmail draft per business per week, summarizing that business's
> open + completed-this-week + stale tasks. **Drafts only — never
> auto-sends** (per docs/01-design.md locked decision).

## Overview

```
                       ┌──────────────────────────────────┐
                       │ scripts/run_weekly_digest.py     │
                       │ (systemd timer, Mon 8am Phoenix) │
                       └───────────────┬──────────────────┘
                                       │ 1. resolve last completed week
                                       │ 2. for each business with allowlist:
                                       │    - fetch open / completed / stale
                                       │    - build payload (subject+html+text)
                                       │    - INSERT weekly_digest_runs
                                       │      (idempotency_key)
                                       ▼
                            ┌──────────────────────────┐
                            │  httpx → n8n webhook     │
                            │  (auto.kyleconway.ai)    │
                            │  → Gmail drafts.create   │
                            └────────────┬─────────────┘
                                         │  draft_id
                                         ▼
                            ┌──────────────────────────┐
                            │  Operator's Gmail        │
                            │  Drafts folder           │
                            │  (review + send manually)│
                            └──────────────────────────┘
```

## One-time setup

### 1. Migrations

Migration `0017_weekly_digest.sql` creates two tables:
- `weekly_digest_allowlist` (per-business recipient list with
  `to`/`cc`/`bcc` roles)
- `weekly_digest_runs` (audit log + idempotency table)

```bash
python3 scripts/migrate.py
```

### 2. n8n webhook

Create an n8n workflow on `auto.kyleconway.ai`:

```
[Webhook trigger: POST /webhook/opsmemory-weekly, Header Auth Bearer]
    ↓
[Gmail node: drafts.create with To/Cc/Bcc + html + text]
    ↓
[Respond to webhook: 200 with { draft_id, gmail_draft_url }]
```

Required webhook fields (from OpsMemory):
- `business_slug`, `business_name`, `run_id`
- `subject`, `html_body`, `text_body`
- `to: []`, `cc: []`, `bcc: []`
- `counts: {open, completed, stale}`
- `week_start_iso`, `week_end_iso`, `generated_at`

n8n must echo back `draft_id` in its 200 response so OpsMemory
can capture it in `weekly_digest_runs.draft_id` for audit.

Set the bearer secret in OpsMemory's `.env`:

```
N8N_GMAIL_DIGEST_WEBHOOK_URL=https://auto.kyleconway.ai/webhook/opsmemory-weekly
N8N_GMAIL_DIGEST_WEBHOOK_BEARER=<32-byte random>
```

### 3. Seed the allowlist

Per business, add at least one recipient (curl as an admin user
through Cloudflare Access, or psql directly):

```bash
curl -X POST https://tracker.kyleconway.ai/v1/weekly_digest/allowlist \
  -H "Content-Type: application/json" \
  -d '{"business_slug": "borderline",
        "recipient_email": "joanna@example.com",
        "role": "to",
        "notes": "Borderline primary inbox"}'

curl -X POST https://tracker.kyleconway.ai/v1/weekly_digest/allowlist \
  -H "Content-Type: application/json" \
  -d '{"business_slug": "borderline",
        "recipient_email": "kyle@example.com",
        "role": "cc",
        "notes": "CC for awareness"}'
```

A business with no allowlist entries is silently skipped by the
runner (no draft, no row).

### 4. Systemd timer

`/etc/systemd/system/opsmemory-weekly-digest.service`:

```ini
[Unit]
Description=OpsMemory weekly Gmail digest builder
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/opsmemory
EnvironmentFile=/opt/opsmemory/.env
ExecStart=/opt/opsmemory/.venv/bin/python scripts/run_weekly_digest.py --send
User=opsmemory
```

`/etc/systemd/system/opsmemory-weekly-digest.timer`:

```ini
[Unit]
Description=Run OpsMemory weekly digest Mondays 8am Phoenix

[Timer]
# 15:00 UTC = 8:00 America/Phoenix (no DST)
OnCalendar=Mon 15:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now opsmemory-weekly-digest.timer
```

`Persistent=true` means a missed run (server reboot) catches up
on next boot.

## Operating

### Run modes

```bash
# Default: walk + build payload, no DB writes, no n8n call.
python3 scripts/run_weekly_digest.py

# Restrict to one business + a specific week:
python3 scripts/run_weekly_digest.py --business borderline --week 2026-05-04

# Production cron uses --send.
python3 scripts/run_weekly_digest.py --send

# Force dry-run regardless of flag (kill switch).
NOTIFICATIONS_DRY_RUN=1 python3 scripts/run_weekly_digest.py --send
```

### Per-row log line

```
[SEND] week=2026-05-04..2026-05-10 business=borderline \
       to=1 cc=1 bcc=0 open=4 completed=3 stale=1 \
       key=weekly_digest:borderline:2026-05-04 \
       run_id=<uuid> send=sent http=200 code=sent \
       draft_id=r-1234567890123456789 \
       subject='OpsMemory weekly: Borderline Fireworks SD for week of May 4'
```

### Per-run summary

```
[SEND] considered=2 emitted=2 sent=2 send_failed=0 send_errors=0 \
       skipped_claim=0 errors=0 week=2026-05-04..2026-05-10
```

### Status codes (mirrors chunk-10 vocabulary)

- `code=sent` — n8n returned 2xx, draft created, `draft_id` captured.
- `code=config` — n8n returned 401/403. Bearer wrong or CF Access
  misconfigured. Operator-actionable.
- `code=bad_request` — n8n returned other 4xx (e.g. Gmail rejected
  the recipient). Audit `error.detail` for the n8n response.
- `code=transient` — 5xx or network/timeout. Re-queue the same week
  with `--week YYYY-MM-DD --send` to retry.

## API surface (admin only)

All endpoints require an admin user principal. Service principals
get 403.

```
GET    /v1/weekly_digest/allowlist?business_slug=X
POST   /v1/weekly_digest/allowlist
DELETE /v1/weekly_digest/allowlist/{id}

GET    /v1/weekly_digest/runs?business_slug=X[&limit=N]
GET    /v1/weekly_digest/runs/{id}        (full payload incl.
                                            html_body)
```

There is **no** API endpoint to trigger a run. The systemd cron
is the only writer to `weekly_digest_runs`. To re-run a specific
week, use the runner CLI on Spark.

## Smoke tests

### A. Allowlist read/write

```bash
curl https://tracker.kyleconway.ai/v1/weekly_digest/allowlist \
  | jq '.items'
```

### B. Dry-run for current week

```bash
python3 scripts/run_weekly_digest.py
```

Expect a `[DRY-RUN]` line per business with at least one
allowlist entry. No DB writes.

### C. Targeted dry-run for last week

```bash
python3 scripts/run_weekly_digest.py --business borderline --week 2026-05-04
```

Renders the actual subject + counts that would have shipped.

### D. Production send + Gmail check

```bash
python3 scripts/run_weekly_digest.py --send
```

Then:
1. SQL check: row in `weekly_digest_runs` with `status='sent'`,
   `draft_id` populated.
2. Gmail check: open the operator's Drafts folder, confirm a
   draft titled "OpsMemory weekly: ... for week of ..." with the
   right recipients in To/Cc/Bcc.
3. Re-run: second invocation logs `[CLAIM-SKIP]
   reason=already_claimed` and produces no second draft.

### E. Audit query

```sql
SELECT id, business_id, week_start_iso, status,
       sent_at, failed_at,
       error->>'code' AS code,
       payload->'counts' AS counts,
       draft_id
  FROM weekly_digest_runs
 WHERE created_at > now() - interval '14 days'
 ORDER BY created_at DESC;
```

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Runner exits 1 `gmail sender preflight failed` | `N8N_GMAIL_DIGEST_WEBHOOK_URL` unset | Set it (see § One-time setup #2). |
| `[CLAIM-SKIP] reason=already_claimed` on every run | Row already exists for that week | Expected. Use `--week` to target a different week. |
| `code=config` audit row | Bearer wrong / CF Access broken | Re-mint the bearer; verify CF Access policy. |
| `code=bad_request` from n8n | Gmail rejected payload (bad email, unsubscribed, etc.) | Inspect `error.detail`; remove the bad recipient from allowlist. |
| `code=transient` audit row | 5xx from n8n / network blip | Re-run same `--week` with `--send`. The UNIQUE constraint will block until the row is updated to a non-`scheduled` status. |
| Empty business sends "No activity" draft | Expected | Operator can choose to delete the draft from Gmail. |
| Some tasks missing | Visibility scope: `task_businesses` filter; `deletion_state='active'`; status filter | Check task is wired to the business and not soft-deleted. |
| Multi-assignee task showing "Joanna, Kyle" | Expected | The runner aggregates all assignees into a comma-joined list. |

## Decommissioning

1. Stop the timer:
   ```bash
   sudo systemctl disable --now opsmemory-weekly-digest.timer
   ```
2. (Optional) Clear the allowlist:
   ```sql
   DELETE FROM weekly_digest_allowlist;
   ```
3. Unset `N8N_GMAIL_DIGEST_WEBHOOK_URL`. Subsequent runner
   invocations preflight-fail with exit 1 and never touch the DB.

## Code paths

- `api/migrations/0017_weekly_digest.sql` — schema
- `api/app/notifications/weekly_digest.py` — pure payload builder
- `api/app/notifications/gmail_sender.py` — n8n bridge
- `api/app/v1_weekly_digest.py` — admin API
- `scripts/run_weekly_digest.py` — runner / cron entry point

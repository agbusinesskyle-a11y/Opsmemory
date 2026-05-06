# OpsMemory — 14-Chunk Implementation Plan

Cadence: same as Conway_Motherduckdb. After each chunk, Codex senior-engineer review of completed work AND next-chunk plan, before Kyle approves the next chunk.

## Chunk 1 — Substrate

**Scope**: DB + identity + backups + healthchecks + PWA shell deployed.

**Files**:
```
README.md
.env.example
docker-compose.yml
api/Dockerfile
api/requirements.txt
api/app/__init__.py
api/app/main.py
api/app/db.py
api/app/auth.py
api/app/health.py
api/migrations/0001_initial.sql
web/index.html
web/app.js
web/manifest.json
web/sw.js
web/icons/icon-192.png
web/icons/icon-512.png
scripts/backup_action_tracker.ps1
scripts/restore_check.ps1
docs/05-chunk1-runbook.md
```

**Acceptance criteria**:
- `action_tracker` database exists, migrations repeatable.
- 4 users + 2 businesses + 6 business_memberships seeded.
- 7 lifecycle ENUMs created (`app_role`, `user_status`, `task_lifecycle_state`, `review_lifecycle_state`, `ingest_lifecycle_state`, `notification_lifecycle_state`, `deletion_lifecycle_state`).
- API binds `127.0.0.1:8010` only. No public port.
- `/healthz` (liveness, no DB), `/readyz` (DB ping + migration check + optional backup-status check), `/whoami` (full principal record) all working.
- Backend validates Cloudflare Access JWT in production mode; supports `AUTH_MODE=local` for dev.
- PWA shell loads at `tracker.kyleconway.ai`. Joanna can open on phone, Add to Home Screen, launch standalone.
- Daily backup runs (`pg_dump -Fc -Z 9` + rsync to Spark #2). One manual restore test passing.
- Existing `n8n`, `openbrain`, `family_docs`, `family_health` databases untouched (verified before/after).

**Explicitly NOT in Chunk 1**: LLM, Slack, SOPs, offline outbox, encryption, B2 offsite.

**Deferred to Chunk 1.5**:
- Backblaze B2 offsite leg (3rd backup copy)
- GPG encryption of dumps before they leave home network
- Weekly automated restore-check timer (Chunk 1 ships manual restore test only)

---

## Chunk 2 — API + read-only dashboard

**Scope**: FastAPI skeleton with authz middleware on every mutation route. Read-only endpoints powering a real dashboard. Audit and event model in place.

**Tables added**: `tasks`, `task_history`, `task_field_versions`, `task_assignees`, `task_businesses`, `task_embeddings` (pgvector extension), `client_mutations`.

**Endpoints**: `GET /v1/tasks`, `GET /v1/tasks/:id`, `GET /v1/businesses`, `GET /v1/users` (admin only).

**Acceptance**: PWA shows real task list (manually inserted via SQL for now). Filters by owner, business, status. Field-level version vectors visible in API responses.

---

## Chunk 3 — First ingest path: meeting recap

**Scope**: 7-step reconciliation pipeline end-to-end on the meeting-recap source. LLM auto-merge OFF (Day 1-30 phase). Everything lands in `review_items`.

**Files**: migrate Co-Work parser from `C:\Meeting Recap\meeting-recap\` → `C:\opsmemory\ingest\meeting_recap\`. Add `reconciliation/` modules.

**Tables added**: `ingest_events` (full schema with status lifecycle), `review_items`, `auto_merge_policy` (seeded OFF for all sources).

**Acceptance**: paste a meeting recap, hit `/v1/ingest/meeting_recap`, see candidate `review_items` populated, verify each step's logged output.

---

## Chunk 4 — Review UI + approve/reject mutation flow

**Scope**: PWA shows pending review queue. Approve / reject / edit-then-approve flow. Approval applies the proposed mutation through validate + apply steps.

**Acceptance**: Kyle clicks approve on a pending item, task is created, audit log shows the approval mutation, `task_state_transitions` records the create.

---

## Chunk 5 — Slack ingest *(shipped — see chunk-5-close tag)*

**Scope**: Slack Events API → n8n → POST /v1/ingest/slack with service auth.
Source-scoped dedupe (meeting_recap-only partial UNIQUE on
normalized_hash; Slack idempotency on (source, source_external_id) only).
Pipeline generalization: source registry, per-source extract prompt
(slack_message_extract.v1), source-neutral task_choose.v1.
Deterministic post-extract resolver (slack_resolve.py) for
channel→business via `slack_channel_mappings` and Slack mention
→ canonical user via `user_identities(provider='slack')`.
Owner ID materialized into `task_assignees` on CREATE approve.

**Acceptance**: Slack message → ingest_event → review_item with
resolved owner + business → admin approves → task created with
assignee.

---

## Chunk 6 — PWA writes + offline + outbox *(shipped — see chunk-6-close tag)*

**Scope**: idempotent client mutation contract (`POST /v1/tasks/{id}/toggle_done`)
backed by `client_mutations` with replay-safe result_payload /
error_payload. Service worker caches app shell (cache-first) and
`/v1/tasks` list (network-first, stale-if-error on 5xx +
network-throw). IndexedDB outbox queues writes; replay on
window.online + visibilitychange + 10s backoff drainer.
Optimistic UI for toggle_done with previous_task_snapshot revert
on 409. Sync indicator badge: synced / pending / conflict / offline /
cached.

**Acceptance**: airplane mode, toggle a task, see "1 pending";
reconnect, replay flushes, see "synced". 409 surfaces inline
conflict marker with Retry / Discard.

---

## Chunk 7 — SOPs + anchor events *(next)*

**Scope**: SOP templates, anchor events, materialization, date-shift propagation, per-field human-override flag.

**Tables added**: `sops`, `sop_versions`, `sop_template_tasks`, `anchor_events`, `sop_instances`, `sop_generated_tasks`.

**Acceptance**: drop a SOP, set anchor date, fire anchor → SOP
materializes one ingest_event + N review_items via the existing
reconciliation pipeline (proposed_action='CREATE_TASK', source='sop_anchor').
Admin approve → tasks created. Move anchor date — unedited
materialized tasks shift, edited tasks stay (per-field
manually_overridden flag).

Note: original chunk plan numbered Slack as Chunk 7 and SOPs as
Chunk 9. After chunks 1-4 shipped, Slack moved to chunk 5 (closer to
the LLM pipeline foundation), and the offline outbox claimed chunk 6.
The remaining numbering shifts up: SOPs are now chunk 7.

---

## Chunk 8 — Slack query bot

**Scope**: read-only `/tasks <owner>`, `/tasks stale`, `/tasks rh-opening`.

**Acceptance**: Slack returns formatted task list within 2 seconds.

---

## Chunk 9 — Excel/file drop ingest

**Scope**: Drive folder watch via existing n8n integration. Excel/CSV parser, free-form file parser.

---

## Chunk 10 — Push notifications + settings

**Scope**: per-user notification prefs UI. Daily 7am Phoenix digest as default. Push delivery via Web Push API (PWA) and Slack DM as fallback.

**Tables added**: `notification_prefs`, `notification_deliveries`.

---

## Chunk 11 — Weekly Gmail digest drafts

**Scope**: existing `Tool: Gmail Send Borderline` n8n workflow generates a weekly draft summarizing open + completed + stale items. Recipient allowlist. Drafts only — never auto-sends.

---

## Chunk 12 — mcp-server read-only

**Scope**: MCP integration for Kyle AI Assistant queries. Tenant-scoped reads only. Prompt-injection defenses applied to all task text exposed via MCP.

---

## Chunk 13 — SOP suggestion engine

**Scope**: year-over-year pattern detection on completed tasks. "You and Joanna both completed 'order containers' in May 2025 and May 2026 — promote to RedHot Opening Prep SOP?" One-click promote.

---

## Chunk gates

Before each chunk closes:

1. All chunk acceptance criteria pass.
2. Codex senior-advisor review of the completed work AND the proposed next-chunk plan.
3. Kyle approves both review and next-chunk plan.

If any gate fails, the chunk does not close.

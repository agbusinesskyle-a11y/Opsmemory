# OpsMemory — Locked Design Decisions

Date: 2026-05-04
Status: Locked. Implementation begins after this doc commits.

This document records the 18 design decisions that govern OpsMemory. 10 came from Kyle's initial planning Q&A; 8 came from Codex's senior-engineer review with Kyle's adjudication.

---

## 10 original decisions (Q&A with Kyle)

### 1. Auth & permissions — Tiered

- Owners can complete their own tasks.
- Kyle and Joanna are admins (can complete anyone's task).
- Multi-owner ("shared") tasks closeable by any assigned owner.
- Completion is reversible by anyone with completion rights for **24 hours**. After 24h, only admins can reopen.
- Reversal is a new audit-log mutation, never a destructive undo.

### 2. SOPs — Templates with anchor events

- SOPs are templates tied to anchor events (e.g., "RedHot Opening 2026"). Setting an anchor date materializes all template tasks with concrete dates.
- Anchor date shifts propagate to materialized tasks.
- **Versioning** (Codex override): when a template changes, materialized tasks update **unedited** fields; fields a human has edited stay locked via a per-field `manually_overridden` flag.
- Old SOPs not deleted on update — version history preserved.

### 3. Retention — Soft delete forever, with admin hard-delete

- `status` (`open`/`done`) + `completed_at` + `completion_note`.
- `deleted_at`, `deleted_by`, `superseded_by_task_id` columns on tasks.
- Default views filter `deleted_at IS NOT NULL` out.
- Admin "trash" view shows soft-deleted tasks; restore endpoint clears `deleted_at`.
- After 7 days in trash, admin can hard-delete (audit-trail row retained, content gone).
- Year-over-year pattern detection (SOP suggestion engine) deferred to Chunk 14.

### 4. Status vocabulary — Two task states + five lifecycles

- Task: `open` / `done`
- Codex override: each other lifecycle gets its own ENUM, separately tracked:
  - Review: `pending` / `approved` / `rejected` / `needs_changes` / `superseded`
  - Ingest: `received` / `extracting` / `pending_review` / `completed` / `failed` / `cancelled`
  - Notification: `pending` / `scheduled` / `sent` / `failed` / `cancelled` / `suppressed`
  - Deletion: `active` / `soft_deleted` / `restore_requested` / `restored` / `hard_delete_eligible` / `hard_deleted`
- All transitions logged in a single `task_state_transitions` audit table.
- `dependency` text field on tasks captures blocked-on-X for stale detection (no formal `blocked` status).

### 5. App platform — PWA only

- Single web codebase served as PWA with `manifest.json` + service worker.
- Installable on iOS/Android/Mac/Windows from the browser. No app stores.
- Tauri + Capacitor as a future upgrade path if app-store distribution becomes needed.

### 6. Offline + concurrency — Read-cached writes; 3-layer concurrency

- Service worker caches the app shell + last-loaded `/api/state` response.
- Outbox pattern: writes queue locally first, sync when online.
- Sync indicator in UI (synced / N pending / offline).
- **Concurrency** (Codex override): three layers, not just one.
  1. **Field-level version compare** — every read includes per-field versions; writes submit base version; server rejects stale.
  2. **60-second human-touch holdoff** — LLM auto-merges blocked on tasks human-touched in last 60s.
  3. **Apply-time conflict re-check** — final UPDATE inside transaction with `WHERE field_version = $base`.
- Conflict response = `409` with current state and merge UI.

### 7. Push notifications — Per-user configurable

- Default: daily 7am Phoenix digest summarizing open + due-soon + stale items.
- Each user can dial up (assignment alerts, due-date pushes) or down (silent) in settings.
- Stale-item escalation toggle: admins can opt in to see other owners' stale items.
- Audit log records every escalation event.

### 8. Classification threshold — Balanced

- LLM extracts a task when input has actor + verb + object.
- Casual chitchat ("we talked about pumpkins") does not produce tasks.
- One threshold across all sources at launch; can split per-source later if approval rate diverges.

### 9. LLM provider — litellm chain with capability tier

- Primary: Claude Sonnet 4.6
- Fallback: GPT-4.1
- Last-resort: local Llama on Spark — **extract/summarize only, never mutates** (Codex override).
- Hard daily $ cap configured in litellm. Slack alert + queue pause on cap hit.
- During outages: Llama runs extraction, items queue in pending review for humans to approve manually. Better LLMs re-process queued events when they return.

### 10. Concurrency — Covered above (decision #6).

---

## 8 Codex-driven decisions (senior-engineer review)

### A. Identity layer — Real `users` table on top of Cloudflare Access

- Cloudflare Access proves the email is real (Google SSO IdP).
- Postgres `users` table proves that email is allowed and what role.
- `user_identities` table maps external provider subjects (CF Access, Slack) to internal user IDs.
- `service_accounts` table for n8n, doc-processor, MCP — separate auth path with `X-OpsMemory-Service-Key` header (NOT `Authorization`, which Cloudflare uses for service tokens).
- Roles: `admin` / `owner` / `service`.

### B. Concurrency depth — 3-layer

(See decision #6 above.)

### C. Llama scope — Extract/summarize only

(Folded into decision #9 above.)

### D. Lifecycle formalization — All 5 ENUMs in Chunk 1

(See decision #4 above.)

### E. Deletion model — Soft + admin hard-delete after 7 days

(See decision #3 above.)

### F. Auto-merge — Phased rollout

- **Days 1-30**: Everything lands in pending review. No auto-merge regardless of confidence. Builds the approval-rate dataset.
- **Days 30-90**: `CREATE_TASK` proposals can auto-merge above a threshold for sources where approval rate exceeded 90% in prior period. `COMPLETE_TASK` and `DELETE_TASK` always go to review.
- **Day 90+**: Per-source evaluation. `COMPLETE`/`DELETE` may earn auto-merge if approval rate justifies.
- `auto_merge_policy` table seeded with all sources OFF at launch.

### G. Backup strategy — 3-2-1 rule

- Daily `pg_dump` at 02:17 Phoenix on Spark #1.
- Rsync to Spark #2 (second copy on different machine).
- Backblaze B2 offsite (third copy, different geography).
- **Chunk 1 ships the local + Spark #2 legs only.** B2 offsite + GPG encryption deferred to Chunk 1.5.
- Restore-test required: one manual restore must pass before Chunk 1 declared done. Weekly automated restore-check is a Chunk 1.5 deliverable.

### H. Reconciliation pipeline — 7-step deterministic with LLM at 2 steps

1. **Extract** candidates from input (LLM call, narrow prompt — task-shaped facts only)
2. **Normalize** fields (deterministic — owner aliases, business inference, date parsing)
3. **Retrieve** candidate matches (deterministic — business filter + owner filter + time window + lexical search + embedding similarity)
4. **Choose** action: CREATE / UPDATE / COMPLETE / IGNORE / AMBIGUOUS (LLM call, structured prompt with retrieved candidates)
5. **Validate** (deterministic — schema + authz + business rules)
6. **Apply or queue review** (deterministic — `auto_merge_policy` lookup, confidence threshold, recent-human-touch check)
7. **Apply-time conflict re-check** (deterministic — version check inside DB transaction)

Each step independently testable. LLM calls logged with prompt, model, version.

---

## Cross-cutting principles

- **Prompt injection defense**: all imported text is treated as DATA, never instructions. Stored separately from prompts. Bounded excerpts only. Strict JSON schemas. Tool-call instructions in source content are rejected. Prompt + model + version logged per call.
- **Provenance**: every task field traceable to a source — who/what set it, from which event, at what confidence, under which reconciler version.
- **Multi-tenancy**: not classic multi-tenancy. One tenant (Kyle's operations) with business scoping. RedHot/Borderline modeled via `business_memberships` and per-task business linkage; cross-business tasks first-class.
- **Loaded weapons**: existing Gmail/Calendar tools never auto-fire to customers/vendors from tracker state. Drafts only. Recipient allowlist. Approval gates + idempotency keys + suppression logs before any send.
- **Two-gate workflow**: Codex review + Kyle approval before any chunk closes.

---

## Owners (final, for users seed)

- `agbusiness.kyle@gmail.com` — Kyle Conway — admin — RedHot + Borderline
- `joanna@borderlinefireworksoutlet.com` — Joanna Noriega — admin — RedHot + Borderline
- `Noriega3636@gmail.com` — Caleb Noriega — owner — RedHot
- `sarahjconway@gmail.com` — Sarah Conway — owner — Borderline

All four use Google SSO via Cloudflare Access.

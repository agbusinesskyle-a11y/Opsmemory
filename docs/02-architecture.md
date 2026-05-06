# OpsMemory — Architecture

## Four-layer overview

```
INGEST                     PROCESS                STATE                 OUTPUT
─────────                  ──────────             ──────────            ──────────
Meeting recap     ─┐                                                ┌─→ PWA dashboard
Slack channel     ─┤                                                │   tracker.kyleconway.ai
Slack /task       ─┤    ┌─────────────────┐    ┌──────────────┐    │   (Cloudflare Access)
Email forward     ─┼──→ │ 7-step pipeline │    │ Postgres on  │    │
Excel drop        ─┤    │ LLM @ extract   │    │ Spark        │    ├─→ Slack bot
SOP file drop     ─┤    │   + choose only │←──→│ action_tracker│   │   /tasks <owner>
Web paste box     ─┘    │                 │    │              │    │   /tasks stale
                        │ litellm chain:  │    │ users        │    │
                        │  Sonnet primary │    │ tasks        │    ├─→ Daily 7am push
                        │  → GPT-4.1      │    │ task_history │    │   (per-user config)
                        │  → local Llama  │    │ ingest_events│    │
                        │    (read only)  │    │ review_items │    └─→ Weekly Gmail draft
                        └─────────────────┘    │ sops         │        (existing n8n tool,
                                               │ anchor_events│         drafts only,
                                               │ sop_instances│         allowlist)
                                               └──────────────┘
                                                      ↑
                                               Audit log,
                                               idempotency keys,
                                               field-level versioning,
                                               provenance per field
```

## Layer 1: Ingest

Every input source converges to one table: `ingest_events`. Source-specific parsers turn raw input (Slack JSON, Excel rows, OCR'd PDF, free text) into a canonical "raw_content + source_metadata" event row.

| Source | Mechanism | Chunk |
|---|---|---|
| Meeting recap | Existing Co-Work Python parser posts to API | 3 |
| Slack channel | Bot listens to a designated channel | 7 |
| Slack `/task` slash | Slash command writes directly | 7 |
| Slack ✅ reaction | Bot reads reaction events as completion signals | 7 |
| Email forward | Forwarding address parsed | (later) |
| Excel/CSV drop | Drive folder watch + parser | 10 |
| SOP file drop | Drive folder watch + SOP parser | 9 |
| Web paste box | Dashboard textarea | 5 |

Idempotency: `content_hash` (normalized) + `source_external_id` (when source provides one) deduplicate at ingest.

## Layer 2: Process — 7-step reconciliation pipeline

Codex's design: LLM is one step in a pipeline, not the engine.

1. **Extract** — narrow LLM prompt: "given this raw input, output candidate task-shaped facts as strict JSON"
2. **Normalize** — deterministic: alias resolution (owner names), business inference, date parsing, dedup-keys
3. **Retrieve** — deterministic: candidate matches by business filter + owner filter + time window + lexical search + embedding similarity (top N)
4. **Choose** — structured LLM prompt: "given input X and these N candidates, pick CREATE / UPDATE / COMPLETE / IGNORE / AMBIGUOUS"
5. **Validate** — deterministic: schema enforcement, authz, business rules
6. **Apply or queue** — deterministic: `auto_merge_policy` lookup, confidence threshold, 60s human-touch holdoff, source-and-action-type-aware
7. **Apply-time conflict re-check** — DB transaction with `WHERE field_version = $base`

LLM provider chain (litellm):
- Primary: Sonnet 4.6
- Fallback: GPT-4.1
- Last resort: local Llama 70B on Spark — **extract/summarize only**, never invoked at step 4 (choose)
- Hard daily $ cap; Slack alert on cap hit

Module layout:
```
reconciliation/
├── extract.py        (LLM call: text → candidate tasks)
├── normalize.py      (deterministic)
├── retrieve.py       (deterministic)
├── choose.py         (LLM call: pick from retrieved candidates)
├── validate.py       (deterministic)
├── apply.py          (deterministic)
└── pipeline.py       (orchestrator)
```

Each module independently testable. Every LLM call logs prompt + model + prompt-version.

## Layer 3: State — Postgres `action_tracker`

Schema rooted in identity + provenance + lifecycle.

### Chunk 1 tables (substrate)

| Table | Purpose |
|---|---|
| `schema_migrations` | Migration bookkeeping |
| `users` | Identity + role + tz |
| `user_identities` | Provider subject mapping (Cloudflare Access, Slack) |
| `businesses` | RedHot, Borderline, future tenants |
| `business_memberships` | Per-user-per-business role |
| `service_accounts` | API key auth for n8n, doc-processor, MCP |
| `task_state_transitions` | Cross-table state-change audit log |

### Chunks 2-3 tables (added)

| Table | Purpose |
|---|---|
| `tasks` | The core task object with field versions |
| `task_history` | Immutable per-field audit |
| `task_field_versions` | Per-field version vector |
| `task_assignees` | M:M users↔tasks |
| `task_businesses` | M:M businesses↔tasks |
| `task_embeddings` | Vector store for retrieval (pgvector) |
| `ingest_events` | Raw input + source metadata + status |
| `review_items` | Pending mutations awaiting approval |
| `client_mutations` | Idempotency keys per client write |

### Later chunks

| Table | Chunk |
|---|---|
| `sops`, `sop_versions`, `sop_template_tasks` | 9 |
| `anchor_events`, `sop_instances`, `sop_generated_tasks` | 9 |
| `notification_prefs`, `notification_deliveries` | 11 |
| `auto_merge_policy` | 3 |

### Postgres role separation

- `opsmemory_owner` — owns schema, runs migrations. Used only at deploy time.
- `opsmemory_app` — runtime API role. Per-table grants only:
  - `schema_migrations` — SELECT only
  - `users`, `user_identities`, `businesses`, `business_memberships`, `service_accounts` — SELECT + UPDATE only
  - `task_state_transitions` — SELECT + INSERT only
  - **Future tables**: each migration grants per-table explicitly. The 0001 broad default-privileges grant was REVOKED in 0002 per Codex's review (it would have silently made every new table writable). Migrations from 0002 onward never set `ALTER DEFAULT PRIVILEGES`; they always grant per-table.
  Cannot DROP, never DELETEs from any current Chunk 1/2 table — soft-delete is via `deleted_at` UPDATE.

### Database isolation

`action_tracker` is a dedicated database. Does not share with `n8n`, `openbrain`, `family_docs`, `family_health`, `litellm`. Bad migration in `action_tracker` cannot affect existing services.

## Layer 4: Output

| Surface | Auth | Chunk |
|---|---|---|
| PWA dashboard at `tracker.kyleconway.ai` | Cloudflare Access (Google SSO) | 1 (shell), 2 (read), 4-5 (write) |
| Slack bot queries (`/tasks <owner>`, `/tasks stale`) | Slack user → `users` lookup | 8 |
| Daily 7am Phoenix push digest | Per-user notification prefs | 11 |
| Weekly Gmail draft | Existing `Tool: Gmail Send Borderline` n8n workflow, drafts only, recipient allowlist | 12 |
| mcp-server read API | Tenant-scoped, read-only initially | 13 |

## Cross-cutting concerns

### Identity and authorization

- Cloudflare Access JWT (`Cf-Access-Jwt-Assertion`) verified by `PyJWT[crypto]` against team domain JWKS
- Email claim → `user_identities` lookup → `users` row → role
- Service auth via `X-OpsMemory-Service-Key` header → HMAC-SHA256 with `SERVICE_KEY_PEPPER` → `service_accounts` lookup
- Authz check on every mutation route (not middleware)

### Concurrency (3-layer)

1. Field-level version compare (per-field version vectors)
2. 60s human-touch holdoff for LLM auto-merges
3. Apply-time conflict re-check inside DB transaction

Conflict response: `409` with current state. Client UI shows merge dialog.

### Backup (3-2-1 rule)

- Local encrypted dump on Spark #1 (Chunk 1)
- Rsync replica to Spark #2 (Chunk 1)
- Backblaze B2 offsite (Chunk 1.5)
- Restore test required to declare Chunk 1 done

### Logging

- JSON to stdout, Docker rotation `max-size=10m max-file=5`
- Auth headers and service keys redacted
- Request ID per line, propagated to DB queries

### Time

All `timestamptz` UTC in DB. Phoenix only at display layer and for cron schedules.

### CSP

```
default-src 'self';
script-src 'self';
style-src 'self';
img-src 'self' data:;
connect-src 'self';
manifest-src 'self';
worker-src 'self';
object-src 'none';
base-uri 'self';
frame-ancestors 'none';
```

### CORS

None. PWA is same-origin with API. Different origins are intentionally rejected.

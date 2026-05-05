# OpsMemory

Shared operational task graph for Kyle Conway's two fireworks businesses (RedHot Fireworks AZ + Borderline Fireworks SD). Year-round task system with multi-modal LLM-mediated ingest, served as a PWA + Slack bot on the Spark substrate.

## Status

- **2026-05-04** — Design phase complete. Codex senior-engineer review at 0.72 confidence. 18 design decisions locked (10 original + 8 Codex-driven). 14-chunk implementation plan locked. Chunk 1 (substrate) ready to start.

## Owners

- Kyle Conway — admin (both businesses)
- Joanna Noriega — admin (both businesses)
- Caleb Noriega — owner (RedHot)
- Sarah Conway — owner (Borderline)

## Architecture summary

Four layers:

1. **Ingest** — meeting recap, Slack channel, `/task` slash command, email forward, Excel/CSV drop, SOP file, web paste-box. All converge to `ingest_events`.
2. **Process** — Codex 7-step deterministic pipeline. LLM (litellm chain: Sonnet → GPT-4.1 → local Llama) only at extract and choose steps. Local Llama is extract-only, never mutates.
3. **State** — Postgres `action_tracker` database on Spark. Two-role separation (`opsmemory_owner` for DDL, `opsmemory_app` for runtime).
4. **Output** — PWA at `tracker.kyleconway.ai` (Cloudflare Access auth), Slack bot, daily push digest, weekly Gmail draft (drafts only, no auto-send), mcp-server read-only.

## Repo layout

```
opsmemory/
├── README.md                 (this file)
├── docs/
│   ├── 01-design.md          (18 locked design decisions)
│   ├── 02-architecture.md    (4-layer + schema overview)
│   ├── 03-chunk-plan.md      (14-chunk roadmap)
│   ├── 04-codex-chunk1-plan.md  (Codex's full Chunk 1 blueprint)
│   └── 99-decisions-log.md   (running log of decisions)
├── api/                      (FastAPI backend — added in Chunk 1)
├── web/                      (PWA — added in Chunk 1)
├── infra/                    (compose, cloudflared, migrations — added in Chunk 1)
├── ingest/                   (per-source parsers — added in Chunk 3)
├── reconciliation/           (LLM router, embedding, confidence — Chunk 3)
└── scripts/                  (backup, restore, ops — added in Chunk 1)
```

## Substrate dependencies

Deploys to existing Spark infrastructure:

- Postgres (existing instance, new dedicated `action_tracker` database)
- Cloudflared tunnel (existing Spark #1 tunnel; UUID lives in operator-only `docs/secrets-references.md`), new `tracker.kyleconway.ai` ingress
- Cloudflare Access (Google SSO IdP, 24h session)
- litellm (existing proxy, new model routing rules)
- mcp-server (existing — read integration in Chunk 13)
- Existing n8n tools: `Tool: Gmail Send Borderline`, `Tool: Calendar Create/Read` — used by later chunks for digests/reminders, drafts only

## Working agreements

- **Two-gate workflow**: Codex senior-engineer review of completed chunk + next-chunk plan before Kyle approves next chunk.
- **No customer/vendor-facing automation from tracker state without explicit human approval gates.**
- **Local Llama extracts only — never mutates.**
- **Auto-merge phased**: Days 1-30 everything to review, Days 30-90 CREATE auto, Day 90+ evaluate per-source telemetry.
- **3-2-1 backup rule** by Chunk 1.5: daily pg_dump + Spark #2 rsync + Backblaze B2 offsite.

See `docs/` for detail.

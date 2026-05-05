# Decisions Log

Running log of decisions made during OpsMemory build. Each entry: date, decider, decision, why.

---

## 2026-05-04 — Naming
**Decided**: Project named "OpsMemory". Subdomain `tracker.kyleconway.ai`. Repo at `C:\opsmemory\`, GitHub at `https://github.com/agbusinesskyle-a11y/Opsmemory.git`.
**Decider**: Kyle.

## 2026-05-04 — Substrate target
**Decided**: Deploy on Spark substrate (same as Conway_Motherduckdb), separate `action_tracker` Postgres database. Not on Cloudflare Workers + KV (Co-Work Phase 2 plan rejected).
**Decider**: Kyle, after joint Codex+Claude SWOT.
**Why**: Codex confidence 0.82 on Spark-native; KV state model wrong for relational/audit needs; Spark substrate already proven via Conway migration.

## 2026-05-04 — 10 design decisions Q&A
See `01-design.md`.

## 2026-05-04 — Codex review at 0.72 confidence
**Decided**: Accept all 6 Codex hardening overrides + answer 8 follow-up questions to lock the design.
**Decider**: Kyle.
**Why**: Codex flagged underspecification in concurrency, identity, lifecycles, deletion, auto-merge, backup, reconciliation pipeline. All overrides harden data integrity without changing product shape.

## 2026-05-04 — Joanna email migration
**Decided**: Joanna's login email switched from a Yahoo address to a Google Workspace address on her business domain.
**Decider**: Kyle.
**Why**: Yahoo email would have required Cloudflare Access One-Time PIN as a second IdP. Single Google IdP is simpler.
(Real addresses live in operator-only `docs/secrets-references.md`.)

## 2026-05-04 — Three Chunk 1 deferrals
**Decided**:
- Cloudflare Access app creation deferred to deploy time (code uses env-var placeholders).
- GPG backup encryption deferred to Chunk 1.5.
- B2 offsite backup deferred to Chunk 1.5.

**Decider**: Kyle.
**Why**: Cuts Chunk 1 scope without compromising recoverability — backups still on Spark #2 by Chunk 1 close. Encryption matters when backups leave home network (Chunk 1.5 ships that).

## 2026-05-04 — Design docs first commit
**Decided**: First commit is design docs only (this directory). Chunk 1 code lands as second commit.
**Decider**: Kyle.
**Why**: Auditable "this is what we agreed to" reference. Subsequent commits diff-able against the design.

## 2026-05-05 — Chunk 1 deployed to Spark
**Status**: Live at `tracker.kyleconway.ai`, fronted by Cloudflare Access (Google + email PIN), backed by `action_tracker` DB on the existing Spark Postgres container, daily backup timer active.

**Deploy notes**:
- Existing Spark Postgres is `pgvector/pgvector:pg17` running as a superuser whose name is set by the existing infrastructure (not `postgres`). OpsMemory uses two new roles: `opsmemory_owner` (DDL) + `opsmemory_app` (runtime, narrow grants).
- Spark Docker network name does not match the design's default — set `SPARK_NETWORK_NAME` in `.env` to whatever `docker network ls` shows for the existing infrastructure network.
- pwsh installed via `snap install powershell --classic` (path: `/snap/bin/pwsh`).
- pg_dump/pg_restore/psql NOT installed on the host — backup scripts use `docker exec -i <pg-container> ...` instead. PG17 client wasn't in Ubuntu Noble's default repo and PGDG add-via-`echo|tee` got mangled by terminal paste-wrapping.
- Cloudflare Access team domain configured during deploy (real value in operator-only `docs/secrets-references.md`).
- Cloudflared tunnel UUID is the existing Spark #1 tunnel shared with auto.kyleconway.ai and mcp.kyleconway.ai (real value in `docs/secrets-references.md`).
- Repo went public on GitHub during deploy because PAT auth in chat was brittle. Acceptable: no secrets in committed repo (verified via `.gitignore` + redaction sweep at start of Chunk 1.5).

**Production state at chunk-1-deployed**:
- Phone login (Kyle's owner account) verified end-to-end
- /readyz returns 200 with `backup_check: enabled`, recent backup_age_hours and restore_age_hours
- Daily backup timer enabled, next firing 02:17 America/Phoenix
- All other tenants (n8n / openbrain / family_*) untouched and still serving

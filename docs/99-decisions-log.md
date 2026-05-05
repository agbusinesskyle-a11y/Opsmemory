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
**Decided**: Joanna uses `joanna@borderlinefireworksoutlet.com` (Google Workspace) instead of `Joannamori@ymail.com`.
**Decider**: Kyle.
**Why**: Yahoo email would have required Cloudflare Access One-Time PIN as a second IdP. Single Google IdP is simpler.

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

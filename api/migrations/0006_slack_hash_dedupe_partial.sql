-- OpsMemory — Migration 0006: Make hash dedup partial / meeting_recap-only.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Codex chunk-5-step1 review: migration 0005 created a UNIQUE
-- (source, normalized_hash) index "to dedupe within a source." But for
-- Slack that uniqueness is wrong — identical short messages ("ok",
-- "+1", "lgtm", "done") legitimately recur across channels and threads,
-- and a (source, normalized_hash) UNIQUE collapses all of them into a
-- single ingest_event. The actual Slack idempotency key is the message
-- ts via (source, source_external_id), already enforced by the
-- partial UNIQUE index from migration 0003.
--
-- This migration:
--   1. Drops the (source, normalized_hash) UNIQUE index from 0005.
--   2. Recreates content-hash uniqueness as a partial index that ONLY
--      applies to source='meeting_recap'. Recaps are long, source-
--      authored, and a duplicate hash IS a duplicate ingest. Slack
--      messages no longer participate in hash uniqueness.
--   3. Adds a non-unique source-scoped hash index for fast lookup
--      (so the meeting_recap dedupe pre-check stays cheap, and any
--      future analytic / debugging query by hash is indexed).
--
-- The partial-unique pattern is what Codex recommended in step-1 review:
-- "make meeting-recap hash uniqueness a partial unique index."

-- =========================================================================
-- Drop the over-broad source-scoped hash UNIQUE from 0005.
-- =========================================================================

DROP INDEX IF EXISTS ingest_events_source_hash_uidx;

-- =========================================================================
-- meeting_recap-only partial UNIQUE on normalized_hash.
-- =========================================================================

CREATE UNIQUE INDEX IF NOT EXISTS ingest_events_meeting_recap_hash_uidx
  ON ingest_events(normalized_hash)
  WHERE source = 'meeting_recap';

-- =========================================================================
-- Non-unique helper index for source+hash lookups (any source).
-- Keeps the dedupe pre-check fast on meeting_recap and gives ad-hoc
-- queries (operator forensics, "where did this hash come from") a
-- usable index regardless of source.
-- =========================================================================

CREATE INDEX IF NOT EXISTS ingest_events_source_hash_idx
  ON ingest_events(source, normalized_hash);

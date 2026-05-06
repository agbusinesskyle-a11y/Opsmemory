-- OpsMemory — Migration 0005: Slack ingest source.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Two purposes:
--
-- 1. Source-scoped dedupe. Per Codex chunk-4-close review: the global
--    UNIQUE on ingest_events.normalized_hash from migration 0003 is
--    wrong for Slack, where two short messages in different channels
--    can legitimately have identical text (e.g. "lgtm", "+1"). Drops
--    the global index and replaces it with UNIQUE (source,
--    normalized_hash) so the same canonical content can appear across
--    sources without colliding, while still deduping within a source.
--
-- 2. Seed auto_merge_policy for slack_message. All three actions OFF
--    on day 1, mirroring the chunk-3 meeting_recap launch posture
--    (per chunk1.5 phased rollout). Slack is shorter and more
--    context-poor than meeting recaps; do not phase faster.
--
-- This migration carries NO endpoint or pipeline changes — those land
-- in api/app/v1_ingest.py (slack endpoint) in the same chunk. The
-- pipeline still skips non-meeting_recap sources for now; a follow-up
-- commit generalizes the prompt + step routing.

-- =========================================================================
-- Source-scoped dedup
-- =========================================================================

DROP INDEX IF EXISTS ingest_events_hash_uidx;

CREATE UNIQUE INDEX IF NOT EXISTS ingest_events_source_hash_uidx
  ON ingest_events(source, normalized_hash);

-- =========================================================================
-- auto_merge_policy seed for slack_message
-- =========================================================================

INSERT INTO auto_merge_policy (source_type, action_type, enabled, note)
VALUES
  ('slack_message', 'CREATE_TASK',  false,
    'Phased rollout — Day 0. Slack is shorter and more context-poor than '
    'recaps; promote only after observed approval data, CREATE first.'),
  ('slack_message', 'UPDATE_TASK',  false,
    'Phased rollout — Day 0. Promote later than CREATE.'),
  ('slack_message', 'COMPLETE_TASK', false,
    'Phased rollout — Day 0. Always-review for the foreseeable future.')
ON CONFLICT (source_type, action_type) DO NOTHING;

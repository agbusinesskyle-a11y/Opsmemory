-- OpsMemory — Migration 0012: file_drop auto_merge_policy seed (Chunk 9 step 1).
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Per Codex chunk-8-close STEP 9 PLAN: smallest first commit lands the
-- ingest endpoint and the auto_merge_policy seed. file_drop is NOT yet
-- registered in api/app/reconciliation/sources.SOURCES (worker ignores
-- it by allowlist), so events queue with status='received' but don't
-- get processed. Parser + prompt land in chunk 9 step 2.
--
-- All three actions seeded OFF day-1, mirroring the meeting_recap and
-- slack_message rollout posture (everything goes to review for the
-- first 30 days; promote per-source after observed approval rate).

INSERT INTO auto_merge_policy (source_type, action_type, enabled, note)
VALUES
  ('file_drop', 'CREATE_TASK',  false,
    'Phased rollout — Day 0. File-drop sources (CSV/XLSX/free-form) '
    'land in Chunk 9; promote later than meeting_recap because each '
    'row may carry low context.'),
  ('file_drop', 'UPDATE_TASK',  false,
    'Phased rollout — Day 0.'),
  ('file_drop', 'COMPLETE_TASK', false,
    'Phased rollout — Day 0. Always-review for the foreseeable future.')
ON CONFLICT (source_type, action_type) DO NOTHING;

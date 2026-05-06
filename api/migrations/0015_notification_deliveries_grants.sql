-- OpsMemory — Migration 0015: notification_deliveries grants for the
-- scheduler runner (Chunk 10 step 4 commit 3).
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Migration 0013 created notification_deliveries SELECT-only for
-- opsmemory_app. The scheduler now claims a delivery row per
-- (pref_id, scheduled_for) tuple via:
--    INSERT INTO notification_deliveries (...)
--    ON CONFLICT (idempotency_key) DO NOTHING
--    RETURNING id
--
-- A successful RETURNING id means this worker won the race and
-- owns the dispatch. The sender (step 5) updates the same row to
-- 'sent' / 'failed' on attempt. UPDATE is also granted here so
-- step 5 doesn't need a follow-up migration.

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    GRANT INSERT, UPDATE ON notification_deliveries TO opsmemory_app;
  END IF;
END $$;

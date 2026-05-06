-- OpsMemory — Migration 0014: notifications API grants (Chunk 10 step 2).
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Per Codex chunk-10-step1 STEP 2 PLAN: the prefs + subscription
-- endpoints need INSERT/UPDATE on these two tables.
-- notification_deliveries stays SELECT-only here; the sender (step 5)
-- adds INSERT then. Soft-revoke for subscriptions is UPDATE
-- status='revoked', not DELETE.

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    GRANT INSERT, UPDATE ON notification_prefs TO opsmemory_app;
    GRANT INSERT, UPDATE ON web_push_subscriptions TO opsmemory_app;
  END IF;
END $$;

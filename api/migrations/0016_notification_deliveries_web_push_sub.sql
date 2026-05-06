-- OpsMemory — Migration 0016: notification_deliveries.web_push_subscription_id
-- (Chunk 10 step 5 commit 1).
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Per Codex chunk-10-step5 plan-review: Web Push sends are per
-- subscription/device. Audit cardinality changes from
--    (pref_id, scheduled_for)
-- to
--    (pref_id, scheduled_for, web_push_subscription_id)
-- for the web_push channel. Other channels (slack_dm, email_digest)
-- keep web_push_subscription_id NULL — those remain per-pref.
--
-- The column is named web_push_subscription_id (not the generic
-- subscription_id) to honestly reflect that the FK is bound to
-- web_push_subscriptions; future Slack DM fan-out, if added, will
-- need its own column or a generalized targets table.
--
-- ON DELETE SET NULL preserves audit history when a subscription
-- row is hard-deleted (today subscriptions are only soft-revoked
-- via status='revoked', but a future operator action could DELETE).

ALTER TABLE notification_deliveries
  ADD COLUMN IF NOT EXISTS web_push_subscription_id uuid;

DO $$ BEGIN
  IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
       WHERE conname = 'notification_deliveries_web_push_subscription_id_fkey'
  ) THEN
    ALTER TABLE notification_deliveries
      ADD CONSTRAINT notification_deliveries_web_push_subscription_id_fkey
      FOREIGN KEY (web_push_subscription_id)
      REFERENCES web_push_subscriptions(id)
      ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS notification_deliveries_web_push_sub_idx
  ON notification_deliveries(web_push_subscription_id)
  WHERE web_push_subscription_id IS NOT NULL;

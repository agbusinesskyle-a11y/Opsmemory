-- OpsMemory — Migration 0008: client_mutations runtime + idempotency-replay payloads.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns the
-- outer transaction.
--
-- Per Codex chunk-5-close STEP 6 PLAN: open the client_mutations
-- table to runtime writes so the PWA can post idempotent task
-- mutations directly (toggle done, etc.). Replay-safe by storing the
-- exact response body + status of the original successful apply, so a
-- network-retry of the same idempotency_key returns the same result
-- without re-executing the mutation.
--
-- Schema delta:
--   client_mutations
--     + result_payload  jsonb DEFAULT '{}' — body of the response
--                        sent to the client on the successful apply.
--                        Replays return this verbatim.
--     + error_payload   jsonb DEFAULT '{}' — body of the response on
--                        a conflict (409) or validation failure (422).
--                        Replays return this verbatim with the cached
--                        response_status.
--     + response_status int — HTTP status of the original response.
--                        NULL until the mutation has been processed.
--
-- Runtime grants:
--   - GRANT INSERT, UPDATE on client_mutations to opsmemory_app.
--     0002 had SELECT only because no endpoints wrote to it; chunk 6
--     opens write paths.
--
-- The existing status CHECK ('received', 'applied', 'rejected',
-- 'conflict') from 0002 covers the four lifecycle states the chunk 6
-- toggle endpoint emits.

-- =========================================================================
-- Columns
-- =========================================================================

ALTER TABLE client_mutations
  ADD COLUMN IF NOT EXISTS result_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS error_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS response_status int;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'client_mutations_result_payload_object_chk'
  ) THEN
    ALTER TABLE client_mutations
      ADD CONSTRAINT client_mutations_result_payload_object_chk
        CHECK (jsonb_typeof(result_payload) = 'object');
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'client_mutations_error_payload_object_chk'
  ) THEN
    ALTER TABLE client_mutations
      ADD CONSTRAINT client_mutations_error_payload_object_chk
        CHECK (jsonb_typeof(error_payload) = 'object');
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'client_mutations_response_status_range_chk'
  ) THEN
    ALTER TABLE client_mutations
      ADD CONSTRAINT client_mutations_response_status_range_chk
        CHECK (response_status IS NULL OR response_status BETWEEN 100 AND 599);
  END IF;
END $$;

-- =========================================================================
-- Runtime grants
-- =========================================================================

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    GRANT INSERT, UPDATE ON client_mutations TO opsmemory_app;
  END IF;
END $$;

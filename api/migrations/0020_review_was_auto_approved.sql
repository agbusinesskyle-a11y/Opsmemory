-- OpsMemory — Migration 0020: review_items.was_auto_approved.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns
-- the outer transaction.
--
-- Scope (Phase UI-2B3-1, Codex Option D plan): the new
-- POST /v1/tasks endpoint creates an auto-approved review_item per
-- task so the audit chain stays uniform across entry surfaces:
--
--   pipeline-derived task:  ingest_event -> review_item -> task
--   manual-create task:     ingest_event -> review_item (auto)
--                                              -> task
--
-- The was_auto_approved flag lets the Triage UI exclude these
-- machine-stamped audit rows from the operator-facing
-- "Completed today" sub-tab. Without the flag, every typed
-- Quick Add would show up in Triage seconds after submission as
-- "approved by you", which is noise — the operator already knows
-- they created it.
--
-- Default false for backfill: existing review_items were all
-- operator-reviewed, so they correctly stay was_auto_approved=false.

ALTER TABLE review_items
  ADD COLUMN IF NOT EXISTS was_auto_approved boolean NOT NULL DEFAULT false;

-- Partial index so the Completed sub-tab's exclusion query is
-- index-supported. The index is small because was_auto_approved=true
-- is the rarer case (only auto-stamped manual-create rows).
CREATE INDEX IF NOT EXISTS review_items_was_auto_approved_idx
  ON review_items(was_auto_approved)
  WHERE was_auto_approved = true;

-- Existing GRANT SELECT, INSERT, UPDATE on review_items to
-- opsmemory_app (0003 line 328) covers the new column. No new
-- grant needed.

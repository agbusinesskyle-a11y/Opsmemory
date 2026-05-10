-- OpsMemory — Migration 0019: review_items snooze fields.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns
-- the outer transaction.
--
-- Scope (Phase UI-2B1, docs/17-ui-ux-research.md §10): the Triage
-- redesign added a Snoozed sub-tab. This migration adds the two
-- columns the snooze workflow writes:
--
--   snoozed_until   nullable. When > now(), the item is hidden from
--                   the default Inbox / Stale views and surfaces in
--                   Snoozed. When <= now(), the item is treated as
--                   un-snoozed and re-enters the Inbox queue (the
--                   row is otherwise unchanged — status stays
--                   'pending', no scheduler needs to wake it up).
--   snooze_reason   nullable, free text. Optional context the
--                   reviewer added at snooze time.
--
-- Design choice: snooze does NOT introduce a new lifecycle state.
-- The state machine (pending / needs_changes / approved / rejected
-- / superseded) stays exactly as defined in 0003. Snooze is a
-- temporary visibility filter on top of 'pending'. This keeps the
-- approve/reject/edit transitions and the audit trail unchanged.
--
-- Index: partial on snoozed_until WHERE snoozed_until IS NOT NULL.
-- The "is currently snoozed" predicate (snoozed_until > now()) hits
-- this index when the inbox query filters it out and when the
-- snoozed sub-view filters it in.

ALTER TABLE review_items
  ADD COLUMN IF NOT EXISTS snoozed_until timestamptz;

ALTER TABLE review_items
  ADD COLUMN IF NOT EXISTS snooze_reason text;

CREATE INDEX IF NOT EXISTS review_items_snoozed_until_idx
  ON review_items(snoozed_until)
  WHERE snoozed_until IS NOT NULL;

-- Existing GRANT SELECT, INSERT, UPDATE on review_items to
-- opsmemory_app (0003 line 328) covers the snooze write path.
-- No new grant needed.

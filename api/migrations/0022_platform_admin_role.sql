-- OpsMemory — Migration 0022: add platform_admin to app_role enum.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns
-- the outer transaction.
--
-- Phase MT-2 split into two migrations because Postgres requires the
-- enum value to be committed BEFORE it can appear in an UPDATE
-- statement targeting that enum column. 0022 just adds the value;
-- 0023 relaxes the users.role CHECK constraint and migrates Kyle +
-- Joanna's rows. This split keeps both migrations idempotent and
-- replay-safe.

ALTER TYPE app_role ADD VALUE IF NOT EXISTS 'platform_admin';

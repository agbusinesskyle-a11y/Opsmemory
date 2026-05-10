-- OpsMemory — Migration 0024: seed Family Room business.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns
-- the outer transaction.
--
-- Phase MT-3: third tenant. Kyle + Sarah's personal/family
-- coordination space, separate from the fireworks businesses
-- (borderline, redhot) and Conway Feed. The same authz/retrieve/
-- Quick Add stack handles it; nothing in the schema changes.
--
-- Public identifier (no PII), inline-seeded like
-- redhot/borderline/conway-feed. Fixed UUID for parity.
-- Membership rows for Kyle + Sarah are added separately (private,
-- via psql or scripts/seed_initial.py with a future owners.json
-- update).

INSERT INTO businesses (id, slug, name)
VALUES
  ('00000000-0000-0000-0000-000000000204', 'family', 'Family Room')
ON CONFLICT (id) DO UPDATE
SET slug = EXCLUDED.slug,
    name = EXCLUDED.name,
    updated_at = now();

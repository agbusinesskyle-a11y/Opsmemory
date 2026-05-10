-- OpsMemory — Migration 0021: seed Conway Feed business.
--
-- Idempotent. NO top-level BEGIN/COMMIT — scripts/migrate.py owns
-- the outer transaction.
--
-- Scope (Phase MT-1, second-tenant onboarding): adds Conway Feed
-- as a peer business of redhot / borderline. Caleb Noriega is the
-- operations manager; his business_membership row is added via
-- scripts/seed_initial.py (private — kept out of public SQL per
-- the chunk-1.5 split).
--
-- Why a peer business and not a parent/child structure under Selah:
--   The current schema has a flat `businesses` table and the entire
--   authz / retrieve / Quick Add stack is built on a single business
--   slug per task. Modeling Selah as a parent + Conway Feed as a
--   child would require a multi-week refactor (parent_business_id,
--   inheritance rules in business_memberships, retrieve scoping
--   changes). Instead we treat Conway Feed as a distinct tenant —
--   exactly the pattern the next OpsMemory deploys (other clients
--   beyond Kyle) will use too. Selah Financial doesn't need to
--   exist as a row in OpsMemory; it's the off-platform parent.
--
-- Fixed UUID 00000000-0000-0000-0000-000000000203 follows the
-- 0001 convention (000000000201/202 for redhot/borderline).

INSERT INTO businesses (id, slug, name)
VALUES
  ('00000000-0000-0000-0000-000000000203', 'conway-feed', 'Conway Feed')
ON CONFLICT (id) DO UPDATE
SET slug = EXCLUDED.slug,
    name = EXCLUDED.name,
    updated_at = now();

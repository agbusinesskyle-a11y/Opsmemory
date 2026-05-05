Reading prompt from stdin...
OpenAI Codex v0.128.0 (research preview)
--------
workdir: C:\Users\agbus
model: gpt-5.5
provider: openai
approval: never
sandbox: read-only
reasoning effort: xhigh
reasoning summaries: none
session id: 019df54f-ad66-7e00-b3e4-565a81c7a19a
--------
user
Senior-engineer detailed Chunk 1 planning request. Self-contained.

==== ROLE ====

You are senior engineer for OpsMemory. We've completed the design phase together (you reviewed the 10 decisions + 8 follow-up answers). Kyle approved your hardening overrides. Repo just initialized at C:\opsmemory connected to https://github.com/agbusinesskyle-a11y/Opsmemory.git. Now we plan Chunk 1 in the kind of detail that lets implementation proceed without surprise.

Kyle's specific instruction: "Plan this phase with codex again. Make sure each phase is well thought out. Think about the small details to make this all work seamlessly. Play it out front to back back to front. No sense in building unless it works."

This is not a design review. It is the implementation plan. Be specific.

==== WHAT IS LOCKED ====

**18 design decisions:**
1. Auth tiered (admin/owner/service); 24h reversal window; shared tasks closeable by any assignee
2. SOPs are templates with anchor events; updates propagate to unedited fields with per-field human-override flag
3. Soft-delete with admin hard-delete after 7 days; superseded_by_task_id for merges; restore endpoint
4. Two task states (open/done) but FIVE separate lifecycles formalized as Postgres ENUMs in chunk 1 (task, review, ingest, notification, deletion)
5. PWA only; manifest+SW
6. Three-layer concurrency: field-level version compare + 60s human-touch holdoff for LLM + apply-time conflict re-check inside transaction
7. Per-user push prefs; 7am Phoenix daily digest default
8. Balanced LLM classification threshold
9. litellm chain Sonnet → GPT-4.1 → local Llama (extract/summarize ONLY for Llama, never mutates)
10. (covered by #6)

**8 Codex-driven decisions:**
- Identity: real users + user_identities + service_accounts tables; CF Access proves email, SQL proves authz
- Concurrency: 3-layer (covered #6)
- Llama scope: extract/summarize only
- Lifecycles: 5 enums + task_state_transitions audit table
- Deletion: soft + 7d admin hard delete
- Auto-merge: phased — Days 1-30 review only, 30-90 CREATE auto, 90+ evaluate
- Backup: daily pg_dump + Spark #2 rsync + B2 offsite (3-2-1). DEFERRED to Chunk 1.5: B2 offsite leg.
- Reconciliation: 7-step deterministic pipeline, LLM only at extract+choose

**14-chunk plan (Codex-revised):**
1. Substrate (this chunk)
2. API skeleton with authz, read-only dashboard, audit/event model
3. Meeting recap ingest to pending review only
4. Review UI + approve/reject flow
5. PWA write paths
6. Offline + outbox + SW
7. Slack ingest
8. Slack query bot
9. SOP/anchor system
10. Excel/file ingest
11. Push notifications + settings UI
12. Gmail digest drafts (allowlist)
13. MCP read-only
14. SOP suggestion engine

**Spark substrate (already running):**
- n8n 2.18.5 at auto.kyleconway.ai (22 production workflows)
- mcp-server at mcp.kyleconway.ai
- Postgres with 8 databases (n8n, openbrain, family_docs, family_health, litellm, others)
- litellm proxy
- doc-processor (OCR via gpt-4.1)
- cloudflared tunnel (b510e94c-8eab-40dd-ae8d-5c933a3896da), Cloudflare Access available
- Spark #2 reachable for backup rsync
- /opt/open-brain on Spark — files arrive via rsync from Kyle's laptop, NOT a git checkout

**Chunk 1 scope (per your earlier review):**
Files:
- README.md
- .env.example
- docker-compose.yml
- api/app/{main.py,db.py,auth.py,health.py}
- api/migrations/0001_initial.sql
- web/ (PWA shell)
- scripts/{backup_action_tracker.ps1,restore_check.ps1}

Acceptance:
- action_tracker DB exists, repeatable migrations
- /healthz /readyz /whoami working
- CF Access JWT validation OR explicit local-dev mode
- Seeded: 4 users + 2 businesses
- PWA shell at tracker.kyleconway.ai
- No public ports
- Backup runs, restore test documented & passing
- n8n/openbrain/family DBs untouched

Explicitly NOT in Chunk 1: LLM, Slack, SOPs, offline logic.
B2 offsite deferred to Chunk 1.5.

==== WHAT I NEED FROM YOU ====

Treat this as the BLUEPRINT before code. I'll implement what you spec.

**1. Front-to-back deploy trace.** Starting from "git repo just initialized, no code." Every step in order to a fully-deployed Chunk 1: file authoring order, dependencies between files, when DB is created, when migrations run, when service is registered with cloudflared, when Cloudflare Access policy is applied, when DNS record is created, when first healthcheck passes. Be linear and complete.

**2. Back-to-front user trace.** Joanna opens https://tracker.kyleconway.ai on her phone. Walk every layer: DNS → Cloudflare edge → Access challenge → Google SSO → JWT issuance → cloudflared tunnel → Spark service → FastAPI app → auth.py JWT validation → users table lookup → /whoami response → PWA shell renders → service worker registers. Identify every component that must exist for this single request to succeed. Anything missing fails the chunk.

**3. The full 0001_initial.sql migration.** All tables, columns, types, FKs, indexes, CHECK constraints, ENUMs, seed data inserts. Concrete DDL — I'll copy it. Include:
- All 5 lifecycle ENUMs
- users, user_identities, service_accounts tables
- businesses table + seed (RedHot, Borderline)
- users seed (Kyle, Joanna admin; Caleb, Sarah owner)
- task_state_transitions audit table (even though tasks table comes in chunk 2)
- migrations bookkeeping table (schema_migrations or similar)
- Required extensions (uuid-ossp? citext? pgvector? or defer pgvector to chunk 3?)

**4. Auth flow concrete.** auth.py contents specifically. CF Access JWT signature verification (using which library? team domain? application audience?). Local-dev mode (env flag? what does it return?). Exception handling. What goes in middleware vs route dependency. How service-account API keys validate (separate header? same header?).

**5. Healthcheck designs.**
- /healthz: liveness — what does it actually check?
- /readyz: readiness — DB ping? backup-script-last-run? what threshold?
- /whoami: who am I — full user record + role + role-derived permissions?

**6. Backup script + restore test.**
- backup_action_tracker.ps1: pg_dump command (which flags? -Fc custom format?), encryption (gpg? which key?), local retention (how many days?), rsync to Spark #2 (which path? authentication?), cron registration mechanism (Windows Task Scheduler? cron-via-WSL? Spark-native cron?).
- restore_check.ps1: pulls latest dump, restores to action_tracker_restore_test DB, runs WHAT smoke checks (row counts? schema validation?), drops test DB. Failure mode: fires Slack alert via existing tooling.
- Cadence: daily backup at what time? Restore test weekly?

**7. docker-compose.yml structure.**
- Service definition for opsmemory-api (FastAPI)
- How it integrates with existing infrastructure/spark1-postgres-redis.compose.yml — new compose file or extend existing?
- Env vars needed (.env.example contents)
- Network setup — does it share network with mcp-server, n8n, postgres?
- Health checks
- Restart policy
- Secrets handling (CF Access JWKS, DB password, B2 keys when added)

**8. Cloudflared ingress + DNS + Access policy.**
- Exact cloudflared rule for tracker.kyleconway.ai → http://localhost:XXXX
- DNS CNAME (or proxied A record?) configuration
- Cloudflare Access application config: name, domain, policy (which emails allowed?), session duration
- Order in which these are created vs. service starting (avoid 502 windows)
- Failure mode: what if Access is misconfigured and locks Kyle out?

**9. PWA shell minimum viable.**
- index.html (what's the empty state?)
- manifest.json (icons? — can we ship with placeholders?)
- service worker (just register-and-claim, no caching logic in chunk 1?)
- A "logged in as Joanna Noriega — admin" screen pulling from /whoami — does that count as "shell," or is it too much?
- What's the install-on-phone test exactly?

**10. Things in Chunk 1 that aren't obvious yet.** What am I going to forget? Examples I'd expect:
- Logging strategy (where do logs go? rotation? PII redaction?)
- Secrets at rest (just .env? or actual secret store?)
- Time zone handling (Phoenix vs UTC — store everything as UTC?)
- Error tracking (Sentry? local file? skip for now?)
- API versioning (/v1/ prefix from day 1?)
- CORS (PWA same-origin so probably no, but worth confirming)
- CSP headers (PWA needs careful CSP)
- Rate limiting (any in chunk 1, or defer?)
- Database connection pooling (asyncpg pool config)
- Migration framework choice (alembic? raw SQL with version table? something else?)

**11. Ordering within chunk 1.** What gets built first within the chunk? What blocks what? Suggested commit/PR-sized increments.

**12. The ONE thing you'd defer.** If Kyle wanted to ship Chunk 1 faster, what's the smallest cut that still produces a "boring, recoverable substrate" you'd sign off on?

**13. Acceptance test plan.** Concrete tests that, when they pass, declare Chunk 1 done:
- DB tests (migration up/down, seeds present)
- Auth tests (CF Access valid JWT → 200, invalid → 401, missing → 401, local-dev → returns dev user)
- Backup tests (pg_dump produces valid file, restore-check creates test DB and validates)
- Smoke tests (healthcheck through cloudflared from outside)
- Acceptance criteria checklist

Output as numbered sections matching the 13 above. Be concrete. I will write code from this. If something is ambiguous, name it as ambiguous and recommend a default.

Length: take what you need. Detail is the point.

codex
I’ll treat this as a build blueprint rather than a review. I’m going to pin the few platform defaults that are ambiguous, then walk the deployment and request paths end to end so the implementation has fewer hidden decisions.
web search: 
web search: Cloudflare Access JWT validation JWKS AUD official docs
web search: 
web search: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/
web search: 
web search: 'Cf-Access-Jwt-Assertion' in https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/
codex
**1. Front-To-Back Deploy Trace**

1. Create repo structure in `C:\opsmemory`; do not use `/opt/open-brain`.
2. Author `.env.example` first because API, compose, and scripts all depend on the same env contract.
3. Author `api/migrations/0001_initial.sql`.
4. Author `api/app/db.py`, then `auth.py`, then `health.py`, then `main.py`.
5. Author `web/index.html`, `web/app.js`, `web/manifest.json`, `web/sw.js`, icons.
6. Author `docker-compose.yml`.
7. Author backup/restore scripts.
8. Push to GitHub.
9. On Spark: clone to `/opt/opsmemory`.
10. Create Postgres roles/database only for OpsMemory:

```sql
CREATE ROLE opsmemory_owner LOGIN PASSWORD '<owner-password>';
CREATE ROLE opsmemory_app LOGIN PASSWORD '<app-password>';
CREATE DATABASE action_tracker OWNER opsmemory_owner;
GRANT CONNECT ON DATABASE action_tracker TO opsmemory_app;
```

11. Run `0001_initial.sql` against `action_tracker` as `opsmemory_owner`.
12. Verify seeds: 4 users, 2 businesses, 5 lifecycle enums.
13. Create production `.env` on Spark at `/opt/opsmemory/.env`, mode `0640`, not committed.
14. Start API with Docker Compose, binding only `127.0.0.1:8010`.
15. First local checks:

```bash
curl http://127.0.0.1:8010/healthz
curl http://127.0.0.1:8010/readyz
```

16. Create Cloudflare Access application for `tracker.kyleconway.ai` before DNS points there.
17. Apply Access allow policy for Kyle, Joanna, Caleb, Sarah.
18. Add cloudflared ingress: `tracker.kyleconway.ai -> http://localhost:8010`.
19. Create proxied DNS CNAME to the existing tunnel.
20. Test external Access login.
21. Run first backup manually.
22. Run restore check manually.
23. Enable daily backup timer and weekly restore-check timer.
24. Flip `READYZ_REQUIRE_BACKUP=true` after first successful backup/restore.
25. Final check: Joanna logs in from phone, PWA shell renders, `/whoami` returns admin.

**2. Back-To-Front User Trace**

Joanna opens `https://tracker.kyleconway.ai`.

Required path:

1. Phone DNS resolves `tracker.kyleconway.ai`.
2. Cloudflare sees proxied CNAME to tunnel `b510e94c-8eab-40dd-ae8d-5c933a3896da.cfargotunnel.com`.
3. Cloudflare Access application matches hostname.
4. No valid `CF_Authorization` cookie, so Access redirects to Google SSO.
5. Joanna authenticates with Google.
6. Cloudflare checks policy: Joanna’s exact email must be allowed.
7. Cloudflare issues Access JWT/cookie.
8. Request resumes through Cloudflare Tunnel.
9. Existing `cloudflared` on Spark receives request over outbound tunnel.
10. Ingress sends request to `http://localhost:8010`.
11. Docker host forwards `127.0.0.1:8010` to `opsmemory-api:8000`.
12. FastAPI serves PWA shell.
13. Browser runs `app.js` and requests `/whoami`.
14. Cloudflare adds `Cf-Access-Jwt-Assertion` header to origin request.
15. `auth.py` verifies JWT signature, issuer, audience, expiry.
16. `auth.py` extracts email, lowercases it, queries `user_identities` + `users`.
17. SQL proves Joanna is active admin.
18. `/whoami` returns user, role, businesses, permissions.
19. UI renders `Logged in as Joanna Noriega — admin`.
20. Browser registers `sw.js`.

Missing any of these fails Chunk 1.

**3. Full `0001_initial.sql`**

Ambiguity: exact Google emails are unknown. Replace the four `@kyleconway.ai` seed emails with the real emails before production Access testing.

```sql
BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

DO $$ BEGIN
  CREATE TYPE app_role AS ENUM ('admin', 'owner', 'service');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE user_status AS ENUM ('active', 'disabled');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE task_lifecycle_state AS ENUM ('open', 'done');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE review_lifecycle_state AS ENUM (
    'pending',
    'approved',
    'rejected',
    'needs_changes',
    'superseded'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE ingest_lifecycle_state AS ENUM (
    'received',
    'extracting',
    'pending_review',
    'completed',
    'failed',
    'cancelled'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE notification_lifecycle_state AS ENUM (
    'pending',
    'scheduled',
    'sent',
    'failed',
    'cancelled',
    'suppressed'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE deletion_lifecycle_state AS ENUM (
    'active',
    'soft_deleted',
    'restore_requested',
    'restored',
    'hard_delete_eligible',
    'hard_deleted'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  description text NOT NULL,
  checksum text,
  applied_at timestamptz NOT NULL DEFAULT now(),
  execution_ms integer,
  dirty boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS businesses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug citext NOT NULL UNIQUE,
  name text NOT NULL,
  deletion_state deletion_lifecycle_state NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  CHECK (slug::text ~ '^[a-z0-9][a-z0-9-]*$'),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (
    (deletion_state = 'active' AND deleted_at IS NULL)
    OR deletion_state <> 'active'
  )
);

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email citext NOT NULL UNIQUE,
  display_name text NOT NULL,
  role app_role NOT NULL,
  status user_status NOT NULL DEFAULT 'active',
  timezone text NOT NULL DEFAULT 'America/Phoenix',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz,
  CHECK (role IN ('admin', 'owner')),
  CHECK (position('@' in email::text) > 1),
  CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE TABLE IF NOT EXISTS user_identities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider text NOT NULL,
  provider_subject text,
  email citext NOT NULL,
  claims jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_authenticated_at timestamptz,
  CHECK (provider IN ('cloudflare_access', 'google')),
  CHECK (position('@' in email::text) > 1),
  CHECK (jsonb_typeof(claims) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS user_identities_provider_subject_uidx
  ON user_identities(provider, provider_subject)
  WHERE provider_subject IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS user_identities_provider_email_uidx
  ON user_identities(provider, email);

CREATE INDEX IF NOT EXISTS user_identities_user_id_idx
  ON user_identities(user_id);

CREATE TABLE IF NOT EXISTS business_memberships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role app_role NOT NULL,
  status user_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (role IN ('admin', 'owner')),
  UNIQUE (business_id, user_id)
);

CREATE INDEX IF NOT EXISTS business_memberships_user_id_idx
  ON business_memberships(user_id);

CREATE TABLE IF NOT EXISTS service_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  description text,
  role app_role NOT NULL DEFAULT 'service',
  status user_status NOT NULL DEFAULT 'active',
  key_prefix text NOT NULL UNIQUE,
  key_hash text NOT NULL,
  scopes text[] NOT NULL DEFAULT ARRAY[]::text[],
  created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  expires_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (role = 'service'),
  CHECK (length(key_prefix) >= 8),
  CHECK (key_hash ~ '^[a-f0-9]{64}$'),
  CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE TABLE IF NOT EXISTS task_state_transitions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE RESTRICT,
  from_state task_lifecycle_state,
  to_state task_lifecycle_state NOT NULL,
  actor_kind text NOT NULL,
  actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  actor_service_account_id uuid REFERENCES service_accounts(id) ON DELETE SET NULL,
  reason text,
  request_id text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  transitioned_at timestamptz NOT NULL DEFAULT now(),
  CHECK (from_state IS NULL OR from_state <> to_state),
  CHECK (actor_kind IN ('user', 'service', 'system', 'migration')),
  CHECK (
    (actor_kind = 'user' AND actor_user_id IS NOT NULL AND actor_service_account_id IS NULL)
    OR (actor_kind = 'service' AND actor_user_id IS NULL AND actor_service_account_id IS NOT NULL)
    OR (actor_kind IN ('system', 'migration') AND actor_user_id IS NULL AND actor_service_account_id IS NULL)
  ),
  CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX IF NOT EXISTS task_state_transitions_task_id_idx
  ON task_state_transitions(task_id);

CREATE INDEX IF NOT EXISTS task_state_transitions_business_time_idx
  ON task_state_transitions(business_id, transitioned_at DESC);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_businesses_updated_at') THEN
    CREATE TRIGGER trg_businesses_updated_at
    BEFORE UPDATE ON businesses
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_users_updated_at') THEN
    CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_user_identities_updated_at') THEN
    CREATE TRIGGER trg_user_identities_updated_at
    BEFORE UPDATE ON user_identities
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_business_memberships_updated_at') THEN
    CREATE TRIGGER trg_business_memberships_updated_at
    BEFORE UPDATE ON business_memberships
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_service_accounts_updated_at') THEN
    CREATE TRIGGER trg_service_accounts_updated_at
    BEFORE UPDATE ON service_accounts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

INSERT INTO businesses (id, slug, name)
VALUES
  ('00000000-0000-0000-0000-000000000201', 'redhot', 'RedHot'),
  ('00000000-0000-0000-0000-000000000202', 'borderline', 'Borderline')
ON CONFLICT (id) DO UPDATE
SET slug = EXCLUDED.slug,
    name = EXCLUDED.name,
    updated_at = now();

INSERT INTO users (id, email, display_name, role)
VALUES
  ('00000000-0000-0000-0000-000000000101', 'kyle@kyleconway.ai', 'Kyle Conway', 'admin'),
  ('00000000-0000-0000-0000-000000000102', 'joanna@kyleconway.ai', 'Joanna Noriega', 'admin'),
  ('00000000-0000-0000-0000-000000000103', 'caleb@kyleconway.ai', 'Caleb Conway', 'owner'),
  ('00000000-0000-0000-0000-000000000104', 'sarah@kyleconway.ai', 'Sarah Conway', 'owner')
ON CONFLICT (id) DO UPDATE
SET email = EXCLUDED.email,
    display_name = EXCLUDED.display_name,
    role = EXCLUDED.role,
    updated_at = now();

INSERT INTO user_identities (user_id, provider, provider_subject, email)
SELECT id, 'cloudflare_access', NULL, email
FROM users
ON CONFLICT (provider, email) DO UPDATE
SET user_id = EXCLUDED.user_id,
    updated_at = now();

INSERT INTO business_memberships (business_id, user_id, role)
VALUES
  ('00000000-0000-0000-0000-000000000201', '00000000-0000-0000-0000-000000000101', 'admin'),
  ('00000000-0000-0000-0000-000000000202', '00000000-0000-0000-0000-000000000101', 'admin'),
  ('00000000-0000-0000-0000-000000000201', '00000000-0000-0000-0000-000000000102', 'admin'),
  ('00000000-0000-0000-0000-000000000202', '00000000-0000-0000-0000-000000000102', 'admin'),
  ('00000000-0000-0000-0000-000000000201', '00000000-0000-0000-0000-000000000103', 'owner'),
  ('00000000-0000-0000-0000-000000000202', '00000000-0000-0000-0000-000000000104', 'owner')
ON CONFLICT (business_id, user_id) DO UPDATE
SET role = EXCLUDED.role,
    updated_at = now();

INSERT INTO schema_migrations (version, description)
VALUES ('0001_initial', 'Chunk 1 substrate: auth, businesses, lifecycle enums, seed data')
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description,
    applied_at = now(),
    dirty = false;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    GRANT USAGE ON SCHEMA public TO opsmemory_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO opsmemory_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO opsmemory_app;
  END IF;
END $$;

COMMIT;
```

`pgvector` is deferred until LLM/embedding work. Use `pgcrypto` instead of `uuid-ossp`.

**4. Auth Flow Concrete**

Use `PyJWT[crypto]` for Cloudflare JWT verification. Cloudflare’s docs specify the `Cf-Access-Jwt-Assertion` origin header, issuer as the team domain, and audience as the Access application AUD.

Recommended env:

```env
AUTH_MODE=cloudflare
CF_ACCESS_TEAM_DOMAIN=https://<team-name>.cloudflareaccess.com
CF_ACCESS_AUD=<tracker-application-aud>
LOCAL_DEV_EMAIL=kyle@kyleconway.ai
ALLOW_DEV_USER_SWITCH=false
SERVICE_KEY_PEPPER=<long-random-secret>
```

Human auth:

1. Require `Cf-Access-Jwt-Assertion`.
2. Verify RS256 signature using `${CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs`.
3. Verify issuer and audience.
4. Require `email`.
5. Look up `user_identities.provider='cloudflare_access'` by email.
6. Require active user.
7. Return principal.

Service-account auth:

1. Require Cloudflare Access service auth at the edge.
2. App also requires `X-OpsMemory-Service-Key`.
3. Hash key with HMAC-SHA256 using `SERVICE_KEY_PEPPER`.
4. Match `service_accounts.key_prefix` and `key_hash`.
5. Require active and not expired.

Do not use `Authorization` for app service keys; Cloudflare Access may use it for service-token configuration.

`auth.py` shape:

```python
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Literal

import jwt
from fastapi import HTTPException, Request, status
from jwt import InvalidTokenError, PyJWKClient


@dataclass(frozen=True)
class Principal:
    principal_type: Literal["user", "service"]
    id: str
    display_name: str
    email: str | None
    role: str
    businesses: list[dict[str, Any]]
    permissions: dict[str, bool]
    auth_method: str


_jwk_client: PyJWKClient | None = None


def _team_domain() -> str:
    raw = os.environ["CF_ACCESS_TEAM_DOMAIN"].rstrip("/")
    if not raw.startswith("https://"):
        raw = f"https://{raw}"
    return raw


def _jwks() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(f"{_team_domain()}/cdn-cgi/access/certs")
    return _jwk_client


def _permissions(role: str, scopes: list[str] | None = None) -> dict[str, bool]:
    scopes = scopes or []
    if role == "admin":
        return {
            "can_view_all_businesses": True,
            "can_manage_users": True,
            "can_restore": True,
            "can_hard_delete": True,
            "can_use_service_api": False,
        }
    if role == "owner":
        return {
            "can_view_all_businesses": False,
            "can_manage_users": False,
            "can_restore": False,
            "can_hard_delete": False,
            "can_use_service_api": False,
        }
    return {
        "can_view_all_businesses": "businesses:read" in scopes,
        "can_manage_users": False,
        "can_restore": False,
        "can_hard_delete": False,
        "can_use_service_api": True,
    }


def _verify_cf_jwt(request: Request, require_email: bool) -> dict[str, Any]:
    token = request.headers.get("Cf-Access-Jwt-Assertion")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing access token")

    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=os.environ["CF_ACCESS_AUD"],
            issuer=_team_domain(),
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token")

    if require_email and not claims.get("email"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing access email")

    return claims


async def _load_user(request: Request, email: str, claims: dict[str, Any], auth_method: str) -> Principal:
    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              ui.id AS identity_id,
              u.id::text AS id,
              u.email::text AS email,
              u.display_name,
              u.role::text AS role
            FROM user_identities ui
            JOIN users u ON u.id = ui.user_id
            WHERE ui.provider = 'cloudflare_access'
              AND ui.email = $1
              AND u.status = 'active'
            """,
            email.lower(),
        )
        if not row:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user not authorized")

        businesses = await conn.fetch(
            """
            SELECT b.id::text AS id, b.slug::text AS slug, b.name, bm.role::text AS role
            FROM business_memberships bm
            JOIN businesses b ON b.id = bm.business_id
            WHERE bm.user_id = $1::uuid
              AND bm.status = 'active'
              AND b.deletion_state = 'active'
            ORDER BY b.name
            """,
            row["id"],
        )

        claim_subset = {
            "sub": claims.get("sub"),
            "email": claims.get("email"),
            "iss": claims.get("iss"),
            "aud": claims.get("aud"),
            "iat": claims.get("iat"),
            "exp": claims.get("exp"),
        }

        await conn.execute(
            """
            UPDATE user_identities
            SET last_authenticated_at = now(), claims = $2::jsonb
            WHERE id = $1
            """,
            row["identity_id"],
            json.dumps(claim_subset),
        )

        await conn.execute(
            "UPDATE users SET last_seen_at = now() WHERE id = $1::uuid",
            row["id"],
        )

    return Principal(
        principal_type="user",
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        businesses=[dict(b) for b in businesses],
        permissions=_permissions(row["role"]),
        auth_method=auth_method,
    )


async def _load_service(request: Request, raw_key: str) -> Principal:
    pepper = os.environ.get("SERVICE_KEY_PEPPER")
    if not pepper:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="service auth unavailable")

    key_prefix = raw_key[:16]
    key_hash = hmac.new(pepper.encode(), raw_key.encode(), hashlib.sha256).hexdigest()

    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text, name, key_hash, scopes
            FROM service_accounts
            WHERE key_prefix = $1
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > now())
            """,
            key_prefix,
        )

        if not row or not hmac.compare_digest(row["key_hash"], key_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service key")

        await conn.execute(
            "UPDATE service_accounts SET last_used_at = now() WHERE id = $1::uuid",
            row["id"],
        )

    scopes = list(row["scopes"] or [])
    return Principal(
        principal_type="service",
        id=row["id"],
        email=None,
        display_name=row["name"],
        role="service",
        businesses=[],
        permissions=_permissions("service", scopes),
        auth_method="service_key",
    )


async def require_principal(request: Request) -> Principal:
    auth_mode = os.environ.get("AUTH_MODE", "cloudflare")

    service_key = request.headers.get("X-OpsMemory-Service-Key")
    if service_key:
        if auth_mode == "cloudflare":
            _verify_cf_jwt(request, require_email=False)
        return await _load_service(request, service_key)

    if auth_mode == "local":
        email = os.environ.get("LOCAL_DEV_EMAIL", "kyle@kyleconway.ai")
        if os.environ.get("ALLOW_DEV_USER_SWITCH") == "true":
            email = request.headers.get("X-Dev-User-Email", email)
        return await _load_user(request, email, {"email": email}, "local_dev")

    claims = _verify_cf_jwt(request, require_email=True)
    return await _load_user(request, claims["email"], claims, "cloudflare_access")
```

Auth should be a route dependency, not global middleware. Middleware should handle request IDs, logging, security headers, and proxy headers only.

**5. Healthcheck Designs**

`/healthz`: liveness only.

Checks:
- Process is alive.
- App can return JSON.
- No DB dependency.
- No auth dependency.

Response:

```json
{"ok":true,"service":"opsmemory-api","version":"chunk1","time":"2026-05-04T00:00:00Z"}
```

`/readyz`: readiness.

Checks:
- DB pool available.
- `SELECT 1` succeeds.
- `schema_migrations` contains `0001_initial`.
- Optional backup status file is recent.

Production after first backup:

```env
READYZ_REQUIRE_BACKUP=true
READYZ_BACKUP_MAX_AGE_HOURS=36
READYZ_RESTORE_MAX_AGE_HOURS=192
```

Before first backup, keep `READYZ_REQUIRE_BACKUP=false` to avoid bootstrapping failure.

`/whoami`: authenticated identity.

Return:

```json
{
  "principal_type": "user",
  "id": "...",
  "email": "joanna@...",
  "display_name": "Joanna Noriega",
  "role": "admin",
  "businesses": [
    {"id":"...","slug":"redhot","name":"RedHot","role":"admin"}
  ],
  "permissions": {
    "can_view_all_businesses": true,
    "can_manage_users": true,
    "can_restore": true,
    "can_hard_delete": true
  },
  "auth_method": "cloudflare_access"
}
```

**6. Backup Script + Restore Test**

Run backups on Spark #1, not Windows. Use PowerShell 7 via Spark-native `systemd` timers.

`backup_action_tracker.ps1`:

- Uses `pg_dump -Fc -Z 9 --no-owner --no-acl`.
- Dumps only `action_tracker`.
- Writes to `/var/backups/opsmemory/action_tracker/YYYY/MM/`.
- Encrypts with GPG public key.
- Deletes plaintext immediately.
- Keeps local encrypted backups 14 days.
- Rsyncs encrypted dump to Spark #2.
- Writes status JSON for `/readyz`.

Command shape:

```powershell
pg_dump `
  --dbname $env:ACTION_TRACKER_DATABASE_URL `
  --format custom `
  --compress 9 `
  --no-owner `
  --no-acl `
  --file $PlainDump
```

Encryption:

```powershell
gpg --batch --yes --trust-model always `
  --recipient $env:BACKUP_GPG_RECIPIENT `
  --output $EncryptedDump `
  --encrypt $PlainDump
```

Rsync target default:

```text
opsbackup@spark2:/srv/backups/opsmemory/action_tracker/
```

Cadence:
- Daily backup: `02:17 America/Phoenix`.
- Weekly restore test: Sunday `03:05 America/Phoenix`.

`restore_check.ps1`:

1. Pull latest encrypted dump from Spark #2.
2. Decrypt to temp dir.
3. Drop/recreate `action_tracker_restore_test`.
4. `pg_restore --no-owner --no-acl --dbname action_tracker_restore_test latest.dump`.
5. Smoke checks:
   - `schema_migrations` has `0001_initial`.
   - five lifecycle enum types exist.
   - `users` count = 4.
   - `businesses` count = 2.
   - Joanna exists and role = admin.
   - `task_state_transitions` table exists.
6. Drop restore DB unless `KEEP_RESTORE_DB=true`.
7. Write restore status JSON.
8. On failure, POST JSON to `BACKUP_ALERT_WEBHOOK_URL`, preferably an existing n8n alert webhook.

B2 is intentionally deferred to Chunk 1.5.

**7. `docker-compose.yml` Structure**

Use a new OpsMemory compose file. Do not edit existing n8n/openbrain/family compose unless only attaching to an existing external Docker network is required.

Recommended shape:

```yaml
services:
  opsmemory-api:
    build:
      context: .
      dockerfile: api/Dockerfile
    container_name: opsmemory-api
    env_file:
      - .env
    ports:
      - "127.0.0.1:8010:8000"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz').read()"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    networks:
      - spark_internal

networks:
  spark_internal:
    external: true
    name: spark_internal
```

One scope correction: add `api/Dockerfile` and `api/requirements.txt`. Without them, Compose either installs dependencies at runtime or cannot build reproducibly.

`.env.example` must include:

```env
ENVIRONMENT=production
APP_VERSION=chunk1
AUTH_MODE=cloudflare
CF_ACCESS_TEAM_DOMAIN=https://<team>.cloudflareaccess.com
CF_ACCESS_AUD=<aud>
LOCAL_DEV_EMAIL=kyle@kyleconway.ai
ALLOW_DEV_USER_SWITCH=false
DATABASE_URL=postgresql://opsmemory_app:<password>@postgres:5432/action_tracker
DB_POOL_MIN=1
DB_POOL_MAX=10
SERVICE_KEY_PEPPER=<generate-random>
WEB_ROOT=/app/web
READYZ_REQUIRE_BACKUP=false
READYZ_BACKUP_MAX_AGE_HOURS=36
READYZ_RESTORE_MAX_AGE_HOURS=192
BACKUP_STATUS_FILE=/var/lib/opsmemory/backup/status.json
RESTORE_STATUS_FILE=/var/lib/opsmemory/backup/restore_status.json
LOG_LEVEL=INFO
```

Secrets stay out of Git. For Chunk 1, `.env` with strict permissions is acceptable. Move to a real secret store later.

**8. Cloudflared Ingress + DNS + Access Policy**

Ingress rule:

```yaml
ingress:
  - hostname: tracker.kyleconway.ai
    service: http://localhost:8010
  - service: http_status:404
```

DNS:

```text
tracker.kyleconway.ai CNAME b510e94c-8eab-40dd-ae8d-5c933a3896da.cfargotunnel.com
Proxy: enabled
```

Access app:

```text
Name: OpsMemory Tracker
Type: Self-hosted
Domain: tracker.kyleconway.ai
Session duration: 24h
IdP: Google
Policy: Allow OpsMemory Humans
Emails: Kyle, Joanna, Caleb, Sarah exact Google emails
```

Optional service policy:

```text
Policy: Service Auth - OpsMemory automation
Service token: opsmemory-healthcheck / future n8n token
```

Safe order:

1. Start API locally.
2. Verify local health.
3. Create Access app and allow policy.
4. Add tunnel route / DNS.
5. Test externally.

Lockout failure mode:
- Keep Joanna as second admin from day one.
- Keep local SSH recovery: `curl http://127.0.0.1:8010/healthz`.
- Do not enable `AUTH_MODE=local` in production.
- If Access blocks Kyle, fix policy in Cloudflare dashboard; origin stays private.

Cloudflare docs basis: Access JWT validation uses `Cf-Access-Jwt-Assertion`, issuer, AUD, and JWKS; service tokens use `CF-Access-Client-Id` / `CF-Access-Client-Secret`; Tunnel maps public hostnames to local services. Sources: [JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/), [service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/), [Tunnel routing](https://developers.cloudflare.com/tunnel/routing/).

**9. PWA Shell Minimum Viable**

Yes, a `/whoami`-driven screen counts as the shell. It proves DNS, Access, tunnel, API auth, DB seed, and frontend wiring.

Minimum files:

```text
web/index.html
web/app.js
web/manifest.json
web/sw.js
web/icons/icon-192.png
web/icons/icon-512.png
```

UI:
- Header: `OpsMemory`.
- Main state after `/whoami`: `Logged in as Joanna Noriega — admin`.
- Businesses list.
- Empty state: `No open tasks`.
- Error state if SQL auth fails: `This account is not enabled for OpsMemory`.

Service worker:
- `install -> skipWaiting`.
- `activate -> clients.claim`.
- Pass-through `fetch` handler.
- No offline cache/outbox in Chunk 1.

Install test:
1. Open on Joanna’s phone.
2. Complete Access login.
3. Confirm shell renders.
4. Add to Home Screen.
5. Launch from home screen.
6. Confirm standalone display and `/whoami` still works.

**10. Things You’ll Forget**

Logging:
- JSON logs to stdout.
- Docker log rotation: `max-size=10m`, `max-file=5`.
- Redact auth headers and service keys.
- Include request ID.

Secrets:
- `.env` on Spark only, `0640`.
- GPG public key in server keyring.
- Private restore key only where restore checks run.
- No secrets in README.

Time:
- Store all DB timestamps as `timestamptz` UTC.
- Phoenix only for schedules and future digest display.

API versioning:
- Keep `/healthz`, `/readyz`, `/whoami` root for acceptance.
- Add `/v1/whoami` alias now if easy.
- Future write APIs under `/v1`.

CORS:
- No broad CORS.
- Same-origin PWA only.

CSP:
```text
default-src 'self';
script-src 'self';
style-src 'self';
img-src 'self' data:;
connect-src 'self';
manifest-src 'self';
worker-src 'self';
object-src 'none';
base-uri 'self';
frame-ancestors 'none';
```

Rate limiting:
- Defer app-level limiting.
- Rely on Cloudflare Access/WAF for Chunk 1.

DB pooling:
- `asyncpg` pool.
- Min 1, max 10.
- Set `statement_timeout` and `idle_in_transaction_session_timeout`.

Migrations:
- Raw SQL + `schema_migrations` for Chunk 1.
- Do not auto-migrate on every API boot in production.
- Run migration as explicit deploy step.

Other:
- Real seed emails must match Google.
- Cloudflared route order matters.
- Static files need correct MIME types.
- App must trust only Cloudflare JWT, not loose email headers.
- Confirm no `0.0.0.0` host port binding.

**11. Ordering Within Chunk 1**

Commit-sized increments:

1. Repo skeleton, `.env.example`, README deploy runbook.
2. DB migration, seed data, scratch migration test.
3. DB connection/pool and migration verification.
4. Auth dependency and `/whoami`.
5. `/healthz`, `/readyz`, structured errors.
6. PWA shell, manifest, SW, icons.
7. Dockerfile/Compose, no-public-port validation.
8. Cloudflare Access/tunnel deployment notes.
9. Backup script.
10. Restore-check script.
11. Acceptance test runbook and final verification.

Blocking chain:
- Migration blocks auth.
- Auth blocks `/whoami`.
- `/whoami` blocks meaningful PWA shell.
- Local API health blocks cloudflared route.
- First backup blocks final recoverability acceptance.

**12. The One Thing To Defer**

Defer the automated weekly restore-check timer, not the restore test itself.

I would still require:
- Daily backup timer enabled.
- One manual restore test passing.
- Restore procedure documented.

That preserves the “boring, recoverable substrate” while cutting the least risky automation work. I would not defer Cloudflare auth, seed users, no-public-port deployment, or the first restore proof.

**13. Acceptance Test Plan**

DB tests:

```bash
createdb action_tracker_scratch
psql action_tracker_scratch -v ON_ERROR_STOP=1 -f api/migrations/0001_initial.sql
psql action_tracker_scratch -v ON_ERROR_STOP=1 -f api/migrations/0001_initial.sql
```

Validate:

```sql
SELECT count(*) FROM users; -- 4
SELECT count(*) FROM businesses; -- 2
SELECT version FROM schema_migrations WHERE version='0001_initial';
SELECT typname FROM pg_type
WHERE typname IN (
  'task_lifecycle_state',
  'review_lifecycle_state',
  'ingest_lifecycle_state',
  'notification_lifecycle_state',
  'deletion_lifecycle_state'
);
```

Auth tests:
- Missing CF token -> `401`.
- Invalid JWT -> `401`.
- Valid JWT wrong AUD -> `401`.
- Valid JWT unknown email -> `403`.
- Valid Joanna JWT -> `200`, role `admin`.
- `AUTH_MODE=local` -> seeded dev user.
- Invalid `X-OpsMemory-Service-Key` -> `401`.
- Valid service key + CF service auth -> `200`.

Backup tests:
- `backup_action_tracker.ps1` creates encrypted `.dump.gpg`.
- `pg_restore --list` passes before encryption or after decrypt.
- Rsync copy exists on Spark #2.
- Status JSON records success.
- `restore_check.ps1` creates restore DB, validates rows/schema, drops DB.
- Failure path posts alert if webhook configured.

Smoke tests:
- `curl http://127.0.0.1:8010/healthz` on Spark -> `200`.
- `curl http://127.0.0.1:8010/readyz` -> `200`.
- External unauthenticated browser hits Cloudflare Access, not FastAPI.
- External authenticated Joanna sees PWA shell.
- `/whoami` through browser returns Joanna.
- `ss -ltnp` shows API bound to `127.0.0.1`, not `0.0.0.0`.
- Confirm `n8n`, `openbrain`, `family_docs`, `family_health` DBs unchanged before/after.

Chunk 1 is done when DB, auth, PWA shell, tunnel, Access, backup, and restore all pass from a clean deploy path.
tokens used
48,638
**1. Front-To-Back Deploy Trace**

1. Create repo structure in `C:\opsmemory`; do not use `/opt/open-brain`.
2. Author `.env.example` first because API, compose, and scripts all depend on the same env contract.
3. Author `api/migrations/0001_initial.sql`.
4. Author `api/app/db.py`, then `auth.py`, then `health.py`, then `main.py`.
5. Author `web/index.html`, `web/app.js`, `web/manifest.json`, `web/sw.js`, icons.
6. Author `docker-compose.yml`.
7. Author backup/restore scripts.
8. Push to GitHub.
9. On Spark: clone to `/opt/opsmemory`.
10. Create Postgres roles/database only for OpsMemory:

```sql
CREATE ROLE opsmemory_owner LOGIN PASSWORD '<owner-password>';
CREATE ROLE opsmemory_app LOGIN PASSWORD '<app-password>';
CREATE DATABASE action_tracker OWNER opsmemory_owner;
GRANT CONNECT ON DATABASE action_tracker TO opsmemory_app;
```

11. Run `0001_initial.sql` against `action_tracker` as `opsmemory_owner`.
12. Verify seeds: 4 users, 2 businesses, 5 lifecycle enums.
13. Create production `.env` on Spark at `/opt/opsmemory/.env`, mode `0640`, not committed.
14. Start API with Docker Compose, binding only `127.0.0.1:8010`.
15. First local checks:

```bash
curl http://127.0.0.1:8010/healthz
curl http://127.0.0.1:8010/readyz
```

16. Create Cloudflare Access application for `tracker.kyleconway.ai` before DNS points there.
17. Apply Access allow policy for Kyle, Joanna, Caleb, Sarah.
18. Add cloudflared ingress: `tracker.kyleconway.ai -> http://localhost:8010`.
19. Create proxied DNS CNAME to the existing tunnel.
20. Test external Access login.
21. Run first backup manually.
22. Run restore check manually.
23. Enable daily backup timer and weekly restore-check timer.
24. Flip `READYZ_REQUIRE_BACKUP=true` after first successful backup/restore.
25. Final check: Joanna logs in from phone, PWA shell renders, `/whoami` returns admin.

**2. Back-To-Front User Trace**

Joanna opens `https://tracker.kyleconway.ai`.

Required path:

1. Phone DNS resolves `tracker.kyleconway.ai`.
2. Cloudflare sees proxied CNAME to tunnel `b510e94c-8eab-40dd-ae8d-5c933a3896da.cfargotunnel.com`.
3. Cloudflare Access application matches hostname.
4. No valid `CF_Authorization` cookie, so Access redirects to Google SSO.
5. Joanna authenticates with Google.
6. Cloudflare checks policy: Joanna’s exact email must be allowed.
7. Cloudflare issues Access JWT/cookie.
8. Request resumes through Cloudflare Tunnel.
9. Existing `cloudflared` on Spark receives request over outbound tunnel.
10. Ingress sends request to `http://localhost:8010`.
11. Docker host forwards `127.0.0.1:8010` to `opsmemory-api:8000`.
12. FastAPI serves PWA shell.
13. Browser runs `app.js` and requests `/whoami`.
14. Cloudflare adds `Cf-Access-Jwt-Assertion` header to origin request.
15. `auth.py` verifies JWT signature, issuer, audience, expiry.
16. `auth.py` extracts email, lowercases it, queries `user_identities` + `users`.
17. SQL proves Joanna is active admin.
18. `/whoami` returns user, role, businesses, permissions.
19. UI renders `Logged in as Joanna Noriega — admin`.
20. Browser registers `sw.js`.

Missing any of these fails Chunk 1.

**3. Full `0001_initial.sql`**

Ambiguity: exact Google emails are unknown. Replace the four `@kyleconway.ai` seed emails with the real emails before production Access testing.

```sql
BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

DO $$ BEGIN
  CREATE TYPE app_role AS ENUM ('admin', 'owner', 'service');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE user_status AS ENUM ('active', 'disabled');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE task_lifecycle_state AS ENUM ('open', 'done');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE review_lifecycle_state AS ENUM (
    'pending',
    'approved',
    'rejected',
    'needs_changes',
    'superseded'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE ingest_lifecycle_state AS ENUM (
    'received',
    'extracting',
    'pending_review',
    'completed',
    'failed',
    'cancelled'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE notification_lifecycle_state AS ENUM (
    'pending',
    'scheduled',
    'sent',
    'failed',
    'cancelled',
    'suppressed'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
  CREATE TYPE deletion_lifecycle_state AS ENUM (
    'active',
    'soft_deleted',
    'restore_requested',
    'restored',
    'hard_delete_eligible',
    'hard_deleted'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  description text NOT NULL,
  checksum text,
  applied_at timestamptz NOT NULL DEFAULT now(),
  execution_ms integer,
  dirty boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS businesses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug citext NOT NULL UNIQUE,
  name text NOT NULL,
  deletion_state deletion_lifecycle_state NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  CHECK (slug::text ~ '^[a-z0-9][a-z0-9-]*$'),
  CHECK (jsonb_typeof(metadata) = 'object'),
  CHECK (
    (deletion_state = 'active' AND deleted_at IS NULL)
    OR deletion_state <> 'active'
  )
);

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email citext NOT NULL UNIQUE,
  display_name text NOT NULL,
  role app_role NOT NULL,
  status user_status NOT NULL DEFAULT 'active',
  timezone text NOT NULL DEFAULT 'America/Phoenix',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz,
  CHECK (role IN ('admin', 'owner')),
  CHECK (position('@' in email::text) > 1),
  CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE TABLE IF NOT EXISTS user_identities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider text NOT NULL,
  provider_subject text,
  email citext NOT NULL,
  claims jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_authenticated_at timestamptz,
  CHECK (provider IN ('cloudflare_access', 'google')),
  CHECK (position('@' in email::text) > 1),
  CHECK (jsonb_typeof(claims) = 'object')
);

CREATE UNIQUE INDEX IF NOT EXISTS user_identities_provider_subject_uidx
  ON user_identities(provider, provider_subject)
  WHERE provider_subject IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS user_identities_provider_email_uidx
  ON user_identities(provider, email);

CREATE INDEX IF NOT EXISTS user_identities_user_id_idx
  ON user_identities(user_id);

CREATE TABLE IF NOT EXISTS business_memberships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role app_role NOT NULL,
  status user_status NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (role IN ('admin', 'owner')),
  UNIQUE (business_id, user_id)
);

CREATE INDEX IF NOT EXISTS business_memberships_user_id_idx
  ON business_memberships(user_id);

CREATE TABLE IF NOT EXISTS service_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  description text,
  role app_role NOT NULL DEFAULT 'service',
  status user_status NOT NULL DEFAULT 'active',
  key_prefix text NOT NULL UNIQUE,
  key_hash text NOT NULL,
  scopes text[] NOT NULL DEFAULT ARRAY[]::text[],
  created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  expires_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (role = 'service'),
  CHECK (length(key_prefix) >= 8),
  CHECK (key_hash ~ '^[a-f0-9]{64}$'),
  CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE TABLE IF NOT EXISTS task_state_transitions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id uuid NOT NULL,
  business_id uuid NOT NULL REFERENCES businesses(id) ON DELETE RESTRICT,
  from_state task_lifecycle_state,
  to_state task_lifecycle_state NOT NULL,
  actor_kind text NOT NULL,
  actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  actor_service_account_id uuid REFERENCES service_accounts(id) ON DELETE SET NULL,
  reason text,
  request_id text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  transitioned_at timestamptz NOT NULL DEFAULT now(),
  CHECK (from_state IS NULL OR from_state <> to_state),
  CHECK (actor_kind IN ('user', 'service', 'system', 'migration')),
  CHECK (
    (actor_kind = 'user' AND actor_user_id IS NOT NULL AND actor_service_account_id IS NULL)
    OR (actor_kind = 'service' AND actor_user_id IS NULL AND actor_service_account_id IS NOT NULL)
    OR (actor_kind IN ('system', 'migration') AND actor_user_id IS NULL AND actor_service_account_id IS NULL)
  ),
  CHECK (jsonb_typeof(metadata) = 'object')
);

CREATE INDEX IF NOT EXISTS task_state_transitions_task_id_idx
  ON task_state_transitions(task_id);

CREATE INDEX IF NOT EXISTS task_state_transitions_business_time_idx
  ON task_state_transitions(business_id, transitioned_at DESC);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_businesses_updated_at') THEN
    CREATE TRIGGER trg_businesses_updated_at
    BEFORE UPDATE ON businesses
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_users_updated_at') THEN
    CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_user_identities_updated_at') THEN
    CREATE TRIGGER trg_user_identities_updated_at
    BEFORE UPDATE ON user_identities
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_business_memberships_updated_at') THEN
    CREATE TRIGGER trg_business_memberships_updated_at
    BEFORE UPDATE ON business_memberships
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_service_accounts_updated_at') THEN
    CREATE TRIGGER trg_service_accounts_updated_at
    BEFORE UPDATE ON service_accounts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
  END IF;
END $$;

INSERT INTO businesses (id, slug, name)
VALUES
  ('00000000-0000-0000-0000-000000000201', 'redhot', 'RedHot'),
  ('00000000-0000-0000-0000-000000000202', 'borderline', 'Borderline')
ON CONFLICT (id) DO UPDATE
SET slug = EXCLUDED.slug,
    name = EXCLUDED.name,
    updated_at = now();

INSERT INTO users (id, email, display_name, role)
VALUES
  ('00000000-0000-0000-0000-000000000101', 'kyle@kyleconway.ai', 'Kyle Conway', 'admin'),
  ('00000000-0000-0000-0000-000000000102', 'joanna@kyleconway.ai', 'Joanna Noriega', 'admin'),
  ('00000000-0000-0000-0000-000000000103', 'caleb@kyleconway.ai', 'Caleb Conway', 'owner'),
  ('00000000-0000-0000-0000-000000000104', 'sarah@kyleconway.ai', 'Sarah Conway', 'owner')
ON CONFLICT (id) DO UPDATE
SET email = EXCLUDED.email,
    display_name = EXCLUDED.display_name,
    role = EXCLUDED.role,
    updated_at = now();

INSERT INTO user_identities (user_id, provider, provider_subject, email)
SELECT id, 'cloudflare_access', NULL, email
FROM users
ON CONFLICT (provider, email) DO UPDATE
SET user_id = EXCLUDED.user_id,
    updated_at = now();

INSERT INTO business_memberships (business_id, user_id, role)
VALUES
  ('00000000-0000-0000-0000-000000000201', '00000000-0000-0000-0000-000000000101', 'admin'),
  ('00000000-0000-0000-0000-000000000202', '00000000-0000-0000-0000-000000000101', 'admin'),
  ('00000000-0000-0000-0000-000000000201', '00000000-0000-0000-0000-000000000102', 'admin'),
  ('00000000-0000-0000-0000-000000000202', '00000000-0000-0000-0000-000000000102', 'admin'),
  ('00000000-0000-0000-0000-000000000201', '00000000-0000-0000-0000-000000000103', 'owner'),
  ('00000000-0000-0000-0000-000000000202', '00000000-0000-0000-0000-000000000104', 'owner')
ON CONFLICT (business_id, user_id) DO UPDATE
SET role = EXCLUDED.role,
    updated_at = now();

INSERT INTO schema_migrations (version, description)
VALUES ('0001_initial', 'Chunk 1 substrate: auth, businesses, lifecycle enums, seed data')
ON CONFLICT (version) DO UPDATE
SET description = EXCLUDED.description,
    applied_at = now(),
    dirty = false;

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opsmemory_app') THEN
    GRANT USAGE ON SCHEMA public TO opsmemory_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO opsmemory_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO opsmemory_app;
  END IF;
END $$;

COMMIT;
```

`pgvector` is deferred until LLM/embedding work. Use `pgcrypto` instead of `uuid-ossp`.

**4. Auth Flow Concrete**

Use `PyJWT[crypto]` for Cloudflare JWT verification. Cloudflare’s docs specify the `Cf-Access-Jwt-Assertion` origin header, issuer as the team domain, and audience as the Access application AUD.

Recommended env:

```env
AUTH_MODE=cloudflare
CF_ACCESS_TEAM_DOMAIN=https://<team-name>.cloudflareaccess.com
CF_ACCESS_AUD=<tracker-application-aud>
LOCAL_DEV_EMAIL=kyle@kyleconway.ai
ALLOW_DEV_USER_SWITCH=false
SERVICE_KEY_PEPPER=<long-random-secret>
```

Human auth:

1. Require `Cf-Access-Jwt-Assertion`.
2. Verify RS256 signature using `${CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs`.
3. Verify issuer and audience.
4. Require `email`.
5. Look up `user_identities.provider='cloudflare_access'` by email.
6. Require active user.
7. Return principal.

Service-account auth:

1. Require Cloudflare Access service auth at the edge.
2. App also requires `X-OpsMemory-Service-Key`.
3. Hash key with HMAC-SHA256 using `SERVICE_KEY_PEPPER`.
4. Match `service_accounts.key_prefix` and `key_hash`.
5. Require active and not expired.

Do not use `Authorization` for app service keys; Cloudflare Access may use it for service-token configuration.

`auth.py` shape:

```python
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Literal

import jwt
from fastapi import HTTPException, Request, status
from jwt import InvalidTokenError, PyJWKClient


@dataclass(frozen=True)
class Principal:
    principal_type: Literal["user", "service"]
    id: str
    display_name: str
    email: str | None
    role: str
    businesses: list[dict[str, Any]]
    permissions: dict[str, bool]
    auth_method: str


_jwk_client: PyJWKClient | None = None


def _team_domain() -> str:
    raw = os.environ["CF_ACCESS_TEAM_DOMAIN"].rstrip("/")
    if not raw.startswith("https://"):
        raw = f"https://{raw}"
    return raw


def _jwks() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(f"{_team_domain()}/cdn-cgi/access/certs")
    return _jwk_client


def _permissions(role: str, scopes: list[str] | None = None) -> dict[str, bool]:
    scopes = scopes or []
    if role == "admin":
        return {
            "can_view_all_businesses": True,
            "can_manage_users": True,
            "can_restore": True,
            "can_hard_delete": True,
            "can_use_service_api": False,
        }
    if role == "owner":
        return {
            "can_view_all_businesses": False,
            "can_manage_users": False,
            "can_restore": False,
            "can_hard_delete": False,
            "can_use_service_api": False,
        }
    return {
        "can_view_all_businesses": "businesses:read" in scopes,
        "can_manage_users": False,
        "can_restore": False,
        "can_hard_delete": False,
        "can_use_service_api": True,
    }


def _verify_cf_jwt(request: Request, require_email: bool) -> dict[str, Any]:
    token = request.headers.get("Cf-Access-Jwt-Assertion")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing access token")

    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=os.environ["CF_ACCESS_AUD"],
            issuer=_team_domain(),
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid access token")

    if require_email and not claims.get("email"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing access email")

    return claims


async def _load_user(request: Request, email: str, claims: dict[str, Any], auth_method: str) -> Principal:
    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              ui.id AS identity_id,
              u.id::text AS id,
              u.email::text AS email,
              u.display_name,
              u.role::text AS role
            FROM user_identities ui
            JOIN users u ON u.id = ui.user_id
            WHERE ui.provider = 'cloudflare_access'
              AND ui.email = $1
              AND u.status = 'active'
            """,
            email.lower(),
        )
        if not row:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user not authorized")

        businesses = await conn.fetch(
            """
            SELECT b.id::text AS id, b.slug::text AS slug, b.name, bm.role::text AS role
            FROM business_memberships bm
            JOIN businesses b ON b.id = bm.business_id
            WHERE bm.user_id = $1::uuid
              AND bm.status = 'active'
              AND b.deletion_state = 'active'
            ORDER BY b.name
            """,
            row["id"],
        )

        claim_subset = {
            "sub": claims.get("sub"),
            "email": claims.get("email"),
            "iss": claims.get("iss"),
            "aud": claims.get("aud"),
            "iat": claims.get("iat"),
            "exp": claims.get("exp"),
        }

        await conn.execute(
            """
            UPDATE user_identities
            SET last_authenticated_at = now(), claims = $2::jsonb
            WHERE id = $1
            """,
            row["identity_id"],
            json.dumps(claim_subset),
        )

        await conn.execute(
            "UPDATE users SET last_seen_at = now() WHERE id = $1::uuid",
            row["id"],
        )

    return Principal(
        principal_type="user",
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        businesses=[dict(b) for b in businesses],
        permissions=_permissions(row["role"]),
        auth_method=auth_method,
    )


async def _load_service(request: Request, raw_key: str) -> Principal:
    pepper = os.environ.get("SERVICE_KEY_PEPPER")
    if not pepper:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="service auth unavailable")

    key_prefix = raw_key[:16]
    key_hash = hmac.new(pepper.encode(), raw_key.encode(), hashlib.sha256).hexdigest()

    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text, name, key_hash, scopes
            FROM service_accounts
            WHERE key_prefix = $1
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > now())
            """,
            key_prefix,
        )

        if not row or not hmac.compare_digest(row["key_hash"], key_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service key")

        await conn.execute(
            "UPDATE service_accounts SET last_used_at = now() WHERE id = $1::uuid",
            row["id"],
        )

    scopes = list(row["scopes"] or [])
    return Principal(
        principal_type="service",
        id=row["id"],
        email=None,
        display_name=row["name"],
        role="service",
        businesses=[],
        permissions=_permissions("service", scopes),
        auth_method="service_key",
    )


async def require_principal(request: Request) -> Principal:
    auth_mode = os.environ.get("AUTH_MODE", "cloudflare")

    service_key = request.headers.get("X-OpsMemory-Service-Key")
    if service_key:
        if auth_mode == "cloudflare":
            _verify_cf_jwt(request, require_email=False)
        return await _load_service(request, service_key)

    if auth_mode == "local":
        email = os.environ.get("LOCAL_DEV_EMAIL", "kyle@kyleconway.ai")
        if os.environ.get("ALLOW_DEV_USER_SWITCH") == "true":
            email = request.headers.get("X-Dev-User-Email", email)
        return await _load_user(request, email, {"email": email}, "local_dev")

    claims = _verify_cf_jwt(request, require_email=True)
    return await _load_user(request, claims["email"], claims, "cloudflare_access")
```

Auth should be a route dependency, not global middleware. Middleware should handle request IDs, logging, security headers, and proxy headers only.

**5. Healthcheck Designs**

`/healthz`: liveness only.

Checks:
- Process is alive.
- App can return JSON.
- No DB dependency.
- No auth dependency.

Response:

```json
{"ok":true,"service":"opsmemory-api","version":"chunk1","time":"2026-05-04T00:00:00Z"}
```

`/readyz`: readiness.

Checks:
- DB pool available.
- `SELECT 1` succeeds.
- `schema_migrations` contains `0001_initial`.
- Optional backup status file is recent.

Production after first backup:

```env
READYZ_REQUIRE_BACKUP=true
READYZ_BACKUP_MAX_AGE_HOURS=36
READYZ_RESTORE_MAX_AGE_HOURS=192
```

Before first backup, keep `READYZ_REQUIRE_BACKUP=false` to avoid bootstrapping failure.

`/whoami`: authenticated identity.

Return:

```json
{
  "principal_type": "user",
  "id": "...",
  "email": "joanna@...",
  "display_name": "Joanna Noriega",
  "role": "admin",
  "businesses": [
    {"id":"...","slug":"redhot","name":"RedHot","role":"admin"}
  ],
  "permissions": {
    "can_view_all_businesses": true,
    "can_manage_users": true,
    "can_restore": true,
    "can_hard_delete": true
  },
  "auth_method": "cloudflare_access"
}
```

**6. Backup Script + Restore Test**

Run backups on Spark #1, not Windows. Use PowerShell 7 via Spark-native `systemd` timers.

`backup_action_tracker.ps1`:

- Uses `pg_dump -Fc -Z 9 --no-owner --no-acl`.
- Dumps only `action_tracker`.
- Writes to `/var/backups/opsmemory/action_tracker/YYYY/MM/`.
- Encrypts with GPG public key.
- Deletes plaintext immediately.
- Keeps local encrypted backups 14 days.
- Rsyncs encrypted dump to Spark #2.
- Writes status JSON for `/readyz`.

Command shape:

```powershell
pg_dump `
  --dbname $env:ACTION_TRACKER_DATABASE_URL `
  --format custom `
  --compress 9 `
  --no-owner `
  --no-acl `
  --file $PlainDump
```

Encryption:

```powershell
gpg --batch --yes --trust-model always `
  --recipient $env:BACKUP_GPG_RECIPIENT `
  --output $EncryptedDump `
  --encrypt $PlainDump
```

Rsync target default:

```text
opsbackup@spark2:/srv/backups/opsmemory/action_tracker/
```

Cadence:
- Daily backup: `02:17 America/Phoenix`.
- Weekly restore test: Sunday `03:05 America/Phoenix`.

`restore_check.ps1`:

1. Pull latest encrypted dump from Spark #2.
2. Decrypt to temp dir.
3. Drop/recreate `action_tracker_restore_test`.
4. `pg_restore --no-owner --no-acl --dbname action_tracker_restore_test latest.dump`.
5. Smoke checks:
   - `schema_migrations` has `0001_initial`.
   - five lifecycle enum types exist.
   - `users` count = 4.
   - `businesses` count = 2.
   - Joanna exists and role = admin.
   - `task_state_transitions` table exists.
6. Drop restore DB unless `KEEP_RESTORE_DB=true`.
7. Write restore status JSON.
8. On failure, POST JSON to `BACKUP_ALERT_WEBHOOK_URL`, preferably an existing n8n alert webhook.

B2 is intentionally deferred to Chunk 1.5.

**7. `docker-compose.yml` Structure**

Use a new OpsMemory compose file. Do not edit existing n8n/openbrain/family compose unless only attaching to an existing external Docker network is required.

Recommended shape:

```yaml
services:
  opsmemory-api:
    build:
      context: .
      dockerfile: api/Dockerfile
    container_name: opsmemory-api
    env_file:
      - .env
    ports:
      - "127.0.0.1:8010:8000"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz').read()"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    networks:
      - spark_internal

networks:
  spark_internal:
    external: true
    name: spark_internal
```

One scope correction: add `api/Dockerfile` and `api/requirements.txt`. Without them, Compose either installs dependencies at runtime or cannot build reproducibly.

`.env.example` must include:

```env
ENVIRONMENT=production
APP_VERSION=chunk1
AUTH_MODE=cloudflare
CF_ACCESS_TEAM_DOMAIN=https://<team>.cloudflareaccess.com
CF_ACCESS_AUD=<aud>
LOCAL_DEV_EMAIL=kyle@kyleconway.ai
ALLOW_DEV_USER_SWITCH=false
DATABASE_URL=postgresql://opsmemory_app:<password>@postgres:5432/action_tracker
DB_POOL_MIN=1
DB_POOL_MAX=10
SERVICE_KEY_PEPPER=<generate-random>
WEB_ROOT=/app/web
READYZ_REQUIRE_BACKUP=false
READYZ_BACKUP_MAX_AGE_HOURS=36
READYZ_RESTORE_MAX_AGE_HOURS=192
BACKUP_STATUS_FILE=/var/lib/opsmemory/backup/status.json
RESTORE_STATUS_FILE=/var/lib/opsmemory/backup/restore_status.json
LOG_LEVEL=INFO
```

Secrets stay out of Git. For Chunk 1, `.env` with strict permissions is acceptable. Move to a real secret store later.

**8. Cloudflared Ingress + DNS + Access Policy**

Ingress rule:

```yaml
ingress:
  - hostname: tracker.kyleconway.ai
    service: http://localhost:8010
  - service: http_status:404
```

DNS:

```text
tracker.kyleconway.ai CNAME b510e94c-8eab-40dd-ae8d-5c933a3896da.cfargotunnel.com
Proxy: enabled
```

Access app:

```text
Name: OpsMemory Tracker
Type: Self-hosted
Domain: tracker.kyleconway.ai
Session duration: 24h
IdP: Google
Policy: Allow OpsMemory Humans
Emails: Kyle, Joanna, Caleb, Sarah exact Google emails
```

Optional service policy:

```text
Policy: Service Auth - OpsMemory automation
Service token: opsmemory-healthcheck / future n8n token
```

Safe order:

1. Start API locally.
2. Verify local health.
3. Create Access app and allow policy.
4. Add tunnel route / DNS.
5. Test externally.

Lockout failure mode:
- Keep Joanna as second admin from day one.
- Keep local SSH recovery: `curl http://127.0.0.1:8010/healthz`.
- Do not enable `AUTH_MODE=local` in production.
- If Access blocks Kyle, fix policy in Cloudflare dashboard; origin stays private.

Cloudflare docs basis: Access JWT validation uses `Cf-Access-Jwt-Assertion`, issuer, AUD, and JWKS; service tokens use `CF-Access-Client-Id` / `CF-Access-Client-Secret`; Tunnel maps public hostnames to local services. Sources: [JWT validation](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/), [service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/), [Tunnel routing](https://developers.cloudflare.com/tunnel/routing/).

**9. PWA Shell Minimum Viable**

Yes, a `/whoami`-driven screen counts as the shell. It proves DNS, Access, tunnel, API auth, DB seed, and frontend wiring.

Minimum files:

```text
web/index.html
web/app.js
web/manifest.json
web/sw.js
web/icons/icon-192.png
web/icons/icon-512.png
```

UI:
- Header: `OpsMemory`.
- Main state after `/whoami`: `Logged in as Joanna Noriega — admin`.
- Businesses list.
- Empty state: `No open tasks`.
- Error state if SQL auth fails: `This account is not enabled for OpsMemory`.

Service worker:
- `install -> skipWaiting`.
- `activate -> clients.claim`.
- Pass-through `fetch` handler.
- No offline cache/outbox in Chunk 1.

Install test:
1. Open on Joanna’s phone.
2. Complete Access login.
3. Confirm shell renders.
4. Add to Home Screen.
5. Launch from home screen.
6. Confirm standalone display and `/whoami` still works.

**10. Things You’ll Forget**

Logging:
- JSON logs to stdout.
- Docker log rotation: `max-size=10m`, `max-file=5`.
- Redact auth headers and service keys.
- Include request ID.

Secrets:
- `.env` on Spark only, `0640`.
- GPG public key in server keyring.
- Private restore key only where restore checks run.
- No secrets in README.

Time:
- Store all DB timestamps as `timestamptz` UTC.
- Phoenix only for schedules and future digest display.

API versioning:
- Keep `/healthz`, `/readyz`, `/whoami` root for acceptance.
- Add `/v1/whoami` alias now if easy.
- Future write APIs under `/v1`.

CORS:
- No broad CORS.
- Same-origin PWA only.

CSP:
```text
default-src 'self';
script-src 'self';
style-src 'self';
img-src 'self' data:;
connect-src 'self';
manifest-src 'self';
worker-src 'self';
object-src 'none';
base-uri 'self';
frame-ancestors 'none';
```

Rate limiting:
- Defer app-level limiting.
- Rely on Cloudflare Access/WAF for Chunk 1.

DB pooling:
- `asyncpg` pool.
- Min 1, max 10.
- Set `statement_timeout` and `idle_in_transaction_session_timeout`.

Migrations:
- Raw SQL + `schema_migrations` for Chunk 1.
- Do not auto-migrate on every API boot in production.
- Run migration as explicit deploy step.

Other:
- Real seed emails must match Google.
- Cloudflared route order matters.
- Static files need correct MIME types.
- App must trust only Cloudflare JWT, not loose email headers.
- Confirm no `0.0.0.0` host port binding.

**11. Ordering Within Chunk 1**

Commit-sized increments:

1. Repo skeleton, `.env.example`, README deploy runbook.
2. DB migration, seed data, scratch migration test.
3. DB connection/pool and migration verification.
4. Auth dependency and `/whoami`.
5. `/healthz`, `/readyz`, structured errors.
6. PWA shell, manifest, SW, icons.
7. Dockerfile/Compose, no-public-port validation.
8. Cloudflare Access/tunnel deployment notes.
9. Backup script.
10. Restore-check script.
11. Acceptance test runbook and final verification.

Blocking chain:
- Migration blocks auth.
- Auth blocks `/whoami`.
- `/whoami` blocks meaningful PWA shell.
- Local API health blocks cloudflared route.
- First backup blocks final recoverability acceptance.

**12. The One Thing To Defer**

Defer the automated weekly restore-check timer, not the restore test itself.

I would still require:
- Daily backup timer enabled.
- One manual restore test passing.
- Restore procedure documented.

That preserves the “boring, recoverable substrate” while cutting the least risky automation work. I would not defer Cloudflare auth, seed users, no-public-port deployment, or the first restore proof.

**13. Acceptance Test Plan**

DB tests:

```bash
createdb action_tracker_scratch
psql action_tracker_scratch -v ON_ERROR_STOP=1 -f api/migrations/0001_initial.sql
psql action_tracker_scratch -v ON_ERROR_STOP=1 -f api/migrations/0001_initial.sql
```

Validate:

```sql
SELECT count(*) FROM users; -- 4
SELECT count(*) FROM businesses; -- 2
SELECT version FROM schema_migrations WHERE version='0001_initial';
SELECT typname FROM pg_type
WHERE typname IN (
  'task_lifecycle_state',
  'review_lifecycle_state',
  'ingest_lifecycle_state',
  'notification_lifecycle_state',
  'deletion_lifecycle_state'
);
```

Auth tests:
- Missing CF token -> `401`.
- Invalid JWT -> `401`.
- Valid JWT wrong AUD -> `401`.
- Valid JWT unknown email -> `403`.
- Valid Joanna JWT -> `200`, role `admin`.
- `AUTH_MODE=local` -> seeded dev user.
- Invalid `X-OpsMemory-Service-Key` -> `401`.
- Valid service key + CF service auth -> `200`.

Backup tests:
- `backup_action_tracker.ps1` creates encrypted `.dump.gpg`.
- `pg_restore --list` passes before encryption or after decrypt.
- Rsync copy exists on Spark #2.
- Status JSON records success.
- `restore_check.ps1` creates restore DB, validates rows/schema, drops DB.
- Failure path posts alert if webhook configured.

Smoke tests:
- `curl http://127.0.0.1:8010/healthz` on Spark -> `200`.
- `curl http://127.0.0.1:8010/readyz` -> `200`.
- External unauthenticated browser hits Cloudflare Access, not FastAPI.
- External authenticated Joanna sees PWA shell.
- `/whoami` through browser returns Joanna.
- `ss -ltnp` shows API bound to `127.0.0.1`, not `0.0.0.0`.
- Confirm `n8n`, `openbrain`, `family_docs`, `family_health` DBs unchanged before/after.

Chunk 1 is done when DB, auth, PWA shell, tunnel, Access, backup, and restore all pass from a clean deploy path.

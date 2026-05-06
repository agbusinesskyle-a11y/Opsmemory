#!/usr/bin/env python3
"""Seed OpsMemory owners, user_identities, and business_memberships.

Reads owner data from one of (in order):

  1. $OPSMEMORY_OWNERS_JSON env var (JSON literal)
  2. $OPSMEMORY_OWNERS_FILE env var (path to JSON file)
  3. .local/owners.json relative to repo root

Seed file format::

    {
      "owners": [
        {
          "id": "00000000-0000-0000-0000-000000000101",
          "email": "kyle@example.com",
          "display_name": "Kyle Conway",
          "role": "admin",
          "businesses": ["redhot", "borderline"]
        },
        ...
      ]
    }

Each owner gets:
  - users row (UPSERT on id)
  - user_identities row (provider=cloudflare_access, UPSERT on (provider, email))
  - business_memberships rows (one per business, UPSERT on (business_id, user_id))

Idempotent. Safe to re-run. Owner removal is NOT handled — delete via
direct DB edit + Cloudflare Access policy update.

Run this AFTER api/migrations/0001_initial.sql is applied:

    docker cp .local/owners.json <pg-container>:/tmp/owners.json
    docker exec -i <pg-container> psql -U opsmemory_owner -d action_tracker -f - <<SQL ...

OR, more idiomatically, run via docker exec from the host with -i:

    OPSMEMORY_OWNERS_FILE=.local/owners.json python3 scripts/seed_initial.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BUSINESS_IDS = {
    "redhot": "00000000-0000-0000-0000-000000000201",
    "borderline": "00000000-0000-0000-0000-000000000202",
}


def load_owners() -> list[dict[str, Any]]:
    raw = os.environ.get("OPSMEMORY_OWNERS_JSON")
    if raw:
        return json.loads(raw)["owners"]

    file_env = os.environ.get("OPSMEMORY_OWNERS_FILE")
    if file_env:
        return json.loads(Path(file_env).read_text())["owners"]

    default = REPO_ROOT / ".local" / "owners.json"
    if default.exists():
        return json.loads(default.read_text())["owners"]

    raise SystemExit(
        "ERROR: no owners source. Set OPSMEMORY_OWNERS_JSON, "
        "OPSMEMORY_OWNERS_FILE, or create .local/owners.json"
    )


def validate(owner: dict[str, Any]) -> None:
    for k in ("id", "email", "display_name", "role"):
        if not owner.get(k):
            raise SystemExit(f"ERROR: owner missing required field {k!r}: {owner!r}")
    if owner["role"] not in ("admin", "owner"):
        raise SystemExit(f"ERROR: invalid role {owner['role']!r}: must be admin|owner")
    if "@" not in owner["email"]:
        raise SystemExit(f"ERROR: invalid email {owner['email']!r}")
    for slug in owner.get("businesses", []):
        if slug not in DEFAULT_BUSINESS_IDS:
            raise SystemExit(
                f"ERROR: unknown business slug {slug!r}; "
                f"add it to DEFAULT_BUSINESS_IDS or seed it via 0001_initial.sql"
            )


def apply_via_psql(args: list[str], sql_body: str) -> None:
    """Run psql with -v args, piping a multi-statement SQL body to stdin.

    Used for the seed transaction. psql interpolates :'varname' in the
    body using safe SQL-literal escaping, eliminating the SQL-injection
    risk that f-string interpolation has with operator-controlled
    display_name / email.
    """
    container = os.environ.get("POSTGRES_CONTAINER", "postgres")
    role = os.environ.get("ACTION_TRACKER_DB_ROLE", "opsmemory_owner")
    db = os.environ.get("ACTION_TRACKER_DB_NAME", "action_tracker")
    cmd = ["docker", "exec", "-i", container, "psql",
           "-U", role, "-d", db, "-v", "ON_ERROR_STOP=1"] + args
    proc = subprocess.run(cmd, input=sql_body, text=True, capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"ERROR: seed psql exited {proc.returncode}")
    print(proc.stdout.strip() or "ok")


def seed_one_owner(owner: dict[str, Any]) -> None:
    """Apply the three INSERT/UPSERT statements for a single owner via psql -v."""
    sql = """
INSERT INTO users (id, email, display_name, role) VALUES
  (:'id'::uuid, :'email', :'display_name', :'role')
ON CONFLICT (id) DO UPDATE SET
  email = EXCLUDED.email,
  display_name = EXCLUDED.display_name,
  role = EXCLUDED.role,
  updated_at = now();

INSERT INTO user_identities (user_id, provider, provider_subject, email) VALUES
  (:'id'::uuid, 'cloudflare_access', NULL, :'email')
ON CONFLICT (provider, email) DO UPDATE SET
  user_id = EXCLUDED.user_id,
  updated_at = now();
""".strip()
    apply_via_psql(
        [
            "-v", f"id={owner['id']}",
            "-v", f"email={owner['email']}",
            "-v", f"display_name={owner['display_name']}",
            "-v", f"role={owner['role']}",
        ],
        sql,
    )

    for slug in owner.get("businesses", []):
        biz_id = DEFAULT_BUSINESS_IDS[slug]
        membership_sql = """
INSERT INTO business_memberships (business_id, user_id, role) VALUES
  (:'biz_id'::uuid, :'user_id'::uuid, :'role')
ON CONFLICT (business_id, user_id) DO UPDATE SET
  role = EXCLUDED.role,
  updated_at = now();
""".strip()
        apply_via_psql(
            [
                "-v", f"biz_id={biz_id}",
                "-v", f"user_id={owner['id']}",
                "-v", f"role={owner['role']}",
            ],
            membership_sql,
        )


def main() -> int:
    owners = load_owners()
    if not owners:
        raise SystemExit("ERROR: owners list is empty")
    for o in owners:
        validate(o)
    print(f"Seeding {len(owners)} owner(s) via parameterized psql...")
    for o in owners:
        seed_one_owner(o)
    print(f"Seeded {len(owners)} owners.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

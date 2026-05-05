#!/usr/bin/env python3
"""OpsMemory migration runner.

Replaces ad-hoc `docker cp + docker exec psql -f` for applying SQL
migrations. Owns the outer transaction, records checksum + execution_ms,
holds a Postgres advisory lock so concurrent deploys can't race, and
refuses to proceed on checksum drift or dirty rows.

Migrations live at `api/migrations/<NNNN>_<slug>.sql`. They run in
lexicographic order. Each file must NOT contain top-level
`BEGIN;` / `COMMIT;` / `ROLLBACK;` — the runner wraps the whole apply
in its own transaction.

Existing deploys (chunk1) have a `schema_migrations` row for
`0001_initial` with `checksum IS NULL`. First run after this script
ships will compute the current file's checksum and backfill the row.
Drift detection is armed from then on.

Usage:
  python3 scripts/migrate.py                # apply pending, normal mode
  python3 scripts/migrate.py --status       # show what's applied vs pending
  python3 scripts/migrate.py --dry-run      # show plan without applying
  python3 scripts/migrate.py --force-backfill   # backfill NULL checksums
                                             # without applying anything else

Exit codes:
  0  success — all migrations applied or already up-to-date
  1  checksum drift (file changed after apply)
  2  dirty row found (previous run failed mid-migration; manual repair)
  3  SQL error during apply
  4  file system error (migrations dir missing, file unreadable)
  5  configuration error (e.g. POSTGRES_CONTAINER missing)
  6  forbidden top-level BEGIN/COMMIT/ROLLBACK in a migration file
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "api" / "migrations"

# Stable bigint for pg_advisory_xact_lock. Derived once from a fixed string.
# int.from_bytes(hashlib.sha256(b'opsmemory.migration_runner').digest()[:8],
#                'big', signed=True)
ADVISORY_LOCK_ID = -2845817902478063167

# Match top-level BEGIN/COMMIT/ROLLBACK lines (case-insensitive). Tolerates
# leading whitespace and a single trailing comment.
_FORBIDDEN_TXN = re.compile(
    r"^\s*(BEGIN|COMMIT|ROLLBACK|END)\b\s*(?:TRANSACTION|WORK)?\s*;",
    re.IGNORECASE | re.MULTILINE,
)

MIGRATION_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclasses.dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str
    checksum: str  # hex SHA-256


def discover() -> list[Migration]:
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"ERROR: migrations dir not found: {MIGRATIONS_DIR}")
    out: list[Migration] = []
    for p in sorted(MIGRATIONS_DIR.iterdir()):
        if not p.is_file() or not p.name.endswith(".sql"):
            continue
        m = MIGRATION_FILENAME.match(p.name)
        if not m:
            print(f"  skipping non-migration file: {p.name}", file=sys.stderr)
            continue
        version = p.stem
        sql = p.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        out.append(Migration(version=version, path=p, sql=sql, checksum=checksum))
    return out


def assert_no_top_level_txn(migration: Migration) -> None:
    # Strip dollar-quoted string literals so DO $$ ... END $$ blocks (with
    # BEGIN/END inside plpgsql) aren't false positives. Two passes: untagged
    # `$$...$$` and tagged `$body$...$body$`. Separate regexes avoid the
    # Python `\1` backreference quirk where alternation with an empty branch
    # produces zero-length matches.
    stripped = re.sub(r"\$\$.*?\$\$", "", migration.sql, flags=re.DOTALL)
    stripped = re.sub(r"\$([A-Za-z_]\w+)\$.*?\$\1\$", "", stripped, flags=re.DOTALL)
    # Also strip line comments and block comments.
    stripped = re.sub(r"--[^\n]*", "", stripped)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    if _FORBIDDEN_TXN.search(stripped):
        raise SystemExit(
            f"ERROR: migration {migration.path.name} contains top-level "
            f"BEGIN/COMMIT/ROLLBACK. Migrations must run inside the runner's "
            f"transaction; remove transaction-control statements from the file."
        )


# ---------------------------------------------------------------------------
# psql via docker exec
# ---------------------------------------------------------------------------

def _exec_psql(stdin_sql: str, *, expect_zero: bool = True) -> tuple[str, str, int]:
    container = os.environ.get("POSTGRES_CONTAINER", "postgres")
    role = os.environ.get("ACTION_TRACKER_DB_ROLE", "opsmemory_owner")
    db = os.environ.get("ACTION_TRACKER_DB_NAME", "action_tracker")
    cmd = ["docker", "exec", "-i", container, "psql",
           "-U", role, "-d", db, "-v", "ON_ERROR_STOP=1", "-X", "-q",
           "-At", "-F", "\x1f"]
    proc = subprocess.run(cmd, input=stdin_sql, text=True, capture_output=True)
    if expect_zero and proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"ERROR: psql exited {proc.returncode}")
    return proc.stdout, proc.stderr, proc.returncode


def fetch_applied() -> dict[str, dict]:
    """Returns {version: {checksum, dirty, applied_at}} from schema_migrations."""
    sql = """
SELECT version, COALESCE(checksum, ''), dirty, applied_at::text
FROM schema_migrations
ORDER BY version
""".strip()
    out, _, _ = _exec_psql(sql)
    rows: dict[str, dict] = {}
    for line in out.strip().splitlines():
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        version, checksum, dirty, applied_at = parts[0], parts[1], parts[2], parts[3]
        rows[version] = {
            "checksum": checksum or None,
            "dirty": (dirty.lower() == "t"),
            "applied_at": applied_at,
        }
    return rows


def backfill_checksum(version: str, checksum: str) -> None:
    sql = f"""
UPDATE schema_migrations
   SET checksum = '{checksum}'
 WHERE version = '{version}' AND checksum IS NULL;
""".strip()
    _exec_psql(sql)


def apply_one(m: Migration) -> int:
    """Apply one migration inside a single transaction with advisory lock.
    Returns execution time in ms."""
    started = time.perf_counter()
    # Build the wrapped SQL: outer txn, advisory lock, dirty=true insert,
    # migration body, dirty=false update with execution_ms placeholder
    # (replaced after inner statement completes via WITH).
    wrapped = f"""
BEGIN;
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_ID});

INSERT INTO schema_migrations (version, description, checksum, dirty, applied_at, execution_ms)
VALUES ('{m.version}', '{m.path.name}', '{m.checksum}', true, now(), NULL);

-- BEGIN MIGRATION BODY: {m.path.name}
{m.sql}
-- END MIGRATION BODY

UPDATE schema_migrations
   SET dirty = false,
       checksum = '{m.checksum}',
       applied_at = now()
 WHERE version = '{m.version}';
COMMIT;
""".strip()
    _exec_psql(wrapped)
    return int((time.perf_counter() - started) * 1000)


def update_execution_ms(version: str, ms: int) -> None:
    sql = f"UPDATE schema_migrations SET execution_ms = {ms} WHERE version = '{version}';"
    _exec_psql(sql)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_status(migrations: Iterable[Migration], applied: dict[str, dict]) -> int:
    print("version             status        checksum_match  dirty  applied_at")
    print("-" * 95)
    for m in migrations:
        row = applied.get(m.version)
        if row is None:
            status = "PENDING"
            match = "n/a"
            dirty = "n/a"
            ts = "—"
        else:
            if row["checksum"] is None:
                match = "needs-backfill"
            elif row["checksum"] == m.checksum:
                match = "ok"
            else:
                match = "DRIFT"
            status = "DIRTY" if row["dirty"] else "applied"
            dirty = "yes" if row["dirty"] else "no"
            ts = row["applied_at"] or "—"
        print(f"  {m.version:<19} {status:<13} {match:<15} {dirty:<6} {ts}")
    # extras in DB not on disk
    extras = set(applied.keys()) - {m.version for m in migrations}
    for v in sorted(extras):
        row = applied[v]
        print(f"  {v:<19} {'NO_FILE':<13} {'n/a':<15} {('yes' if row['dirty'] else 'no'):<6} {row['applied_at']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true",
                        help="Show applied vs pending migrations")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show plan without applying")
    parser.add_argument("--force-backfill", action="store_true",
                        help="Backfill NULL checksums for already-applied migrations,"
                             " then exit (does not apply pending)")
    parser.add_argument("--allow-dirty-apply", action="store_true",
                        help="DANGER: ignore dirty rows and re-attempt. Don't use"
                             " unless you've manually repaired the DB.")
    args = parser.parse_args(argv)

    migrations = discover()
    if not migrations:
        print("No migrations found.")
        return 0

    # Pre-flight: refuse migrations with top-level BEGIN/COMMIT.
    for m in migrations:
        try:
            assert_no_top_level_txn(m)
        except SystemExit as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 6

    try:
        applied = fetch_applied()
    except SystemExit:
        return 5

    if args.status:
        return cmd_status(migrations, applied)

    # Backfill phase: any applied row with checksum=NULL gets the current
    # file's checksum written. Required because chunk1 deploys predate the
    # migration runner; their schema_migrations.checksum is NULL.
    backfilled: list[str] = []
    for m in migrations:
        row = applied.get(m.version)
        if row and row["checksum"] is None and not row["dirty"]:
            if args.dry_run:
                print(f"  would backfill checksum for {m.version}")
            else:
                backfill_checksum(m.version, m.checksum)
            backfilled.append(m.version)

    if args.force_backfill:
        print(f"Backfilled {len(backfilled)} checksum(s); exiting without applying pending.")
        return 0

    # Drift / dirty preflight.
    drift: list[str] = []
    dirty: list[str] = []
    pending: list[Migration] = []
    for m in migrations:
        row = applied.get(m.version)
        if row is None:
            pending.append(m)
            continue
        if row["dirty"] and not args.allow_dirty_apply:
            dirty.append(m.version)
            continue
        if row["checksum"] and row["checksum"] != m.checksum and m.version not in backfilled:
            drift.append(m.version)
            continue
        # Already applied + checksum-matches: skip.

    if dirty:
        print(f"FAIL: dirty rows in schema_migrations (manual repair required):", file=sys.stderr)
        for v in dirty:
            print(f"  - {v}", file=sys.stderr)
        return 2

    if drift:
        print(f"FAIL: checksum drift — committed migration files differ from applied state:",
              file=sys.stderr)
        for v in drift:
            print(f"  - {v}", file=sys.stderr)
        print("If you intentionally rewrote a migration, you must reset the relevant",
              file=sys.stderr)
        print("schema_migrations row by hand. Migrations are immutable once applied.",
              file=sys.stderr)
        return 1

    if not pending:
        print(f"Up-to-date. {len(applied)} migration(s) applied"
              + (f", {len(backfilled)} checksum(s) backfilled." if backfilled else "."))
        return 0

    if args.dry_run:
        print(f"Would apply {len(pending)} migration(s):")
        for m in pending:
            print(f"  - {m.version}  ({m.path.name})  checksum={m.checksum[:12]}…")
        if backfilled:
            print(f"Would backfill {len(backfilled)} checksum(s).")
        return 0

    for m in pending:
        print(f"Applying {m.version} ({m.path.name})")
        try:
            ms = apply_one(m)
        except SystemExit:
            return 3
        try:
            update_execution_ms(m.version, ms)
        except SystemExit:
            print(f"WARNING: applied {m.version} but failed to record execution_ms",
                  file=sys.stderr)
        print(f"  {m.version} applied in {ms} ms")

    summary = f"Applied {len(pending)} migration(s)"
    if backfilled:
        summary += f", backfilled {len(backfilled)} checksum(s)"
    print(summary + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())

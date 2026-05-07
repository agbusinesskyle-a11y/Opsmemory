#!/usr/bin/env python3
"""Insert a meeting_recap ingest event directly into action_tracker.

Bypasses the /v1/ingest API (and its CF Access JWT requirement) so we
can validate the LLM extraction → review_items pipeline on Spark without
needing service-token auth working. The worker (run_pipeline.py) picks
up rows from ingest_events.status='received' regardless of how they
landed there.

Usage:
    set -a; source /opt/opsmemory/.env; set +a
    python3 scripts/insert_test_recap.py /tmp/recap.txt phase-b-test-001

Args:
    1. path to recap text file
    2. (optional) source_external_id for idempotency. Default: phase-b-test-<unix-ts>

Env:
    POSTGRES_CONTAINER       default: postgres
    ACTION_TRACKER_DB_ROLE   default: opsmemory_owner
    ACTION_TRACKER_DB_NAME   default: action_tracker
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time


def canonicalize(content: str) -> str:
    """Mirror api/app/v1_ingest.py canonicalize(): normalize line endings,
    collapse runs of horizontal whitespace per line, strip outer newlines."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    ws = re.compile(r"[ \t]+")
    text = "\n".join(ws.sub(" ", line.rstrip()) for line in text.split("\n"))
    return text.strip("\n")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: insert_test_recap.py <recap_file> [source_external_id]", file=sys.stderr)
        return 2

    path = sys.argv[1]
    ext_id = sys.argv[2] if len(sys.argv) > 2 else f"phase-b-test-{int(time.time())}"

    raw = open(path, encoding="utf-8").read()
    canonical = canonicalize(raw)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    meta = json.dumps({
        "business_hint": "redhot",
        "test": True,
        "inserted_by": "insert_test_recap.py",
    })

    container = os.environ.get("POSTGRES_CONTAINER", "postgres")
    role = os.environ.get("ACTION_TRACKER_DB_ROLE", "opsmemory_owner")
    db = os.environ.get("ACTION_TRACKER_DB_NAME", "action_tracker")

    sql = (
        "INSERT INTO ingest_events "
        "(source, source_external_id, raw_content, normalized_hash, source_metadata, status, actor_type) "
        "VALUES ('meeting_recap', :'ext', :'raw', :'h', :'meta'::jsonb, 'received', 'system') "
        "RETURNING id, status, received_at;"
    )

    cmd = [
        "docker", "exec", "-i", container, "psql",
        "-U", role, "-d", db,
        "-v", "ON_ERROR_STOP=1",
        "-v", f"ext={ext_id}",
        "-v", f"raw={canonical}",
        "-v", f"h={h}",
        "-v", f"meta={meta}",
    ]

    proc = subprocess.run(cmd, input=sql, text=True, capture_output=True)

    print("=" * 60)
    print(f"source_external_id : {ext_id}")
    print(f"normalized_hash    : {h}")
    print(f"raw_content_chars  : {len(canonical)}")
    print("=" * 60)
    print("psql stdout:")
    print(proc.stdout)
    if proc.stderr:
        print("psql stderr:")
        print(proc.stderr)
    print(f"return code: {proc.returncode}")

    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())

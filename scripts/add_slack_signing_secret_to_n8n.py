#!/usr/bin/env python3
"""One-shot edit: forward SLACK_SIGNING_SECRET into the n8n container.

Adds the env-var forwarding line to /opt/open-brain/infrastructure/
spark1-postgres-redis.compose.yml so n8n's Code nodes can read
$env.SLACK_SIGNING_SECRET. Idempotent — bails out cleanly if the
line is already present.

The actual secret value goes into /opt/open-brain/infrastructure/.env
separately (via a one-line append) so we don't bake it into the
compose file (matches the existing pattern for CF_CANARY_NONCE etc).

Usage:
    sudo python3 scripts/add_slack_signing_secret_to_n8n.py
"""
from __future__ import annotations

import pathlib
import sys


COMPOSE_PATH = pathlib.Path(
    "/opt/open-brain/infrastructure/spark1-postgres-redis.compose.yml"
)

# We insert AFTER this line in the n8n service's environment block.
# It's the last entry in the existing canary-env section as of
# 2026-05-08; if the compose file is updated and the marker moves,
# this script bails out so we notice rather than silently misplacing
# the new entry.
INSERT_AFTER = (
    "      TENANT_SHEETS_CANARY_RANGE_READ: "
    "${TENANT_SHEETS_CANARY_RANGE_READ:-}"
)

ADDITION = """\
      # OpsMemory Slack ingest (Phase C, 2026-05-08). The HMAC-verify
      # Code node in the opsmemory-slack-ingest workflow reads
      # $env.SLACK_SIGNING_SECRET to compute the v0 signature it
      # compares against the X-Slack-Signature header. The value lives
      # in infrastructure/.env (NOT this file) so the secret stays out
      # of source. See OpsMemory's docs/15-slack-edge-adapter-contract.md
      # and docs/16-slack-n8n-build-runbook.md (Step 1a).
      SLACK_SIGNING_SECRET: ${SLACK_SIGNING_SECRET:-}
"""

ALREADY_PRESENT_MARKER = "SLACK_SIGNING_SECRET:"


def main() -> int:
    if not COMPOSE_PATH.exists():
        sys.stderr.write(f"ERROR: {COMPOSE_PATH} not found\n")
        return 1

    text = COMPOSE_PATH.read_text()

    if ALREADY_PRESENT_MARKER in text:
        print(f"already present in {COMPOSE_PATH.name} — no edit needed")
        return 0

    if INSERT_AFTER not in text:
        sys.stderr.write(
            f"ERROR: insertion marker not found:\n  {INSERT_AFTER!r}\n"
            "Compose file may have been edited since this script was "
            "written. Inspect the env block manually and either pick a "
            "new INSERT_AFTER or insert by hand.\n"
        )
        return 2

    new_text = text.replace(
        INSERT_AFTER,
        INSERT_AFTER + "\n" + ADDITION,
        1,
    )

    backup = COMPOSE_PATH.with_suffix(
        COMPOSE_PATH.suffix + ".bak.before-slack-secret"
    )
    if not backup.exists():
        backup.write_text(text)
        print(f"backup written: {backup}")

    COMPOSE_PATH.write_text(new_text)
    print(f"inserted SLACK_SIGNING_SECRET forwarding into {COMPOSE_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Diagnose the MCP service-key hash mismatch.

Recomputes the HMAC of the raw key in the same way auth.py would
at runtime, against every pepper available in the container env.
Prints which pepper produces the hash currently stored in
service_accounts for `opsmemory-mcp`.

Run inside the API container:

    docker compose run --rm -v /opt/opsmemory/scripts:/app/scripts \
      opsmemory-api python3 /app/scripts/debug_mcp_key.py
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys


# Hardcode the raw key + expected hash from the bootstrap output. If
# we generate fresh keys, these need to be replaced — but for this
# one-off diagnostic, hardcoding is fine.
RAW_KEY = "opsmem_live_AYSR5ZMEZ8zSL81f_PHLhXpOfDyoIWllfeD4n1h28A8npjh2HoJVuMQ3tkFS"
EXPECTED_HASH = "6761961a6ad23afb48324222c3ae365da88f5e446ed861f9ec18c92ee8885278"


def hmac_hex(pepper: bytes) -> str:
    return hmac.new(pepper, RAW_KEY.encode("utf-8"), hashlib.sha256).hexdigest()


def main() -> int:
    print(f"RAW_KEY      = {RAW_KEY}")
    print(f"EXPECTED     = {EXPECTED_HASH}")
    print()

    # Probe every env var that smells like a pepper.
    candidates: list[tuple[str, bytes]] = []
    for k, v in sorted(os.environ.items()):
        if k.startswith("SERVICE_KEY_PEPPER"):
            print(f"env: {k} = <{len(v)} chars, sha256={hashlib.sha256(v.encode()).hexdigest()[:16]}>")
            if k != "SERVICE_KEY_PEPPER_ACTIVE_VERSION":
                candidates.append((k, v.encode("utf-8")))
    print()

    if not candidates:
        print("ERROR: no SERVICE_KEY_PEPPER* env vars found in container")
        return 1

    print("Hash with each candidate pepper:")
    matched = []
    for name, pepper in candidates:
        h = hmac_hex(pepper)
        marker = " <-- MATCH" if h == EXPECTED_HASH else ""
        print(f"  {name}: {h}{marker}")
        if h == EXPECTED_HASH:
            matched.append(name)
    print()

    if not matched:
        print(
            "VERDICT: no pepper in the container env produces the stored "
            "hash. Either the bootstrap script ran with a different env "
            "than the API container, or the row was pasted from a "
            "different source. Re-running bootstrap should fix it."
        )
        return 2
    print(f"VERDICT: hash matches when computed with {matched[0]!r}.")
    print(
        "         If auth.py disagrees with this pepper at runtime, the "
        "issue is in auth.py's pepper-version resolution logic."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

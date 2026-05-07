#!/usr/bin/env python3
"""Generate a VAPID keypair for OpsMemory's Web Push sender.

Outputs three lines suitable for pasting into /opt/opsmemory/.env:

    VAPID_PUBLIC_KEY=<88-char base64url>
    VAPID_PRIVATE_KEY=<43-char base64url>
    VAPID_SUBJECT=mailto:ops@kyleconway.ai

Override the subject via:
    python3 scripts/gen_vapid_keys.py mailto:you@example.com

Per RFC 8292: public key is the SEC1 uncompressed P-256 point
(0x04 || x || y, 65 bytes total), base64url-encoded without
padding. Private key is the raw 32-byte scalar, same encoding.

Run inside the API container so it uses the cryptography wheel
the production sender uses:

    docker compose exec opsmemory-api python3 /app/scripts/gen_vapid_keys.py
"""

from __future__ import annotations

import base64
import sys

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def main() -> int:
    subject = sys.argv[1] if len(sys.argv) > 1 else "mailto:ops@kyleconway.ai"
    if not (subject.startswith("mailto:") or subject.startswith("https://")):
        print(
            f"ERROR: VAPID subject must start with 'mailto:' or 'https://'; got {subject!r}",
            file=sys.stderr,
        )
        return 1

    priv_key = ec.generate_private_key(ec.SECP256R1())
    pub_bytes = priv_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    priv_bytes = priv_key.private_numbers().private_value.to_bytes(32, "big")

    print(f"VAPID_PUBLIC_KEY={_b64url(pub_bytes)}")
    print(f"VAPID_PRIVATE_KEY={_b64url(priv_bytes)}")
    print(f"VAPID_SUBJECT={subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

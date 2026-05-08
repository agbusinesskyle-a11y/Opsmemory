#!/usr/bin/env python3
"""Slack HMAC fixture — generates signed Slack-shaped requests
plus the test vectors from docs/15-slack-edge-adapter-contract.md.

Two purposes:

1. **Reference implementation.** `verify_slack_signature()` below is
   the correct algorithm. The n8n Code node JavaScript that gets built
   in Phase C must match its semantics exactly. If a known-good signed
   request from this fixture fails to verify in n8n, the n8n
   implementation is wrong.

2. **End-to-end test vectors.** When the operator runs `--mode print-vectors`
   the script emits 8 curl commands covering every failure mode from the
   contract doc's section 9. Run each one against the deployed n8n
   webhook URL after wiring; compare actual response to expected.

Usage:
    # Default secret (test-only) and current timestamp:
    python3 scripts/slack_hmac_fixture.py self-test

    # Print all 8 test-vector curl commands against a target URL:
    python3 scripts/slack_hmac_fixture.py print-vectors \\
        --webhook-url https://auto.kyleconway.ai/webhook/<n8n-uuid> \\
        --signing-secret <slack-app-signing-secret>

    # Sign one custom request body against a target URL (smoke test):
    echo '{"type":"event_callback",...}' | \\
      python3 scripts/slack_hmac_fixture.py sign \\
        --webhook-url https://... \\
        --signing-secret <secret>

NOTE: This fixture does NOT call the network. It produces curl
command strings that the operator runs. Keeps the script
side-effect-free and the operator in the loop on every request.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shlex
import sys
import time
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Reference implementation — the n8n Code node MUST match this
# ---------------------------------------------------------------------------

def compute_slack_signature(signing_secret: str, timestamp: str, raw_body: bytes) -> str:
    """Return the v0= signature Slack would have sent."""
    base = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    digest = hmac.new(signing_secret.encode("ascii"), base, hashlib.sha256).hexdigest()
    return "v0=" + digest


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp_header: str | None,
    signature_header: str | None,
    raw_body: bytes,
    now_unix: int | None = None,
    skew_seconds: int = 300,
) -> tuple[bool, str]:
    """Reference verifier. Returns (ok, reason). On success reason='ok'.

    n8n's Code node JavaScript should produce the same accept/reject
    decision for every input pair the fixture generates.
    """
    if not signature_header:
        return False, "missing_signature_header"
    if not timestamp_header:
        return False, "missing_timestamp_header"
    try:
        ts_int = int(timestamp_header)
    except (TypeError, ValueError):
        return False, "timestamp_not_integer"
    now = now_unix if now_unix is not None else int(time.time())
    if abs(now - ts_int) > skew_seconds:
        return False, "timestamp_skew_exceeded"
    if not signature_header.startswith("v0="):
        return False, "signature_prefix_missing"
    expected = compute_slack_signature(signing_secret, timestamp_header, raw_body)
    # Length-check before timingSafeEqual-style compare to avoid the
    # node 'crypto.timingSafeEqual throws on unequal lengths' gotcha
    # the n8n implementation must also handle.
    if len(signature_header) != len(expected):
        return False, "signature_length_mismatch"
    if not hmac.compare_digest(signature_header, expected):
        return False, "signature_mismatch"
    return True, "ok"


# ---------------------------------------------------------------------------
# Sample bodies
# ---------------------------------------------------------------------------

def sample_app_mention_body(team_id: str = "T01TEST123",
                             channel_id: str = "C01TEST456",
                             user_id: str = "U01TEST789",
                             ts: str = "1746640000.000200") -> bytes:
    """A realistic Slack `app_mention` event_callback payload.

    Keys are sorted-by-Slack-server convention; mutating order would
    not match the wire format Slack actually sends. We treat the bytes
    below as the canonical 'what Slack sent' for fixture purposes.
    """
    body = {
        "token": "<deprecated-token>",
        "team_id": team_id,
        "api_app_id": "A01OPSMEM",
        "event": {
            "type": "app_mention",
            "user": user_id,
            "text": "<@U01OPSMEM> Joanna please order more sparklers by Friday May 9",
            "ts": ts,
            "channel": channel_id,
            "event_ts": ts,
        },
        "type": "event_callback",
        "event_id": "Ev01TESTABC",
        "event_time": int(float(ts)),
        "authorizations": [{"team_id": team_id, "user_id": "U01OPSMEM",
                             "is_bot": True, "is_enterprise_install": False}],
        "event_context": "1-app_mention-T01TEST123-C01TEST456",
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def sample_url_verification_body(challenge: str | None = None) -> bytes:
    if challenge is None:
        challenge = secrets.token_urlsafe(36)
    body = {
        "token": "<deprecated-token>",
        "challenge": challenge,
        "type": "url_verification",
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Test vectors
# ---------------------------------------------------------------------------

@dataclass
class TestVector:
    name: str
    description: str
    expected_response: str
    timestamp: str
    body: bytes
    signature_header: str
    omit_signature: bool = False
    omit_timestamp: bool = False


def build_test_vectors(signing_secret: str, now_unix: int | None = None) -> list[TestVector]:
    """Return all 8 test vectors covering the contract's failure modes."""
    now = now_unix if now_unix is not None else int(time.time())

    valid_body = sample_app_mention_body()
    valid_ts = str(now)
    valid_sig = compute_slack_signature(signing_secret, valid_ts, valid_body)

    stale_ts = str(now - 360)  # 6 minutes old
    stale_sig = compute_slack_signature(signing_secret, stale_ts, valid_body)

    wrong_secret_sig = compute_slack_signature("not-the-real-secret", valid_ts, valid_body)

    # Body-tampered: sign one body, send another.
    tampered_body = valid_body.replace(b"sparklers", b"firecrackers")

    no_v0_sig = valid_sig[3:]  # drop 'v0=' prefix

    challenge_body = sample_url_verification_body(challenge="3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHzx")
    challenge_sig = compute_slack_signature(signing_secret, valid_ts, challenge_body)

    return [
        TestVector(
            name="01_valid_app_mention",
            description="Valid signature, current timestamp, app_mention body",
            expected_response="2xx (n8n forwards to OpsMemory; success path)",
            timestamp=valid_ts,
            body=valid_body,
            signature_header=valid_sig,
        ),
        TestVector(
            name="02_stale_timestamp",
            description="Valid signature, but timestamp 6 minutes old (replay)",
            expected_response="401 (replay protection: skew > 300s)",
            timestamp=stale_ts,
            body=valid_body,
            signature_header=stale_sig,
        ),
        TestVector(
            name="03_wrong_secret",
            description="Signature computed with wrong signing secret",
            expected_response="401 (signature_mismatch)",
            timestamp=valid_ts,
            body=valid_body,
            signature_header=wrong_secret_sig,
        ),
        TestVector(
            name="04_body_tampered",
            description="Sign one body, send a different one (man-in-the-middle)",
            expected_response="401 (signature_mismatch — body bytes don't match base)",
            timestamp=valid_ts,
            body=tampered_body,
            signature_header=valid_sig,  # signature for the ORIGINAL body
        ),
        TestVector(
            name="05_missing_signature_header",
            description="X-Slack-Signature absent",
            expected_response="401 (missing_signature_header)",
            timestamp=valid_ts,
            body=valid_body,
            signature_header="",
            omit_signature=True,
        ),
        TestVector(
            name="06_missing_timestamp_header",
            description="X-Slack-Request-Timestamp absent",
            expected_response="401 (missing_timestamp_header)",
            timestamp="",
            body=valid_body,
            signature_header=valid_sig,
            omit_timestamp=True,
        ),
        TestVector(
            name="07_malformed_signature_no_v0",
            description="Signature header without the 'v0=' prefix",
            expected_response="401 (signature_prefix_missing)",
            timestamp=valid_ts,
            body=valid_body,
            signature_header=no_v0_sig,
        ),
        TestVector(
            name="08_url_verification_challenge",
            description="Slack one-time URL verification handshake at app install",
            expected_response="200 with body {\"challenge\":\"3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHzx\"}",
            timestamp=valid_ts,
            body=challenge_body,
            signature_header=challenge_sig,
        ),
    ]


# ---------------------------------------------------------------------------
# CLI: self-test, print-vectors, sign
# ---------------------------------------------------------------------------

def cmd_self_test(_args: argparse.Namespace) -> int:
    """Run all test vectors through the reference verifier and confirm
    each one produces the expected accept/reject decision.

    This proves the algorithm in this file is internally consistent
    BEFORE we point it at a network endpoint.
    """
    secret = "self-test-secret-do-not-use-in-prod"
    vectors = build_test_vectors(secret)
    failures = 0
    print(f"running {len(vectors)} self-test vectors against the reference verifier")
    print()
    for v in vectors:
        ts = None if v.omit_timestamp else v.timestamp
        sig = None if v.omit_signature else v.signature_header
        ok, reason = verify_slack_signature(
            signing_secret=secret,
            timestamp_header=ts,
            signature_header=sig,
            raw_body=v.body,
        )
        # Vectors 1 and 8 should pass; all others should fail.
        should_pass = v.name in ("01_valid_app_mention", "08_url_verification_challenge")
        passed = (should_pass and ok) or (not should_pass and not ok)
        marker = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"  [{marker}] {v.name}  reason={reason}  expected={'ok' if should_pass else 'reject'}")
    print()
    print(f"{len(vectors) - failures} of {len(vectors)} vectors behaved as expected")
    return 0 if failures == 0 else 1


def _curl_command(webhook_url: str, vector: TestVector) -> str:
    """Render one curl command line for a test vector."""
    parts = ["curl", "-i", "-sS", "-X", "POST", webhook_url,
             "-H", "Content-Type: application/json"]
    if not vector.omit_signature:
        parts += ["-H", f"X-Slack-Signature: {vector.signature_header}"]
    if not vector.omit_timestamp:
        parts += ["-H", f"X-Slack-Request-Timestamp: {vector.timestamp}"]
    parts += ["--data-binary", "@-"]
    quoted = " ".join(shlex.quote(p) for p in parts)
    # Pipe the raw body via stdin so the bytes match what we signed.
    body_quoted = shlex.quote(vector.body.decode("utf-8"))
    return f"printf %s {body_quoted} | {quoted}"


def cmd_print_vectors(args: argparse.Namespace) -> int:
    secret = args.signing_secret
    if not secret:
        sys.stderr.write("ERROR: --signing-secret required\n")
        return 2
    if not args.webhook_url:
        sys.stderr.write("ERROR: --webhook-url required\n")
        return 2

    vectors = build_test_vectors(secret)
    print(f"# Slack edge adapter test vectors")
    print(f"# Generated against webhook URL: {args.webhook_url}")
    print(f"# Run each command and compare to 'expected' to validate n8n implementation.")
    print()
    for v in vectors:
        print(f"## {v.name}")
        print(f"# {v.description}")
        print(f"# expected: {v.expected_response}")
        print(_curl_command(args.webhook_url, v))
        print()
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    secret = args.signing_secret
    if not secret:
        sys.stderr.write("ERROR: --signing-secret required\n")
        return 2
    if not args.webhook_url:
        sys.stderr.write("ERROR: --webhook-url required\n")
        return 2

    raw_body = sys.stdin.buffer.read()
    if not raw_body:
        sys.stderr.write("ERROR: no body on stdin\n")
        return 2

    timestamp = str(int(time.time()))
    sig = compute_slack_signature(secret, timestamp, raw_body)

    vector = TestVector(
        name="custom",
        description="ad-hoc signed request from stdin",
        expected_response="2xx if HMAC + URL routing valid",
        timestamp=timestamp,
        body=raw_body,
        signature_header=sig,
    )
    print(_curl_command(args.webhook_url, vector))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_self = sub.add_parser("self-test",
        help="Run all test vectors through the reference verifier; no network.")
    p_self.set_defaults(func=cmd_self_test)

    p_print = sub.add_parser("print-vectors",
        help="Emit 8 curl commands covering every failure mode in the contract.")
    p_print.add_argument("--webhook-url", required=True,
        help="The deployed n8n webhook URL to test against.")
    p_print.add_argument("--signing-secret", required=True,
        help="Slack app signing secret (the value n8n credentials store).")
    p_print.set_defaults(func=cmd_print_vectors)

    p_sign = sub.add_parser("sign",
        help="Sign one body from stdin against a target URL; useful for ad-hoc smoke tests.")
    p_sign.add_argument("--webhook-url", required=True)
    p_sign.add_argument("--signing-secret", required=True)
    p_sign.set_defaults(func=cmd_sign)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

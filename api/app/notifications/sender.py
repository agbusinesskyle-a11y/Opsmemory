"""OpsMemory notifications: Web Push sender.

Per Codex chunk-10-step5 plan-review (sender slice). This module
owns the encrypt + HTTP call + status-code mapping for one
notification_deliveries row. The runner / test-push endpoint
calls send_one(...) per claimed row.

Layering:
  api/app/notifications/schedule.py    schedule contract
  api/app/notifications/digest.py      pure payload builder
  api/app/notifications/scheduler.py   walker + claim_delivery
  api/app/notifications/sender.py      this module — encrypt +
                                        HTTP + audit transition
                                        per claimed row

Status code mapping (per RFC 8030 + browser push services):
  2xx                   sent (notification_deliveries.status='sent',
                              sent_at=now())
  404 / 410             expired — the push service rejected because
                              the subscription is gone. Mark
                              web_push_subscriptions.status='expired'
                              so the PWA's Reconnect path picks up,
                              and notification_deliveries.status='failed'
                              with error.code='unsubscribed'.
  4xx (not 404/410)     hard failure — payload/key/auth problem.
                              status='failed', error.code='bad_request'.
                              Operator visibility; no retry.
  5xx / network         transient — status='failed' but error.code=
                              'transient' so a future retry path can
                              filter on it. No automatic retry yet.

This module is intentionally thin. It does NOT walk the
notification_deliveries table; the caller passes a single
claimed row dict. Public surface:

  load_vapid_config(env)   read VAPID env vars; structured dict
                            ready for pywebpush.
  send_one(conn, *, ...)   ship one delivery row end-to-end and
                            update audit state.

The pywebpush import is lazy: this module loads cleanly on
systems without the wheel (Windows dev, smoke tests). The
runner / endpoint preflight ensures the wheel exists before
claiming any rows, so missing pywebpush in --send mode fails
fast rather than per-row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


# Hard timeout on the upstream HTTP call. Push services usually
# respond in <1s; 15s is generous and still finite. A stuck
# provider can't deadlock the runner / API.
DEFAULT_SEND_TIMEOUT_SECONDS = 15


def preflight_sender(env: dict[str, str] | None = None) -> "VapidConfig":
    """Validate that everything send_one needs is available BEFORE
    the caller starts claiming rows. Codex chunk-10-step5c2-close:
    a missing pywebpush wheel previously surfaced as per-row
    code='config' failures; now it raises during preflight so the
    runner exits 1 cleanly.

    Returns the loaded VapidConfig so the caller doesn't have to
    re-load it.
    """
    vapid = load_vapid_config(env)
    try:
        from pywebpush import webpush  # noqa: F401  # presence check
    except ImportError as exc:
        raise RuntimeError(
            "pywebpush is not installed; install api/requirements.txt "
            "(pywebpush==2.0.0) before running --send."
        ) from exc
    return vapid


log = logging.getLogger("opsmemory.notifications.sender")


@dataclass
class VapidConfig:
    public_key: str
    private_key: str
    subject: str


def load_vapid_config(env: dict[str, str] | None = None) -> VapidConfig:
    """Read VAPID env into a typed config. Raises RuntimeError when
    not configured — sender requires it (unlike the API which
    cleanly degrades to a 503 on /vapid_public when unset).
    """
    e = env if env is not None else os.environ
    pub = (e.get("VAPID_PUBLIC_KEY") or "").strip()
    priv = (e.get("VAPID_PRIVATE_KEY") or "").strip()
    sub = (e.get("VAPID_SUBJECT") or "").strip()
    missing = [n for n, v in (
        ("VAPID_PUBLIC_KEY", pub),
        ("VAPID_PRIVATE_KEY", priv),
        ("VAPID_SUBJECT", sub),
    ) if not v]
    if missing:
        raise RuntimeError(
            f"VAPID config missing: {missing}. The sender cannot run "
            "without all three vars. The API can still serve /vapid_public "
            "as 503 when unset, but a claim-mode scheduler run will fail."
        )
    return VapidConfig(public_key=pub, private_key=priv, subject=sub)


def provider_from_endpoint(endpoint: str | None) -> str:
    """Best-effort label for notification_deliveries.provider so
    operators can grep failures by push service.
    """
    if not endpoint:
        return "unknown"
    try:
        host = urlparse(endpoint).hostname or ""
    except Exception:
        return "unknown"
    host = host.lower()
    if "fcm.googleapis.com" in host or "android.googleapis.com" in host:
        return "firebase"
    if "mozilla.com" in host or "mozaws.net" in host or "push.services.mozilla.com" in host:
        return "mozilla"
    if "push.apple.com" in host or "icloud.com" in host:
        return "apple"
    if "windows.com" in host:
        return "windows"
    return "other"


@dataclass
class SendResult:
    status: str           # 'sent' | 'failed'
    http_status: int | None
    code: str | None      # 'sent' | 'unsubscribed' | 'bad_request' | 'transient' | 'config'
    detail: str | None
    delivery_id: str | None


def _classify_http(status: int) -> tuple[str, str]:
    if 200 <= status < 300:
        return "sent", "sent"
    if status in (404, 410):
        return "failed", "unsubscribed"
    if 400 <= status < 500:
        return "failed", "bad_request"
    return "failed", "transient"


# Sentinel result kinds returned by _do_send so the audit-write
# step can distinguish a real upstream HTTP status (use
# _classify_http) from a transport-layer failure (force
# 'transient') from a local key/encoding bug (force
# 'bad_request' so an operator notices).
@dataclass
class _RawSendOutcome:
    kind: str            # 'http' | 'transport' | 'local'
    http_status: int | None
    body_text: str


def _do_send_sync(
    *,
    subscription_info: dict,
    payload_bytes: bytes,
    vapid: VapidConfig,
    ttl: int,
    timeout: float,
) -> _RawSendOutcome:
    """Synchronous pywebpush call. send_one wraps this in
    asyncio.to_thread so the asyncio event loop doesn't block on
    the HTTP round trip (Codex chunk-10-step5c2-close (a)).

    Codex chunk-10-step5c2-close also called out the WebPushException
    classification: pure transport failures (DNS, connection refused,
    read timeout) come from `requests` directly and DON'T always go
    through WebPushException. Catch those explicitly and tag them
    'transport' so the audit-write maps to code='transient'.
    A WebPushException with no .response can be a local key/encoding
    bug (e.g. malformed p256dh) — tag 'local' for code='bad_request'.
    """
    from pywebpush import WebPushException, webpush  # type: ignore
    from requests.exceptions import (  # type: ignore
        ConnectionError as RConnectionError,
        Timeout as RTimeout,
        RequestException,
    )

    try:
        resp = webpush(
            subscription_info=subscription_info,
            data=payload_bytes,
            vapid_private_key=vapid.private_key,
            vapid_claims={"sub": vapid.subject},
            ttl=ttl,
            timeout=timeout,
        )
    except WebPushException as exc:
        if exc.response is not None:
            body_text = ""
            try:
                body_text = (exc.response.text or "")[:512]
            except Exception:
                pass
            return _RawSendOutcome(
                kind="http",
                http_status=exc.response.status_code,
                body_text=body_text,
            )
        # No response attached: pywebpush raised before the HTTP
        # call (key/encoding bug locally) OR the requests layer
        # raised a transport error and pywebpush wrapped it. Treat
        # as 'local' (operator-actionable bad_request).
        return _RawSendOutcome(
            kind="local", http_status=None, body_text=str(exc)[:512],
        )
    except (RTimeout, RConnectionError) as exc:
        return _RawSendOutcome(
            kind="transport", http_status=None, body_text=f"{type(exc).__name__}: {str(exc)[:256]}",
        )
    except RequestException as exc:
        return _RawSendOutcome(
            kind="transport", http_status=None, body_text=f"{type(exc).__name__}: {str(exc)[:256]}",
        )

    body_text = ""
    try:
        body_text = (resp.text or "")[:512]
    except Exception:
        pass
    return _RawSendOutcome(
        kind="http", http_status=resp.status_code, body_text=body_text,
    )


async def _do_send(
    *,
    subscription_info: dict,
    payload_bytes: bytes,
    vapid: VapidConfig,
    ttl: int,
    timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
) -> _RawSendOutcome:
    """Async wrapper that hands the sync pywebpush call off to a
    thread pool. Codex chunk-10-step5c2-close (a): blocking the
    event loop on a network round-trip would wedge the API
    process when the test endpoint runs.
    """
    return await asyncio.to_thread(
        _do_send_sync,
        subscription_info=subscription_info,
        payload_bytes=payload_bytes,
        vapid=vapid,
        ttl=ttl,
        timeout=timeout,
    )


async def send_one(
    conn,
    *,
    delivery_id: str,
    user_id: str,
    pref_id: str | None,
    web_push_subscription_id: str,
    payload: dict,
    vapid: VapidConfig,
    ttl: int = 86400,
) -> SendResult:
    """Ship one already-claimed notification_deliveries row.

    Caller must have already inserted the row via
    scheduler.claim_delivery (status='scheduled'). This function:
      1. Marks attempted_at = now() so retries can detect
         in-flight rows.
      2. Loads the subscription. If status != 'active', short-
         circuits to status='failed' code='unsubscribed' (someone
         revoked between claim and send).
      3. Encrypts + POSTs via pywebpush.
      4. Updates the row to status='sent' or status='failed' with
         http_status, error.code, error.detail in the error jsonb.
      5. On 410/404, also UPDATEs web_push_subscriptions.status =
         'expired' so the PWA's Reconnect path picks up.
    """
    now = datetime.now(timezone.utc)

    # Step 1: claim the attempt window. Even if the send fails,
    # this proves we tried so a retry job can decide what to do.
    await conn.execute(
        """
        UPDATE notification_deliveries
           SET attempted_at = $2::timestamptz
         WHERE id = $1::uuid
        """,
        delivery_id, now,
    )

    # Step 2: load the subscription (fresh — status could have
    # changed since claim, AND ownership could have flipped:
    # the POST endpoint does ON CONFLICT (endpoint) DO UPDATE
    # SET user_id = EXCLUDED.user_id, so a re-registration on
    # the same browser/endpoint by a different user would
    # reassign the row. Codex chunk-10-step5c34-close: fetch
    # both status and user_id and reject any mismatch so
    # user A's queued digest can never ship to user B's
    # device.
    sub_row = await conn.fetchrow(
        """
        SELECT id::text          AS id,
               user_id::text     AS user_id,
               endpoint          AS endpoint,
               p256dh_key        AS p256dh_key,
               auth_key          AS auth_key,
               status            AS status
          FROM web_push_subscriptions
         WHERE id = $1::uuid
        """,
        web_push_subscription_id,
    )
    if sub_row is None or sub_row["status"] != "active":
        # Subscription was revoked between claim and send.
        await _mark_failed(
            conn,
            delivery_id=delivery_id,
            http_status=None,
            code="unsubscribed",
            detail=("subscription not found"
                    if sub_row is None
                    else f"subscription status={sub_row['status']}"),
            provider=provider_from_endpoint(sub_row["endpoint"] if sub_row else None),
        )
        return SendResult(
            status="failed",
            http_status=None,
            code="unsubscribed",
            detail="subscription not active at send time",
            delivery_id=delivery_id,
        )
    if sub_row["user_id"] != user_id:
        # Ownership flipped between claim and send. Refuse.
        # The other user's pref will fire its own digest the
        # next cycle; this one becomes a non-leaking audit row.
        log.warning("sender_ownership_mismatch", extra={
            "delivery_id": delivery_id,
            "user_id_claimed": user_id,
            "user_id_now": sub_row["user_id"],
            "subscription_id": web_push_subscription_id,
        })
        await _mark_failed(
            conn,
            delivery_id=delivery_id,
            http_status=None,
            code="ownership_changed",
            detail=(f"subscription reassigned from {user_id} to "
                    f"{sub_row['user_id']} between claim and send"),
            provider=provider_from_endpoint(sub_row["endpoint"]),
        )
        return SendResult(
            status="failed",
            http_status=None,
            code="ownership_changed",
            detail="subscription reassigned to different user",
            delivery_id=delivery_id,
        )

    subscription_info = {
        "endpoint": sub_row["endpoint"],
        "keys": {
            "p256dh": sub_row["p256dh_key"],
            "auth": sub_row["auth_key"],
        },
    }
    provider = provider_from_endpoint(sub_row["endpoint"])

    # Step 3: encrypt + POST. Codex chunk-10-step5c2-close (a):
    # the HTTP call now runs in a thread pool with a finite
    # timeout. _do_send NEVER raises — it returns _RawSendOutcome
    # carrying enough info for us to classify.
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        outcome = await _do_send(
            subscription_info=subscription_info,
            payload_bytes=payload_bytes,
            vapid=vapid,
            ttl=ttl,
        )
    except Exception as exc:
        # Defensive — a programming error inside the helper itself.
        log.exception("sender_unexpected_error", extra={
            "delivery_id": delivery_id,
            "user_id": user_id,
            "pref_id": pref_id,
            "subscription_id": web_push_subscription_id,
        })
        await _mark_failed(
            conn,
            delivery_id=delivery_id,
            http_status=None,
            code="config",
            detail=f"sender exception: {type(exc).__name__}: {str(exc)[:256]}",
            provider=provider,
        )
        return SendResult(
            status="failed", http_status=None, code="config",
            detail=str(exc)[:256], delivery_id=delivery_id,
        )

    # Step 4: classify based on outcome kind.
    if outcome.kind == "transport":
        # Codex chunk-10-step5c2-close: transport failures (DNS,
        # connection refused, read timeout) classify as
        # 'transient' so a future retry walker can filter them.
        await _mark_failed(
            conn, delivery_id=delivery_id,
            http_status=None, code="transient",
            detail=outcome.body_text, provider=provider,
        )
        log.info("sender_failed", extra={
            "delivery_id": delivery_id, "user_id": user_id,
            "pref_id": pref_id,
            "subscription_id": web_push_subscription_id,
            "provider": provider, "http_status": None,
            "code": "transient",
        })
        return SendResult(
            status="failed", http_status=None, code="transient",
            detail=outcome.body_text, delivery_id=delivery_id,
        )
    if outcome.kind == "local":
        # Local key/encoding bug — operator-actionable, not
        # transient; don't retry-store.
        await _mark_failed(
            conn, delivery_id=delivery_id,
            http_status=None, code="bad_request",
            detail=outcome.body_text, provider=provider,
        )
        log.info("sender_failed", extra={
            "delivery_id": delivery_id, "user_id": user_id,
            "pref_id": pref_id,
            "subscription_id": web_push_subscription_id,
            "provider": provider, "http_status": None,
            "code": "bad_request",
        })
        return SendResult(
            status="failed", http_status=None, code="bad_request",
            detail=outcome.body_text, delivery_id=delivery_id,
        )

    # outcome.kind == 'http': real HTTP status from the push service.
    http_status = outcome.http_status or 0
    body_text = outcome.body_text
    status, code = _classify_http(http_status)
    if status == "sent":
        # Codex chunk-10-step5c2-close: bump last_seen_at on
        # successful send so the operator/UI can tell which
        # devices are healthy and reachable. The schema column at
        # 0013_notifications.sql:115 was always meant for this;
        # the sender just wasn't writing it.
        await conn.execute(
            """
            UPDATE notification_deliveries
               SET status   = 'sent',
                   sent_at  = now(),
                   provider = $2::text,
                   error    = '{}'::jsonb
             WHERE id = $1::uuid
            """,
            delivery_id, provider,
        )
        await conn.execute(
            """
            UPDATE web_push_subscriptions
               SET last_seen_at = now()
             WHERE id = $1::uuid
            """,
            web_push_subscription_id,
        )
        log.info("sender_sent", extra={
            "delivery_id": delivery_id,
            "user_id": user_id,
            "pref_id": pref_id,
            "subscription_id": web_push_subscription_id,
            "provider": provider,
            "http_status": http_status,
        })
        return SendResult(
            status="sent", http_status=http_status, code="sent",
            detail=None, delivery_id=delivery_id,
        )

    # HTTP failure: 4xx or 5xx classified by _classify_http.
    await _mark_failed(
        conn,
        delivery_id=delivery_id,
        http_status=http_status,
        code=code,
        detail=body_text,
        provider=provider,
    )
    if code == "unsubscribed":
        # 404 / 410 — also expire the subscription (gated on
        # current status='active' so we don't clobber 'revoked').
        await conn.execute(
            """
            UPDATE web_push_subscriptions
               SET status = 'expired'
             WHERE id = $1::uuid
               AND status = 'active'
            """,
            web_push_subscription_id,
        )
        log.info("sender_subscription_expired", extra={
            "delivery_id": delivery_id,
            "subscription_id": web_push_subscription_id,
            "http_status": http_status,
        })
    elif code == "bad_request":
        # Provider rejected the payload but the subscription
        # itself contacted us — bump last_seen_at so a follow-up
        # operator inspection can see the device was reachable.
        # Per Codex chunk-10-step5c2-close: 'last_seen_at on
        # successful send AND probably on non-404/410 provider
        # contact.'
        await conn.execute(
            """
            UPDATE web_push_subscriptions
               SET last_seen_at = now()
             WHERE id = $1::uuid
            """,
            web_push_subscription_id,
        )

    log.info("sender_failed", extra={
        "delivery_id": delivery_id,
        "user_id": user_id,
        "pref_id": pref_id,
        "subscription_id": web_push_subscription_id,
        "provider": provider,
        "http_status": http_status,
        "code": code,
    })
    return SendResult(
        status="failed", http_status=http_status, code=code,
        detail=body_text, delivery_id=delivery_id,
    )


async def _mark_failed(
    conn,
    *,
    delivery_id: str,
    http_status: int | None,
    code: str,
    detail: str | None,
    provider: str,
) -> None:
    err = {"code": code}
    if http_status is not None:
        err["http_status"] = http_status
    if detail:
        err["detail"] = detail[:1024]
    await conn.execute(
        """
        UPDATE notification_deliveries
           SET status    = 'failed',
               failed_at = now(),
               provider  = $2::text,
               error     = $3::jsonb
         WHERE id = $1::uuid
        """,
        delivery_id, provider, err,
    )

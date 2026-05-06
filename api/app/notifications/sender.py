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

Sentinel values for testing without pywebpush installed (e.g. on
Windows where cryptography wheels can be flaky): see
SENDER_FAKE_OK in send_one.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


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


async def _do_send(
    *,
    subscription_info: dict,
    payload_bytes: bytes,
    vapid: VapidConfig,
    ttl: int,
) -> tuple[int, str]:
    """Perform the encrypt + HTTP POST via pywebpush. Returns
    (http_status, response_body_text). Wrapped in its own helper
    so tests / dry-run callers can monkeypatch this seam without
    touching the surrounding audit logic.
    """
    # Imported lazily so the rest of the module loads on systems
    # without pywebpush (Windows dev, smoke tests).
    from pywebpush import WebPushException, webpush  # type: ignore

    try:
        resp = webpush(
            subscription_info=subscription_info,
            data=payload_bytes,
            vapid_private_key=vapid.private_key,
            vapid_claims={"sub": vapid.subject},
            ttl=ttl,
        )
        body_text = ""
        try:
            body_text = (resp.text or "")[:512]
        except Exception:
            pass
        return resp.status_code, body_text
    except WebPushException as exc:
        # pywebpush raises on non-2xx. Pull the upstream status if
        # the response is attached.
        status = 500
        body_text = str(exc)[:512]
        if exc.response is not None:
            status = exc.response.status_code
            try:
                body_text = (exc.response.text or "")[:512]
            except Exception:
                pass
        return status, body_text


async def send_one(
    conn,
    *,
    delivery_id: str,
    user_id: str,
    pref_id: str,
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
    # changed since claim).
    sub_row = await conn.fetchrow(
        """
        SELECT id::text          AS id,
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

    subscription_info = {
        "endpoint": sub_row["endpoint"],
        "keys": {
            "p256dh": sub_row["p256dh_key"],
            "auth": sub_row["auth_key"],
        },
    }
    provider = provider_from_endpoint(sub_row["endpoint"])

    # Step 3: encrypt + POST.
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        http_status, body_text = await _do_send(
            subscription_info=subscription_info,
            payload_bytes=payload_bytes,
            vapid=vapid,
            ttl=ttl,
        )
    except Exception as exc:
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

    # Step 4: classify and update.
    status, code = _classify_http(http_status)
    if status == "sent":
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

    # Step 5: failure path. 410/404 also expires the subscription.
    await _mark_failed(
        conn,
        delivery_id=delivery_id,
        http_status=http_status,
        code=code,
        detail=body_text,
        provider=provider,
    )
    if code == "unsubscribed":
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

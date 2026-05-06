"""OpsMemory notifications: Slack DM digest sender.

Step 6 of Chunk 10. Mirrors the audit contract of sender.py
(send_one for web_push) so the runner's --send mode can dispatch
slack_dm prefs through the same notification_deliveries lifecycle.

Delivery path: HTTP POST to an n8n webhook on auto.kyleconway.ai.
n8n owns the Slack app credentials and looks up the recipient's
Slack user_id by email (users.lookupByEmail) before calling
chat.postMessage. The OpsMemory API never holds Slack tokens
directly.

Auth: a shared bearer secret travels in the Authorization header
on top of n8n's existing Cloudflare Access gating. If CF Access
is ever misconfigured the bearer is a hard floor.

Status code mapping (mirrors sender.py classifications so audit
queries can grep across channels uniformly):
  2xx                   sent  (notification_deliveries.status='sent')
  401 / 403             config — bearer/Cloudflare Access broken
  404                   bad_request — n8n webhook missing
  4xx (other)           bad_request — n8n returned 4xx (e.g.
                          Slack lookupByEmail user_not_found)
  5xx                   transient — retryable later
  network / timeout     transient
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


log = logging.getLogger("opsmemory.notifications.slack_sender")


DEFAULT_N8N_TIMEOUT_SECONDS = 15
PROVIDER_LABEL = "n8n_bridge"  # matches the migration 0013 comment


@dataclass
class N8nConfig:
    webhook_url: str
    bearer: str | None  # optional but recommended


def load_n8n_config(env: dict[str, str] | None = None) -> N8nConfig:
    """Read N8N_NOTIFICATION_WEBHOOK_URL + bearer from env. Raises
    RuntimeError when the URL is unset; bearer is optional but
    logged.
    """
    e = env if env is not None else os.environ
    url = (e.get("N8N_NOTIFICATION_WEBHOOK_URL") or "").strip()
    bearer = (e.get("N8N_NOTIFICATION_WEBHOOK_BEARER") or "").strip() or None
    if not url:
        raise RuntimeError(
            "N8N_NOTIFICATION_WEBHOOK_URL is unset; the slack_dm "
            "sender requires it. Set it to the n8n webhook URL "
            "that fans out to chat.postMessage (see runbook)."
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(
            f"N8N_NOTIFICATION_WEBHOOK_URL must be a valid http(s) URL; got {url!r}"
        )
    if bearer is None:
        log.warning(
            "n8n_webhook_bearer_unset",
            extra={"detail": "no bearer token; relying on Cloudflare "
                              "Access for transport auth"},
        )
    return N8nConfig(webhook_url=url, bearer=bearer)


def preflight_n8n(env: dict[str, str] | None = None) -> N8nConfig:
    """Validate config + that an HTTP client is available BEFORE
    the runner starts claiming slack_dm rows. Mirrors the
    sender.py preflight_sender shape (Codex chunk-10-step5c2-close).

    httpx is already a dependency via the existing reconciliation
    pipeline (api/app/reconciliation/llm_client.py imports it),
    so we don't need to touch requirements.txt. We probe the
    import here so a missing wheel surfaces at preflight, not
    per-row.
    """
    cfg = load_n8n_config(env)
    try:
        import httpx  # noqa: F401  # presence check
    except ImportError as exc:
        raise RuntimeError(
            "httpx is not installed; the slack_dm sender requires it "
            "(also used by the reconciliation pipeline). Reinstall "
            "api/requirements.txt."
        ) from exc
    return cfg


@dataclass
class _RawSlackOutcome:
    kind: str            # 'http' | 'transport'
    http_status: int | None
    body_text: str


async def _do_send_n8n(
    *,
    cfg: N8nConfig,
    body: dict,
    timeout: float = DEFAULT_N8N_TIMEOUT_SECONDS,
) -> _RawSlackOutcome:
    """POST the digest body to the n8n webhook. Bearer is sent in
    Authorization. Returns _RawSlackOutcome covering HTTP status
    or transport failure.
    """
    import httpx  # type: ignore

    headers = {"Content-Type": "application/json"}
    if cfg.bearer:
        headers["Authorization"] = f"Bearer {cfg.bearer}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                cfg.webhook_url,
                content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                headers=headers,
            )
        body_text = ""
        try:
            body_text = (resp.text or "")[:512]
        except Exception:
            pass
        return _RawSlackOutcome(
            kind="http", http_status=resp.status_code, body_text=body_text,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        return _RawSlackOutcome(
            kind="transport", http_status=None,
            body_text=f"{type(exc).__name__}: {str(exc)[:256]}",
        )
    except httpx.HTTPError as exc:
        return _RawSlackOutcome(
            kind="transport", http_status=None,
            body_text=f"{type(exc).__name__}: {str(exc)[:256]}",
        )


def _classify_slack_http(status: int) -> tuple[str, str]:
    """Map n8n HTTP response status to (audit_status, code).

    Different from web push only at the edges: 401/403 mean the
    bearer / CF Access is broken (operator-actionable -> 'config'),
    not 'unsubscribed'.
    """
    if 200 <= status < 300:
        return "sent", "sent"
    if status in (401, 403):
        return "failed", "config"
    if 400 <= status < 500:
        return "failed", "bad_request"
    return "failed", "transient"


async def _mark_failed(
    conn,
    *,
    delivery_id: str,
    http_status: int | None,
    code: str,
    detail: str | None,
) -> None:
    err: dict[str, Any] = {"code": code}
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
        delivery_id, PROVIDER_LABEL, err,
    )


async def send_one_slack(
    conn,
    *,
    delivery_id: str,
    user_id: str,
    pref_id: str | None,
    user_email: str,
    user_display_name: str | None,
    payload: dict,
    n8n_config: N8nConfig,
    timeout: float = DEFAULT_N8N_TIMEOUT_SECONDS,
) -> "SendResult":
    """Ship one already-claimed slack_dm delivery row through the
    n8n webhook bridge. Mirrors sender.send_one's audit shape.

    The webhook body deliberately omits any Slack tokens — n8n
    holds those. The OpsMemory API only knows the user's email
    and the rendered digest payload; n8n is responsible for
    looking up the Slack user_id and calling chat.postMessage.
    """
    # Local import so callers without httpx (smoke tests) can
    # still load the module.
    from .sender import SendResult

    now = datetime.now(timezone.utc)

    await conn.execute(
        """
        UPDATE notification_deliveries
           SET attempted_at = $2::timestamptz
         WHERE id = $1::uuid
        """,
        delivery_id, now,
    )

    if not user_email:
        # Without an email, n8n can't lookupByEmail -> can't DM.
        await _mark_failed(
            conn, delivery_id=delivery_id, http_status=None,
            code="bad_request",
            detail="user has no email; n8n cannot resolve Slack user_id",
        )
        return SendResult(
            status="failed", http_status=None, code="bad_request",
            detail="user_email missing", delivery_id=delivery_id,
        )

    body = {
        "user_id": user_id,
        "user_email": user_email,
        "user_display_name": user_display_name,
        "delivery_id": delivery_id,
        "pref_id": pref_id,
        "channel": "slack_dm",
        "title": payload.get("title"),
        "body": payload.get("body"),
        "items": payload.get("items") or [],
        "scheduled_for": payload.get("scheduled_for"),
    }

    try:
        outcome = await _do_send_n8n(cfg=n8n_config, body=body, timeout=timeout)
    except Exception as exc:
        log.exception("slack_sender_unexpected_error", extra={
            "delivery_id": delivery_id,
            "user_id": user_id,
            "pref_id": pref_id,
        })
        await _mark_failed(
            conn, delivery_id=delivery_id, http_status=None,
            code="config",
            detail=f"sender exception: {type(exc).__name__}: {str(exc)[:256]}",
        )
        return SendResult(
            status="failed", http_status=None, code="config",
            detail=str(exc)[:256], delivery_id=delivery_id,
        )

    if outcome.kind == "transport":
        await _mark_failed(
            conn, delivery_id=delivery_id, http_status=None,
            code="transient", detail=outcome.body_text,
        )
        log.info("slack_sender_failed", extra={
            "delivery_id": delivery_id, "user_id": user_id,
            "pref_id": pref_id,
            "http_status": None, "code": "transient",
        })
        return SendResult(
            status="failed", http_status=None, code="transient",
            detail=outcome.body_text, delivery_id=delivery_id,
        )

    http_status = outcome.http_status or 0
    body_text = outcome.body_text
    status, code = _classify_slack_http(http_status)
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
            delivery_id, PROVIDER_LABEL,
        )
        log.info("slack_sender_sent", extra={
            "delivery_id": delivery_id, "user_id": user_id,
            "pref_id": pref_id, "http_status": http_status,
        })
        return SendResult(
            status="sent", http_status=http_status, code="sent",
            detail=None, delivery_id=delivery_id,
        )

    await _mark_failed(
        conn, delivery_id=delivery_id, http_status=http_status,
        code=code, detail=body_text,
    )
    log.info("slack_sender_failed", extra={
        "delivery_id": delivery_id, "user_id": user_id,
        "pref_id": pref_id,
        "http_status": http_status, "code": code,
    })
    return SendResult(
        status="failed", http_status=http_status, code=code,
        detail=body_text, delivery_id=delivery_id,
    )

"""OpsMemory notifications: weekly Gmail digest sender.

Mirrors api/app/notifications/slack_sender.py shape. The runner
(scripts/run_weekly_digest.py) calls send_one_gmail_draft per
claimed weekly_digest_runs row; this module POSTs the rendered
digest payload to an n8n webhook that calls Gmail drafts.create
on a designated mailbox.

Per docs/01-design.md locked decision: Gmail tools never auto-fire
to customers/vendors from tracker state. Drafts only. n8n's job is
drafts.create (NOT messages.send). The audit row carries the
returned draft_id for operator follow-up.

Status code mapping (mirrors slack_sender so audit queries are
uniform across channels):
  2xx                   sent (weekly_digest_runs.status='sent',
                              sent_at=now(), draft_id captured)
  401 / 403             config — bearer or CF Access broken
  other 4xx             bad_request — n8n returned 4xx (e.g.
                          drafts.create rejected by Gmail)
  5xx / network         transient — retryable on next cron tick
                          if the operator re-queues
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


log = logging.getLogger("opsmemory.notifications.gmail_sender")


DEFAULT_GMAIL_TIMEOUT_SECONDS = 30
PROVIDER_LABEL = "n8n_gmail_drafts"


@dataclass
class GmailN8nConfig:
    webhook_url: str
    bearer: str | None  # optional but recommended (defense in
                        # depth on top of Cloudflare Access)


def load_gmail_n8n_config(env: dict[str, str] | None = None) -> GmailN8nConfig:
    """Read N8N_GMAIL_DIGEST_WEBHOOK_URL + bearer from env. Raises
    RuntimeError when the URL is unset or malformed; bearer is
    optional but logs a warning.
    """
    e = env if env is not None else os.environ
    url = (e.get("N8N_GMAIL_DIGEST_WEBHOOK_URL") or "").strip()
    bearer = (e.get("N8N_GMAIL_DIGEST_WEBHOOK_BEARER") or "").strip() or None
    if not url:
        raise RuntimeError(
            "N8N_GMAIL_DIGEST_WEBHOOK_URL is unset; the weekly Gmail "
            "digest sender requires it. Set it to the n8n webhook URL "
            "that fans out to Gmail drafts.create (see runbook)."
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(
            f"N8N_GMAIL_DIGEST_WEBHOOK_URL must be a valid http(s) "
            f"URL; got {url!r}"
        )
    if bearer is None:
        log.warning(
            "n8n_gmail_webhook_bearer_unset",
            extra={"detail": "no bearer token; relying on Cloudflare "
                              "Access for transport auth"},
        )
    return GmailN8nConfig(webhook_url=url, bearer=bearer)


def preflight_gmail_n8n(env: dict[str, str] | None = None) -> GmailN8nConfig:
    """Validate config + httpx import BEFORE the runner starts
    claiming rows. Mirrors preflight_n8n in slack_sender.py.
    """
    cfg = load_gmail_n8n_config(env)
    try:
        import httpx  # noqa: F401  # presence check
    except ImportError as exc:
        raise RuntimeError(
            "httpx is not installed; the weekly Gmail digest sender "
            "requires it (also used by the reconciliation pipeline). "
            "Reinstall api/requirements.txt."
        ) from exc
    return cfg


@dataclass
class GmailSendResult:
    status: str           # 'sent' | 'failed'
    http_status: int | None
    code: str | None      # 'sent' | 'config' | 'bad_request' | 'transient'
    detail: str | None    # human-readable error or response excerpt
    draft_id: str | None  # Gmail draftId on success
    run_id: str


@dataclass
class _RawGmailOutcome:
    kind: str            # 'http' | 'transport'
    http_status: int | None
    body_text: str
    body_json: dict | None


def _classify_gmail_http(status: int) -> tuple[str, str]:
    if 200 <= status < 300:
        return "sent", "sent"
    if status in (401, 403):
        return "failed", "config"
    if 400 <= status < 500:
        return "failed", "bad_request"
    return "failed", "transient"


async def _do_send_gmail_n8n(
    *,
    cfg: GmailN8nConfig,
    body: dict,
    timeout: float = DEFAULT_GMAIL_TIMEOUT_SECONDS,
) -> _RawGmailOutcome:
    """POST the digest body to the n8n webhook. Returns
    _RawGmailOutcome covering HTTP status + parsed JSON (when
    available) or transport failure.
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
        body_json: dict | None = None
        try:
            body_text = (resp.text or "")[:1024]
        except Exception:
            pass
        try:
            j = resp.json()
            if isinstance(j, dict):
                body_json = j
        except Exception:
            pass
        return _RawGmailOutcome(
            kind="http", http_status=resp.status_code,
            body_text=body_text, body_json=body_json,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        return _RawGmailOutcome(
            kind="transport", http_status=None, body_text=f"{type(exc).__name__}: {str(exc)[:256]}",
            body_json=None,
        )
    except httpx.HTTPError as exc:
        return _RawGmailOutcome(
            kind="transport", http_status=None, body_text=f"{type(exc).__name__}: {str(exc)[:256]}",
            body_json=None,
        )


async def _mark_failed(
    conn,
    *,
    run_id: str,
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
        UPDATE weekly_digest_runs
           SET status    = 'failed',
               failed_at = now(),
               error     = $2::jsonb
         WHERE id = $1::uuid
        """,
        run_id, err,
    )


async def send_one_gmail_draft(
    conn,
    *,
    run_id: str,
    business: dict,
    payload: dict,
    n8n_config: GmailN8nConfig,
    timeout: float = DEFAULT_GMAIL_TIMEOUT_SECONDS,
) -> GmailSendResult:
    """Ship one already-claimed weekly_digest_runs row through
    the n8n webhook bridge. Mirrors slack_sender.send_one_slack
    audit transitions:
      1. UPDATE attempted_at = now() so retries can detect.
      2. POST to n8n with subject + bodies + recipients.
      3. n8n calls Gmail drafts.create on its own credentials.
      4. n8n responds with {draft_id, gmail_draft_url} on success.
      5. UPDATE weekly_digest_runs to sent + draft_id, OR
         failed + error JSONB.

    The n8n webhook does NOT need to know OpsMemory's auth model;
    bearer goes in Authorization, CF Access gates the network
    path.
    """
    await conn.execute(
        """
        UPDATE weekly_digest_runs
           SET attempted_at = now()
         WHERE id = $1::uuid
        """,
        run_id,
    )

    if not (payload.get("to") or payload.get("cc") or payload.get("bcc")):
        # Empty allowlist would mean the runner shouldn't have
        # claimed this row in the first place — this is defense
        # in depth.
        await _mark_failed(
            conn, run_id=run_id, http_status=None,
            code="bad_request",
            detail="no recipients in any role; allowlist appears empty",
        )
        return GmailSendResult(
            status="failed", http_status=None, code="bad_request",
            detail="empty recipients", draft_id=None, run_id=run_id,
        )

    body = {
        "business_slug": business.get("slug"),
        "business_name": business.get("name"),
        "run_id": run_id,
        "subject": payload.get("subject"),
        "html_body": payload.get("html_body"),
        "text_body": payload.get("text_body"),
        "to": payload.get("to") or [],
        "cc": payload.get("cc") or [],
        "bcc": payload.get("bcc") or [],
        "counts": payload.get("counts") or {},
        "week_start_iso": payload.get("week_start_iso"),
        "week_end_iso": payload.get("week_end_iso"),
        "generated_at": payload.get("generated_at"),
    }

    try:
        outcome = await _do_send_gmail_n8n(
            cfg=n8n_config, body=body, timeout=timeout,
        )
    except Exception as exc:
        log.exception("gmail_sender_unexpected_error", extra={
            "run_id": run_id, "business_slug": business.get("slug"),
        })
        await _mark_failed(
            conn, run_id=run_id, http_status=None,
            code="config",
            detail=f"sender exception: {type(exc).__name__}: {str(exc)[:256]}",
        )
        return GmailSendResult(
            status="failed", http_status=None, code="config",
            detail=str(exc)[:256], draft_id=None, run_id=run_id,
        )

    if outcome.kind == "transport":
        await _mark_failed(
            conn, run_id=run_id, http_status=None,
            code="transient", detail=outcome.body_text,
        )
        log.info("gmail_sender_failed", extra={
            "run_id": run_id, "business_slug": business.get("slug"),
            "http_status": None, "code": "transient",
        })
        return GmailSendResult(
            status="failed", http_status=None, code="transient",
            detail=outcome.body_text, draft_id=None, run_id=run_id,
        )

    http_status = outcome.http_status or 0
    body_text = outcome.body_text
    status, code = _classify_gmail_http(http_status)
    if status == "sent":
        # Pull draft_id from n8n's JSON response if present.
        draft_id = None
        if outcome.body_json:
            v = outcome.body_json.get("draft_id")
            if isinstance(v, str) and v:
                draft_id = v[:256]
        await conn.execute(
            """
            UPDATE weekly_digest_runs
               SET status   = 'sent',
                   sent_at  = now(),
                   draft_id = $2::text,
                   error    = '{}'::jsonb
             WHERE id = $1::uuid
            """,
            run_id, draft_id,
        )
        log.info("gmail_sender_sent", extra={
            "run_id": run_id, "business_slug": business.get("slug"),
            "http_status": http_status, "draft_id": draft_id,
        })
        return GmailSendResult(
            status="sent", http_status=http_status, code="sent",
            detail=None, draft_id=draft_id, run_id=run_id,
        )

    await _mark_failed(
        conn, run_id=run_id, http_status=http_status,
        code=code, detail=body_text,
    )
    log.info("gmail_sender_failed", extra={
        "run_id": run_id, "business_slug": business.get("slug"),
        "http_status": http_status, "code": code,
    })
    return GmailSendResult(
        status="failed", http_status=http_status, code=code,
        detail=body_text, draft_id=None, run_id=run_id,
    )

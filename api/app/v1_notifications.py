"""OpsMemory v1 notification prefs + web push subscription API
(Chunk 10 step 2).

Endpoints (all user-only — admin/service principals 403):

  GET    /v1/notifications/prefs                      list this user's prefs
  PATCH  /v1/notifications/prefs/{channel}            upsert one pref
  GET    /v1/notifications/web_push/subscriptions     list this user's devices
  POST   /v1/notifications/web_push/subscriptions     upsert a subscription
  DELETE /v1/notifications/web_push/subscriptions/{id} soft-revoke
  GET    /v1/notifications/vapid_public               public VAPID key

Per Codex chunk-10-step1 STEP 2 PLAN:
  - Test-send endpoint is deferred to the sender commit (step 5).
  - VAPID validation happens at app startup (main.lifespan).
  - GET /prefs synthesizes defaults for channels the user hasn't
    saved yet; PATCH UPSERTs.
  - Subscription POST is UPSERT on the natural unique key
    (endpoint), so re-registering the same device updates last_seen
    + keys + label without creating a duplicate row.
  - DELETE is a soft revoke (status='revoked'); audit history stays
    intact via UPDATE rather than DELETE.

Auth model: every authenticated user manages their own prefs.
Admin doesn't get cross-user access — admins manage their own prefs
through the same endpoints. Service principals are rejected (no
machine-managed prefs).
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field, field_validator

from .auth import Principal, require_principal

log = logging.getLogger("opsmemory.v1_notifications")

router = APIRouter(prefix="/v1/notifications")


# ---------------------------------------------------------------------------
# Channels (mirror the schema CHECK lists from migration 0013)
# ---------------------------------------------------------------------------

VALID_CHANNELS = frozenset({"web_push", "slack_dm", "email_digest"})


def _require_user(principal: Principal) -> None:
    """Notification management is user-scoped. Admin manages their
    OWN prefs through the same path; admin role doesn't grant
    cross-user access here. Service principals are rejected.
    """
    if principal.principal_type != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="notification prefs require a user principal",
        )


# ---------------------------------------------------------------------------
# Pydantic bodies
# ---------------------------------------------------------------------------

# Codex chunk-10-step3b1 plan-review (1+5): schedule semantics are
# now validated server-side. Without this, hour=99 / kind='event' /
# missing weekday would silently save and a future scheduler run
# would crash mid-job. Validation is intentionally minimal — the
# scheduler module (step 4) will share the same validator.
_SCHEDULE_KINDS = frozenset({"daily", "weekly"})
_WEEKDAYS = frozenset({"mon", "tue", "wed", "thu", "fri", "sat", "sun"})

# Best-effort timezone whitelist. We don't import zoneinfo here to
# keep this module dependency-free; the operator's deploy ships
# tzdata. Pattern: 'Region/City' or 'UTC'. The scheduler does the
# real resolve at fire time.
import re as _re
_TIMEZONE_RE = _re.compile(r"^(UTC|[A-Z][A-Za-z_]+/[A-Z][A-Za-z_]+(/[A-Z][A-Za-z_]+)?)$")


def _validate_schedule_object(schedule: dict) -> None:
    """Raise ValueError on bad schedule shape. Used by PrefPatchBody.
    Step 4 (digest scheduler) will import this for fire-time validation
    so client and scheduler can never disagree.
    """
    kind = schedule.get("kind")
    if kind not in _SCHEDULE_KINDS:
        raise ValueError(
            f"schedule.kind must be one of {sorted(_SCHEDULE_KINDS)}; got {kind!r}"
        )
    # Codex chunk-10-step3b1-close (blocker 2): bool is an int
    # subclass in Python, so isinstance(True, int) is True. Use a
    # strict type check so JSON `true`/`false` doesn't slip through
    # as 1/0.
    hour = schedule.get("hour")
    if type(hour) is not int or not (0 <= hour <= 23):
        raise ValueError(f"schedule.hour must be int 0..23; got {hour!r}")
    minute = schedule.get("minute")
    if type(minute) is not int or not (0 <= minute <= 59):
        raise ValueError(f"schedule.minute must be int 0..59; got {minute!r}")
    tz = schedule.get("timezone")
    if not isinstance(tz, str) or not _TIMEZONE_RE.match(tz):
        raise ValueError(
            "schedule.timezone must be a string IANA tz id "
            "(e.g. 'America/Phoenix' or 'UTC')"
        )
    if kind == "weekly":
        weekday = schedule.get("weekday")
        if weekday not in _WEEKDAYS:
            raise ValueError(
                f"schedule.weekday required for weekly kind; "
                f"must be one of {sorted(_WEEKDAYS)}; got {weekday!r}"
            )


class PrefPatchBody(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool | None = None
    schedule: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None

    @field_validator("schedule", "settings")
    @classmethod
    def _is_object(cls, v):
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("must be a JSON object")
        return v

    @field_validator("schedule")
    @classmethod
    def _schedule_shape(cls, v):
        if v is None:
            return v
        _validate_schedule_object(v)
        return v


class SubscriptionUpsertBody(BaseModel):
    """Mirrors the shape returned by PushSubscription.toJSON() in the
    PWA, plus an operator-set device_label.
    """
    model_config = {"extra": "forbid"}
    endpoint: str = Field(..., min_length=16, max_length=2048)
    p256dh_key: str = Field(..., min_length=16, max_length=256)
    auth_key: str = Field(..., min_length=16, max_length=64)
    device_label: str | None = Field(default=None, max_length=128)
    user_agent: str | None = Field(default=None, max_length=512)


# ---------------------------------------------------------------------------
# Default pref shapes for "synthesize on GET" behavior
# ---------------------------------------------------------------------------
# Per Codex chunk-10-step1 (h): don't seed prefs in the migration.
# GET /v1/notifications/prefs returns these defaults for channels the
# user hasn't saved yet, with enabled=false. The PWA Settings UI sees
# a complete pref list and PATCHes the ones the user toggles.

_DEFAULT_PREFS: dict[str, dict[str, Any]] = {
    "web_push": {
        "schedule": {
            "kind": "daily",
            "hour": 7,
            "minute": 0,
            "timezone": "America/Phoenix",
        },
        "settings": {
            "include_stale": True,
            "include_completed": False,
            "stale_days": 7,
        },
    },
    "slack_dm": {
        "schedule": {
            "kind": "daily",
            "hour": 7,
            "minute": 0,
            "timezone": "America/Phoenix",
        },
        "settings": {
            "include_stale": True,
            "send_via": "n8n",
        },
    },
    "email_digest": {
        "schedule": {
            "kind": "weekly",
            "weekday": "mon",
            "hour": 8,
            "minute": 0,
            "timezone": "America/Phoenix",
        },
        "settings": {},
    },
}


def _serialize_pref(row: Any) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "channel": row["channel"],
        "enabled": bool(row["enabled"]),
        "schedule": row["schedule"] or {},
        "settings": row["settings"] or {},
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _serialize_subscription(row: Any) -> dict:
    # Note: p256dh_key + auth_key are NOT returned to the client.
    # The PWA already has them locally (from PushSubscription.getKey)
    # and the API only needs to surface the user's own device list.
    return {
        "id": row["id"],
        "endpoint": row["endpoint"],
        "device_label": row.get("device_label"),
        "user_agent": row.get("user_agent"),
        "status": row["status"],
        "created_at": row.get("created_at"),
        "last_seen_at": row.get("last_seen_at"),
    }


# ---------------------------------------------------------------------------
# GET /v1/notifications/prefs
# ---------------------------------------------------------------------------

@router.get("/prefs")
async def list_prefs(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Return the user's stored prefs, synthesizing defaults for any
    channel they haven't yet saved. The PWA Settings UI binds against
    this list and PATCHes the ones the user changes.
    """
    _require_user(principal)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text          AS id,
                   user_id::text     AS user_id,
                   channel           AS channel,
                   enabled           AS enabled,
                   schedule          AS schedule,
                   settings          AS settings,
                   created_at::text  AS created_at,
                   updated_at::text  AS updated_at
            FROM notification_prefs
            WHERE user_id = $1::uuid
            ORDER BY channel
            """,
            principal.id,
        )
    saved = {r["channel"]: r for r in rows}
    items: list[dict] = []
    for ch in sorted(VALID_CHANNELS):
        if ch in saved:
            items.append(_serialize_pref(saved[ch]))
        else:
            defaults = _DEFAULT_PREFS.get(ch, {"schedule": {}, "settings": {}})
            items.append({
                "id": None,
                "user_id": principal.id,
                "channel": ch,
                "enabled": False,
                "schedule": defaults["schedule"],
                "settings": defaults["settings"],
                "created_at": None,
                "updated_at": None,
                "synthesized_default": True,
            })
    return {"items": items}


# ---------------------------------------------------------------------------
# PATCH /v1/notifications/prefs/{channel}
# ---------------------------------------------------------------------------

@router.patch("/prefs/{channel}")
async def patch_pref(
    body: PrefPatchBody,
    request: Request,
    channel: str = Path(..., min_length=1, max_length=32),
    principal: Principal = Depends(require_principal),
) -> dict:
    """UPSERT one pref for the calling user. Channel must be in the
    VALID_CHANNELS set. Body fields are all optional; missing fields
    keep the prior server value (or the default for a fresh row).

    Schedule + settings are passed through to the jsonb columns
    after structural validation. Per Codex chunk-10-step1 (b):
    no DB-level CHECK on the schedule shape yet; the scheduler
    module owns that contract in step 4.
    """
    _require_user(principal)
    if channel not in VALID_CHANNELS:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_channel", "got": channel,
                    "allowed": sorted(VALID_CHANNELS)},
        )

    defaults = _DEFAULT_PREFS.get(channel, {"schedule": {}, "settings": {}})

    pool = request.app.state.db
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id::text AS id, enabled, schedule, settings "
                "FROM notification_prefs "
                "WHERE user_id = $1::uuid AND channel = $2 FOR UPDATE",
                principal.id, channel,
            )
            new_enabled = (
                body.enabled if body.enabled is not None
                else (existing["enabled"] if existing else False)
            )
            new_schedule = (
                body.schedule if body.schedule is not None
                else (existing["schedule"] if existing else defaults["schedule"])
            )
            new_settings = (
                body.settings if body.settings is not None
                else (existing["settings"] if existing else defaults["settings"])
            )

            # Codex chunk-10-step3b1-close (blocker 3): the body-level
            # @field_validator only fires on body.schedule. If the
            # caller sends just {enabled: true} and the existing DB
            # row has a malformed schedule, the bad row would be
            # preserved AND re-enabled. Validate the merged
            # new_schedule before the write so a corrupt row can
            # never be re-armed without the caller fixing it.
            if not isinstance(new_schedule, dict):
                raise HTTPException(status_code=422,
                    detail={"code": "schedule_invalid",
                            "reason": "schedule must be a JSON object"})
            try:
                _validate_schedule_object(new_schedule)
            except ValueError as exc:
                raise HTTPException(status_code=422,
                    detail={"code": "schedule_invalid", "reason": str(exc)})

            if existing:
                row = await conn.fetchrow(
                    """
                    UPDATE notification_prefs
                       SET enabled  = $2,
                           schedule = $3::jsonb,
                           settings = $4::jsonb
                     WHERE id = $1::uuid
                    RETURNING id::text AS id, user_id::text AS user_id,
                              channel, enabled, schedule, settings,
                              created_at::text AS created_at,
                              updated_at::text AS updated_at
                    """,
                    existing["id"], new_enabled, new_schedule, new_settings,
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO notification_prefs
                      (user_id, channel, enabled, schedule, settings)
                    VALUES
                      ($1::uuid, $2, $3, $4::jsonb, $5::jsonb)
                    RETURNING id::text AS id, user_id::text AS user_id,
                              channel, enabled, schedule, settings,
                              created_at::text AS created_at,
                              updated_at::text AS updated_at
                    """,
                    principal.id, channel, new_enabled, new_schedule, new_settings,
                )

    log.info("notifications_pref_patched", extra={
        "user_id": principal.id,
        "channel": channel,
        "enabled": new_enabled,
    })
    return _serialize_pref(row)


# ---------------------------------------------------------------------------
# GET /v1/notifications/web_push/subscriptions
# ---------------------------------------------------------------------------

@router.get("/web_push/subscriptions")
async def list_subscriptions(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    _require_user(principal)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text          AS id,
                   endpoint          AS endpoint,
                   device_label      AS device_label,
                   user_agent        AS user_agent,
                   status            AS status,
                   created_at::text  AS created_at,
                   last_seen_at::text AS last_seen_at
            FROM web_push_subscriptions
            WHERE user_id = $1::uuid AND status = 'active'
            ORDER BY created_at DESC
            """,
            principal.id,
        )
    return {"items": [_serialize_subscription(r) for r in rows]}


# ---------------------------------------------------------------------------
# POST /v1/notifications/web_push/subscriptions
# ---------------------------------------------------------------------------

@router.post("/web_push/subscriptions", status_code=201)
async def upsert_subscription(
    body: SubscriptionUpsertBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Register (or re-register) a Web Push subscription.

    The PushSubscription.endpoint is the natural unique key. Re-
    registering on the same browser/device returns the same endpoint
    so the row UPDATEs instead of duplicating. The keys may rotate
    on resubscribe — accept the latest. status is reset to 'active'
    on every upsert (so an expired/revoked sub that the user
    explicitly re-enables comes back online).
    """
    _require_user(principal)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO web_push_subscriptions
                  (user_id, endpoint, p256dh_key, auth_key,
                   device_label, user_agent, status, last_seen_at)
                VALUES
                  ($1::uuid, $2, $3, $4, $5, $6, 'active', now())
                ON CONFLICT (endpoint) DO UPDATE
                  SET user_id      = EXCLUDED.user_id,
                      p256dh_key   = EXCLUDED.p256dh_key,
                      auth_key     = EXCLUDED.auth_key,
                      device_label = COALESCE(EXCLUDED.device_label,
                                                web_push_subscriptions.device_label),
                      user_agent   = COALESCE(EXCLUDED.user_agent,
                                                web_push_subscriptions.user_agent),
                      status       = 'active',
                      last_seen_at = now()
                RETURNING id::text          AS id,
                          endpoint          AS endpoint,
                          device_label      AS device_label,
                          user_agent        AS user_agent,
                          status            AS status,
                          created_at::text  AS created_at,
                          last_seen_at::text AS last_seen_at
                """,
                principal.id,
                body.endpoint,
                body.p256dh_key,
                body.auth_key,
                body.device_label,
                body.user_agent,
            )
        except asyncpg.UniqueViolationError:
            # Should be unreachable thanks to ON CONFLICT, but defensive.
            raise HTTPException(
                status_code=409,
                detail={"code": "subscription_endpoint_in_use"},
            )

    log.info("notifications_subscription_upserted", extra={
        "user_id": principal.id,
        "subscription_id": row["id"],
    })
    return _serialize_subscription(row)


# ---------------------------------------------------------------------------
# DELETE /v1/notifications/web_push/subscriptions/{id}
# ---------------------------------------------------------------------------

@router.delete("/web_push/subscriptions/{subscription_id}")
async def revoke_subscription(
    subscription_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Soft-revoke a subscription (status='revoked'). Audit history
    stays intact. Re-registering the same endpoint via POST will
    flip it back to 'active'.

    Authz: a user can only revoke their OWN subscriptions; admin
    doesn't get cross-user revoke through this endpoint.
    """
    _require_user(principal)
    sid = str(subscription_id)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        # Codex chunk-10-step2 (e): make this idempotent. Two-phase
        # check: first see if the row exists for this user (any
        # status); if yes, set to revoked and return success even if
        # already revoked. Only return 404 when the id genuinely
        # doesn't belong to this user (or doesn't exist at all).
        # This makes retry/double-click/ambiguous-network safe.
        async with conn.transaction():
            owner = await conn.fetchrow(
                """
                SELECT id, status
                  FROM web_push_subscriptions
                 WHERE id = $1::uuid
                   AND user_id = $2::uuid
                 FOR UPDATE
                """,
                sid, principal.id,
            )
            if owner is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "subscription_not_found", "id": sid},
                )
            already_revoked = owner["status"] == "revoked"
            if not already_revoked:
                await conn.execute(
                    """
                    UPDATE web_push_subscriptions
                       SET status = 'revoked'
                     WHERE id = $1::uuid
                    """,
                    sid,
                )
    log.info("notifications_subscription_revoked", extra={
        "user_id": principal.id,
        "subscription_id": sid,
        "already_revoked": already_revoked,
    })
    return {"id": sid, "status": "revoked"}


# ---------------------------------------------------------------------------
# GET /v1/notifications/vapid_public
# ---------------------------------------------------------------------------

@router.get("/vapid_public")
async def get_vapid_public(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Return the server's VAPID public key for the PWA to pass to
    PushManager.subscribe(). Cached at startup (api.app.main.lifespan)
    so this endpoint is a fast pass-through.

    Returns 503 if the server didn't validate VAPID config at boot
    (operator hasn't set VAPID_PUBLIC_KEY etc.).
    """
    _require_user(principal)
    public_key = getattr(request.app.state, "vapid_public_key", None)
    if not public_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "vapid_unconfigured",
                    "detail": "server VAPID keys not configured; ask admin"},
        )
    return {"public_key": public_key}


# ---------------------------------------------------------------------------
# Startup VAPID validator (called from main.lifespan)
# ---------------------------------------------------------------------------

_VAPID_PUBKEY_RE = re.compile(r"^[A-Za-z0-9_\-]{86,90}$")  # base64url, ~88 chars
_VAPID_PRIVKEY_RE = re.compile(r"^[A-Za-z0-9_\-]{42,48}$")  # base64url, ~43 chars
_VAPID_SUBJECT_RE = re.compile(r"^(mailto:|https://)[^\s]+$")


def validate_vapid_config(env: dict[str, str]) -> dict[str, str] | None:
    """Validate VAPID env at app startup. Returns dict with the
    public key when complete + valid, None when not configured (the
    optional case — operator hasn't set up Web Push yet, but the
    rest of the API still boots).

    Raises ValueError when partially configured (operator started
    setting vars but stopped — fail fast so the deploy doesn't
    silently ship broken Web Push).
    """
    public_key = (env.get("VAPID_PUBLIC_KEY") or "").strip()
    private_key = (env.get("VAPID_PRIVATE_KEY") or "").strip()
    subject = (env.get("VAPID_SUBJECT") or "").strip()

    if not public_key and not private_key and not subject:
        return None  # cleanly unconfigured

    missing = [
        name for name, val in (
            ("VAPID_PUBLIC_KEY", public_key),
            ("VAPID_PRIVATE_KEY", private_key),
            ("VAPID_SUBJECT", subject),
        ) if not val
    ]
    if missing:
        raise ValueError(
            f"VAPID partially configured; missing: {missing}. "
            "Set all three or unset all three."
        )

    if not _VAPID_PUBKEY_RE.match(public_key):
        raise ValueError(
            "VAPID_PUBLIC_KEY does not look like a base64url-encoded "
            "P-256 uncompressed public key (~88 chars)"
        )
    if not _VAPID_PRIVKEY_RE.match(private_key):
        raise ValueError(
            "VAPID_PRIVATE_KEY does not look like a base64url-encoded "
            "P-256 private scalar (~43 chars)"
        )
    if not _VAPID_SUBJECT_RE.match(subject):
        raise ValueError(
            "VAPID_SUBJECT must start with 'mailto:' or 'https://' "
            "per RFC 8292 (e.g. 'mailto:ops@kyleconway.ai')"
        )

    # Caller stores public_key on app.state; private_key + subject
    # stay in os.environ for the sender (step 5) to read.
    return {"public_key": public_key}

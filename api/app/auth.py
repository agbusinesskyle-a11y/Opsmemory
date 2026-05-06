"""OpsMemory authentication.

Two principal types:

1. **User** — Cloudflare Access JWT (header `Cf-Access-Jwt-Assertion`).
   Verified RS256 against ``${CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs``.
   Email claim is looked up in ``user_identities`` (provider='cloudflare_access')
   and joined to ``users``. Active user required.

2. **Service** — header ``X-OpsMemory-Service-Key``. HMAC-SHA256 of the raw
   key with ``SERVICE_KEY_PEPPER``. Match by ``key_prefix``, then constant-time
   compare ``key_hash``. Active and not-expired required.

Local dev mode (``AUTH_MODE=local``) skips JWT verification and uses
``LOCAL_DEV_EMAIL``. Optional ``X-Dev-User-Email`` switching when
``ALLOW_DEV_USER_SWITCH=true``. NEVER enable in production.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import jwt
from fastapi import HTTPException, Request, status
from jwt import InvalidTokenError, PyJWKClient

log = logging.getLogger("opsmemory.auth")


@dataclass(frozen=True)
class Principal:
    principal_type: Literal["user", "service"]
    id: str
    display_name: str
    email: str | None
    role: str
    businesses: list[dict[str, Any]] = field(default_factory=list)
    permissions: dict[str, bool] = field(default_factory=dict)
    auth_method: str = ""


_jwk_client: PyJWKClient | None = None


def _team_domain() -> str:
    raw = os.environ["CF_ACCESS_TEAM_DOMAIN"].rstrip("/")
    if not raw.startswith("https://"):
        raw = f"https://{raw}"
    return raw


def _jwks() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(f"{_team_domain()}/cdn-cgi/access/certs")
    return _jwk_client


def _permissions(role: str, scopes: list[str] | None = None) -> dict[str, bool]:
    scopes = scopes or []
    if role == "admin":
        return {
            "can_view_all_businesses": True,
            "can_manage_users": True,
            "can_restore": True,
            "can_hard_delete": True,
            "can_use_service_api": False,
        }
    if role == "owner":
        return {
            "can_view_all_businesses": False,
            "can_manage_users": False,
            "can_restore": False,
            "can_hard_delete": False,
            "can_use_service_api": False,
        }
    return {
        "can_view_all_businesses": "businesses:read" in scopes,
        "can_manage_users": False,
        "can_restore": False,
        "can_hard_delete": False,
        "can_use_service_api": True,
    }


def _verify_cf_jwt(request: Request, require_email: bool) -> dict[str, Any]:
    token = request.headers.get("Cf-Access-Jwt-Assertion")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing access token",
        )

    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=os.environ["CF_ACCESS_AUD"],
            issuer=_team_domain(),
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except InvalidTokenError as exc:
        log.info(f"jwt_invalid reason={exc.__class__.__name__}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid access token",
        ) from exc

    if require_email and not claims.get("email"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing access email",
        )

    return claims


async def _load_user(
    request: Request,
    email: str,
    claims: dict[str, Any],
    auth_method: str,
) -> Principal:
    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              ui.id AS identity_id,
              u.id::text AS id,
              u.email::text AS email,
              u.display_name,
              u.role::text AS role
            FROM user_identities ui
            JOIN users u ON u.id = ui.user_id
            WHERE ui.provider = 'cloudflare_access'
              AND ui.email = $1
              AND u.status = 'active'
            """,
            email.lower(),
        )
        if not row:
            log.info(f"user_not_authorized email={email}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="user not authorized",
            )

        businesses = await conn.fetch(
            """
            SELECT b.id::text AS id,
                   b.slug::text AS slug,
                   b.name,
                   bm.role::text AS role
            FROM business_memberships bm
            JOIN businesses b ON b.id = bm.business_id
            WHERE bm.user_id = $1::uuid
              AND bm.status = 'active'
              AND b.deletion_state = 'active'
            ORDER BY b.name
            """,
            row["id"],
        )

        # Persist a redacted subset of CF Access claims for audit.
        claim_subset = {
            "sub": claims.get("sub"),
            "email": claims.get("email"),
            "iss": claims.get("iss"),
            "aud": claims.get("aud"),
            "iat": claims.get("iat"),
            "exp": claims.get("exp"),
        }

        # Persist provider_subject on first authentication (COALESCE keeps any
        # already-set value to prevent silent re-binding if the IdP rotates `sub`).
        sub = claims.get("sub")
        await conn.execute(
            """
            UPDATE user_identities
            SET last_authenticated_at = now(),
                claims = $2::jsonb,
                provider_subject = COALESCE(provider_subject, $3)
            WHERE id = $1
            """,
            row["identity_id"],
            json.dumps(claim_subset),
            sub,
        )

        await conn.execute(
            "UPDATE users SET last_seen_at = now() WHERE id = $1::uuid",
            row["id"],
        )

    return Principal(
        principal_type="user",
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        businesses=[dict(b) for b in businesses],
        permissions=_permissions(row["role"]),
        auth_method=auth_method,
    )


def _resolve_pepper(version: str | None) -> str | None:
    """Return the pepper that the given row was hashed with.

    Each service_accounts row records the pepper version in
    metadata.pepper_version. This function looks up THAT version, never
    a different one — silently mapping an empty pepper_version to the
    currently-active versioned pepper would let a rotation invalidate
    every legacy key.

    Mapping:
      version="V1" (or any non-empty)  -> SERVICE_KEY_PEPPER_<VERSION>
      version=None / empty             -> legacy SERVICE_KEY_PEPPER
                                          (NOT the currently active versioned one)

    Bootstrap (creating new keys) uses a separate path that picks the
    active pepper — see scripts/bootstrap_service_account.py.
    """
    if version:
        env_name = f"SERVICE_KEY_PEPPER_{version.upper()}"
        return os.environ.get(env_name)
    return os.environ.get("SERVICE_KEY_PEPPER")


_KEY_PATTERN = re.compile(
    r"^opsmem_(?P<env>live|test)_(?P<kid>[A-Za-z0-9]{16})_(?P<secret>[A-Za-z0-9]{43})$"
)


def _parse_service_key(raw: str) -> dict[str, str] | None:
    """Parse `opsmem_<env>_<kid>_<secret>` into components.

    Returns None on any malformed input. Refuses keys not matching the
    canonical shape — prevents legacy "first 16 chars of opaque blob"
    parsing that Codex flagged in the chunk1 review.
    """
    if not raw or len(raw) > 256:
        return None
    m = _KEY_PATTERN.match(raw)
    if not m:
        return None
    return {"env": m.group("env"), "kid": m.group("kid"), "secret": m.group("secret")}


async def _load_service(request: Request, raw_key: str) -> Principal:
    parsed = _parse_service_key(raw_key)
    if not parsed:
        log.info("service_key_malformed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service key",
        )

    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text AS id, name, key_hash, scopes,
                   COALESCE(metadata->>'pepper_version', '') AS pepper_version
            FROM service_accounts
            WHERE key_prefix = $1
              AND status = 'active'
              AND (expires_at IS NULL OR expires_at > now())
            """,
            parsed["kid"],
        )

        if not row:
            log.info(f"service_key_unknown_kid kid={parsed['kid'][:6]}...")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid service key",
            )

        # Resolve the pepper version this row was hashed with.
        pepper = _resolve_pepper(row["pepper_version"] or None)
        if not pepper:
            log.error(
                "service_pepper_missing",
                extra={"kid": parsed["kid"][:6], "pepper_version": row["pepper_version"]},
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="service auth unavailable",
            )

        # HMAC the full displayed key (env_prefix + kid + secret), so
        # rotating the secret half always changes the hash even if the
        # kid stays the same.
        key_hash = hmac.new(pepper.encode(), raw_key.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(row["key_hash"], key_hash):
            log.info(f"service_key_hash_mismatch kid={parsed['kid'][:6]}...")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid service key",
            )

        await conn.execute(
            "UPDATE service_accounts SET last_used_at = now() WHERE id = $1::uuid",
            row["id"],
        )

    scopes = list(row["scopes"] or [])
    return Principal(
        principal_type="service",
        id=row["id"],
        email=None,
        display_name=row["name"],
        role="service",
        businesses=[],
        permissions=_permissions("service", scopes),
        auth_method="service_key",
    )


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency. Returns the authenticated principal or raises 401/403."""
    auth_mode = os.environ.get("AUTH_MODE", "cloudflare")

    # Service-account key short-circuits user auth.
    service_key = request.headers.get("X-OpsMemory-Service-Key")
    if service_key:
        # In production, Cloudflare Access service tokens still gate the edge.
        # Verify the JWT (without requiring an email claim) to ensure the call
        # came through the tunnel, then validate the app-level service key.
        if auth_mode == "cloudflare":
            _verify_cf_jwt(request, require_email=False)
        return await _load_service(request, service_key)

    if auth_mode == "local":
        email = os.environ.get("LOCAL_DEV_EMAIL")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="LOCAL_DEV_EMAIL must be set when AUTH_MODE=local",
            )
        if os.environ.get("ALLOW_DEV_USER_SWITCH") == "true":
            email = request.headers.get("X-Dev-User-Email", email)
        return await _load_user(request, email, {"email": email}, "local_dev")

    claims = _verify_cf_jwt(request, require_email=True)
    return await _load_user(request, claims["email"], claims, "cloudflare_access")

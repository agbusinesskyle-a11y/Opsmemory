"""OpsMemory FastAPI entry point.

Wires:
- DB pool lifecycle (init on startup, close on shutdown)
- Production fail-closed guard (refuses to boot with AUTH_MODE=local in production)
- Request-ID + structured JSON logging middleware
- Security response headers (CSP, HSTS, X-Content-Type-Options, X-Frame-Options,
  Referrer-Policy, form-action via CSP)
- Health/whoami routes
- PWA static file serving (explicit routes — no catch-all mount that would shadow API)
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .db import close_pool, init_pool
from .health import router as health_router
from .logging_config import configure_logging
from .v1 import router as v1_router
from .v1_ingest import router as v1_ingest_router

# ---------------------------------------------------------------------------
# Logging — proper JSON formatter (escapes quotes/newlines correctly).
# ---------------------------------------------------------------------------
configure_logging()
log = logging.getLogger("opsmemory.main")

CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

# Origins allowed for browser write requests. Cloudflare Access already
# gates all traffic at the edge, but enforcing Origin at the app layer is
# defense in depth against any future cross-origin browser exploit.
# Service-account requests (X-OpsMemory-Service-Key) bypass this check —
# they aren't browsers and don't send Origin.
ALLOWED_ORIGINS = {"https://tracker.kyleconway.ai"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Inject request ID, log start/end, attach security headers, enforce Origin on writes."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id

        log.info(
            "req_start",
            extra={"request_id": request_id, "method": request.method, "path": request.url.path},
        )

        # Origin enforcement on browser write requests (Chunk 1.5 step 10).
        #
        # Two rules, both enforced:
        #   - If Origin header is PRESENT on a write, it MUST be in
        #     ALLOWED_ORIGINS. (Browser write — must come from our own UI.)
        #   - If Origin header is ABSENT on a write, the request MUST be
        #     a service-key call. (Non-browser, machine-to-machine.)
        # Mere presence of X-OpsMemory-Service-Key does NOT bypass an
        # invalid Origin — both checks apply if Origin is present, so a
        # leaked service key combined with a hijacked browser session
        # still can't post from elsewhere.
        if request.method in WRITE_METHODS:
            origin = request.headers.get("Origin")  # None vs "" matters
            has_service_key = bool(request.headers.get("X-OpsMemory-Service-Key"))
            if origin is not None:
                if origin not in ALLOWED_ORIGINS:
                    log.info(
                        "origin_rejected",
                        extra={"request_id": request_id, "origin": origin, "method": request.method},
                    )
                    response = JSONResponse(
                        status_code=403,
                        content={"error": "origin_rejected", "request_id": request_id},
                    )
                    response.headers["X-Request-ID"] = request_id
                    return response
            elif not has_service_key:
                log.info(
                    "origin_missing_no_service_key",
                    extra={"request_id": request_id, "method": request.method},
                )
                response = JSONResponse(
                    status_code=403,
                    content={"error": "origin_required", "request_id": request_id},
                )
                response.headers["X-Request-ID"] = request_id
                return response

        try:
            response = await call_next(request)
        except HTTPException:
            raise
        except Exception:
            log.exception("req_error", extra={"request_id": request_id})
            response = JSONResponse(
                status_code=500,
                content={"error": "internal_error", "request_id": request_id},
            )

        response.headers["X-Request-ID"] = request_id
        response.headers["Content-Security-Policy"] = CSP_HEADER
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        log.info(
            "req_end",
            extra={"request_id": request_id, "status": response.status_code},
        )
        return response


# ---------------------------------------------------------------------------
# Production fail-closed guard
# ---------------------------------------------------------------------------
def _enforce_production_safety() -> None:
    env = os.environ.get("ENVIRONMENT", "").lower()
    if env != "production":
        return
    auth_mode = os.environ.get("AUTH_MODE", "cloudflare").lower()
    allow_switch = os.environ.get("ALLOW_DEV_USER_SWITCH", "false").lower()
    problems: list[str] = []
    if auth_mode == "local":
        problems.append("AUTH_MODE=local is forbidden when ENVIRONMENT=production")
    if allow_switch == "true":
        problems.append("ALLOW_DEV_USER_SWITCH=true is forbidden when ENVIRONMENT=production")
    if problems:
        for p in problems:
            log.error("production_safety_violation", extra={"detail": p})
        raise RuntimeError("; ".join(problems))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    _enforce_production_safety()
    pool = await init_pool()
    app.state.db = pool
    log.info("opsmemory_started", extra={"version": os.environ.get("APP_VERSION", "chunk1")})
    try:
        yield
    finally:
        await close_pool()
        log.info("opsmemory_stopped")


app = FastAPI(
    title="OpsMemory API",
    version=os.environ.get("APP_VERSION", "chunk1"),
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(RequestContextMiddleware)
app.include_router(health_router)
app.include_router(v1_router)
app.include_router(v1_ingest_router)


# ---------------------------------------------------------------------------
# PWA static file serving — explicit routes, no catch-all mount.
# ---------------------------------------------------------------------------
WEB_ROOT = os.environ.get("WEB_ROOT", "/app/web")


def _safe_web_path(*parts: str) -> str:
    """Join WEB_ROOT with components, refusing path traversal."""
    full = os.path.realpath(os.path.join(WEB_ROOT, *parts))
    base = os.path.realpath(WEB_ROOT)
    if not full.startswith(base + os.sep) and full != base:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid path")
    return full


@app.get("/")
async def serve_index() -> FileResponse:
    path = _safe_web_path("index.html")
    if not os.path.isfile(path):
        return PlainTextResponse("OpsMemory shell not built", status_code=503)
    return FileResponse(path, media_type="text/html")


@app.get("/app.js")
async def serve_appjs() -> FileResponse:
    path = _safe_web_path("app.js")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="application/javascript")


@app.get("/manifest.json")
async def serve_manifest() -> FileResponse:
    path = _safe_web_path("manifest.json")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="application/manifest+json")


@app.get("/styles.css")
async def serve_styles() -> FileResponse:
    path = _safe_web_path("styles.css")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="text/css")


@app.get("/sw.js")
async def serve_sw() -> FileResponse:
    path = _safe_web_path("sw.js")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    response = FileResponse(path, media_type="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/icons/{filename}")
async def serve_icon(filename: str) -> FileResponse:
    if not filename or len(filename) > 64:
        raise HTTPException(status_code=400, detail="invalid filename")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if not all(c in allowed for c in filename):
        raise HTTPException(status_code=400, detail="invalid filename")
    if not (filename.endswith(".png") or filename.endswith(".ico")):
        raise HTTPException(status_code=400, detail="invalid extension")

    path = _safe_web_path("icons", filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)

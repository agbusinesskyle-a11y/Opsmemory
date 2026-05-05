"""OpsMemory FastAPI entry point.

Wires:
- DB pool lifecycle (init on startup, close on shutdown)
- Request-ID + structured logging middleware
- Security response headers (CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy)
- Health/whoami routes
- PWA static file serving (explicit routes — no catch-all mount that would shadow API)
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .db import close_pool, init_pool
from .health import router as health_router

# ---------------------------------------------------------------------------
# Logging — JSON-shaped lines on stdout. Docker handles rotation.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    stream=sys.stdout,
)
log = logging.getLogger("opsmemory.main")

CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)

SENSITIVE_HEADER_NAMES = {
    "cookie",
    "authorization",
    "cf-access-jwt-assertion",
    "x-opsmemory-service-key",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Inject request ID, log start/end, attach security headers."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id

        log.info(
            f"req_start id={request_id} method={request.method} path={request.url.path}"
        )

        try:
            response = await call_next(request)
        except HTTPException:
            raise
        except Exception:
            log.exception(f"req_error id={request_id}")
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

        log.info(f"req_end id={request_id} status={response.status_code}")
        return response


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await init_pool()
    app.state.db = pool
    log.info("opsmemory_started")
    try:
        yield
    finally:
        await close_pool()
        log.info("opsmemory_stopped")


app = FastAPI(
    title="OpsMemory API",
    version=os.environ.get("APP_VERSION", "chunk1"),
    lifespan=lifespan,
    docs_url=None,  # No /docs in production. Available in chunk-2+ behind admin auth.
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(RequestContextMiddleware)
app.include_router(health_router)


# ---------------------------------------------------------------------------
# PWA static file serving — explicit routes, no catch-all mount.
# Catch-all mount at "/" would shadow API routes; explicit routes won't.
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


@app.get("/sw.js")
async def serve_sw() -> FileResponse:
    path = _safe_web_path("sw.js")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="not found")
    response = FileResponse(path, media_type="application/javascript")
    # Allow the SW to control the entire origin from any path.
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/icons/{filename}")
async def serve_icon(filename: str) -> FileResponse:
    # Whitelist: alnum + hyphen + underscore + dot, ending in .png or .ico.
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

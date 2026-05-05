# OpsMemory API — Chunk 1
#
# Single-stage Python 3.12 slim. Non-root runtime user.
# uvicorn binds 0.0.0.0:8000 inside container; compose maps 127.0.0.1:8010 on host.

FROM python:3.12-slim

# System deps: libpq5 for asyncpg's optional binary path, ca-certs, curl for healthcheck debug.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 \
      ca-certificates \
      curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN groupadd --system --gid 1000 opsmemory \
 && useradd  --system --gid opsmemory --uid 1000 --create-home --home-dir /app opsmemory

WORKDIR /app

# Python deps as root.
COPY api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# App code.
COPY --chown=opsmemory:opsmemory api /app/api
COPY --chown=opsmemory:opsmemory web /app/web

USER opsmemory

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEB_ROOT=/app/web

EXPOSE 8000

# uvicorn with proxy-headers since cloudflared is the trusted proxy.
CMD ["uvicorn", "api.app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--log-config", "/app/api/log_config.json"]

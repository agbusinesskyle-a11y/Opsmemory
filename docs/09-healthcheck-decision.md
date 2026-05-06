# Docker healthcheck — keep `/healthz`, not `/readyz`

Codex flagged this in the Chunk 1.5 review:

> On healthcheck: switching Compose from `/healthz` to `/readyz` in
> docker-compose.yml is fine only if you understand Docker Compose
> won't automatically restart on unhealthy. Do not create restart loops
> because a backup is stale.

## Decision

Keep the Docker `healthcheck.test` pointed at `/healthz`. Do NOT switch
to `/readyz`.

## Why

`/healthz` and `/readyz` answer two different questions:

- **`/healthz` (liveness)** — "Is the process alive and able to handle
  requests at all?" If this fails, Docker should restart the container.
  Things that fail liveness: process crash, panic, OOM, deadlock.
  Cheap to compute (no DB or filesystem reads).

- **`/readyz` (readiness)** — "Is the system ready to serve work as
  intended?" Includes DB connectivity, migration applied, backup
  freshness, restore-check freshness. Failure means upstream load
  balancers should stop sending traffic — but the process itself is
  fine.

Docker's `healthcheck` doesn't distinguish. If we point it at `/readyz`
and the daily backup runs late by 36+ hours, `/readyz` returns 503
(`backup_stale`). Docker marks the container `unhealthy`. Compose
doesn't auto-restart on unhealthy by default — it just labels — but
some operators enable `restart: on-failure` or use
`autoheal`-style sidecars. With those: container restart loops over a
problem the restart can't fix (a stuck backup timer). The DB connection
gets churned. The error gets noisier.

`/healthz` answers the question Docker actually cares about: is it
alive. Operator monitoring (manual curl, Cloudflare uptime, Slack
alerts) should hit `/readyz` to know if the system is FUNCTIONAL —
that's where backup-stale and restore-stale matter.

## What's actually wired up

- `docker-compose.yml`: `healthcheck.test` → `urllib.request.urlopen('http://127.0.0.1:8000/healthz')` → keeps the container "healthy" as long as the FastAPI process can answer.
- `cloudflared` ingress: routes external traffic to the container regardless of "healthy" status. Cloudflare Access still enforces auth.
- `/readyz` is checked by `validate_env.py` ExecStartPre on the backup timer (after Chunk 1.5 step 3) and by manual operator monitoring.

## When to revisit

If we add Cloudflare Load Balancer with active-passive between Spark #1
and Spark #2 (per the original dual-Spark deployment plan), the LB's
health probe should hit `/readyz` to take a stale-backup Spark out of
rotation. That's a future-Chunk concern.

For now: `/healthz` for the Docker probe, `/readyz` for human eyes and
external monitors.

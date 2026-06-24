# deploy - production runtime wiring

## Purpose

Bootstrap, environment, nginx, and systemd assets for running system-core in
production: legacy Flask workers plus FastAPI Grid API behind nginx.

## Ownership

- `bootstrap.sh` - fresh VM bootstrap: packages, repo, venv, Postgres, Redis,
  secrets, systemd, nginx.
- `env.template` - `/etc/aipg/grid.env` source of production env names.
- `README.md` - deploy/cutover/runbook notes.
- `nginx/aipg-api.conf` - public route split between `/v1`, `/api/v2`, `/v2`,
  metrics, and legacy site routes.
- `systemd/aipg-gridapi.service` - uvicorn Grid API unit.
- `systemd/aipg-horde@.service` - legacy Flask unit template.

## Local Contracts

- Env names in `env.template`, systemd, code, and docs must match exactly.
- Public route split is intentional:
  - `/v1/*` -> Grid API.
  - `/api/v2/*` and `/v2/*` -> legacy Flask compatibility.
  - `/metrics` should remain restricted by nginx.
- Secrets belong in `/etc/aipg/grid.env` with restrictive permissions, never in
  git, command argv, or logs.
- Deployment scripts may be destructive on fresh VMs. Do not run them locally
  from an agent without explicit user approval.

## Work Guidance

- When adding services, document ports, health checks, restart behavior, and
  firewall/nginx impact.
- Keep `GRID_SALT` shared across Flask, Grid API, and dashboard; API keys break
  if salts diverge.
- If you rename Base/contract env vars, update `docs/`, `grid_api/services/*`,
  and any SDK examples in the same change.

## Verification

- `nginx -t` on target host after nginx changes.
- `systemd-analyze verify` on target host when changing units.
- Local docs-only safety: `git diff --check`.

## Child DOX Index

- None - leaf.

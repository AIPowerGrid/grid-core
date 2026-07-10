# deploy - production runtime wiring

## Purpose

Existing-host operations plus inherited bootstrap, nginx, and systemd assets for
grid-core (deployed from the historical `/home/aipg/system-core` path).

## Ownership

- `bootstrap.sh` - **quarantined legacy fresh-VM bootstrap**. It still targets an
  obsolete repo/branch and mixed Flask topology; do not run until rewritten and
  reviewed.
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
- Keep `GRID_SALT` shared only by server processes that validate the same legacy
  hashes. The developer console has no local DB/salt path and must not receive it.
- If you rename Base/contract env vars, update `docs/`, `grid_api/services/*`,
  and any SDK examples in the same change.

## Verification

- `nginx -t` on target host after nginx changes.
- `systemd-analyze verify` on target host when changing units.
- Local docs-only safety: `git diff --check`.

## Child DOX Index

- None - leaf.

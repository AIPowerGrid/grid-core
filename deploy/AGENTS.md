# deploy - production runtime wiring

## Purpose

Fresh-host and existing-host operations for the Grid-native core. Production
executes an immutable release selected through `/home/aipg/current`.

## Ownership

- `bootstrap.sh` - fresh-host bootstrap pinned to an operator-supplied full
  commit SHA. Installs only the Grid API, PostgreSQL, Redis, and Nginx.
- `env.template` - `/etc/aipg/grid.env` source of production env names.
- `README.md` - deploy/cutover/runbook notes.
- `DEMAND_BILLING_RUNBOOK.md` - dark deploy, allowlisted canary, alert,
  rollback, and staged demand-charging procedure.
- `VALIDATOR_PAID_AUDIT_RUNBOOK.md` - default-off migration, scoped-budget,
  supervised worker-compensation canary, recovery, and rollback procedure.
- `nginx/aipg-api.conf` - Grid routes, restricted metrics, public docs/health,
  and static `410 Gone` responses for retired API paths.
- `systemd/aipg-gridapi.service` - uvicorn Grid API unit.
- `systemd/aipg-payout.{service,timer}` - custodial payout one-shot and hourly
  scheduler. The service invokes the wrapper from the selected release.
- `systemd/aipg-postgres-backup.{service,timer}` - hardened root-only daily
  backup scheduler; existing hosts enable it only after a supervised restore
  proof.

## Local Contracts

- Env names in `env.template`, systemd, code, and docs must match exactly.
- Public route split is intentional:
  - `/v1/*`, `/`, `/health`, `/docs`, and `/openapi.json` -> Grid API.
  - `/api/v2/*` and `/v2/*` -> static `410 Gone`; no legacy process.
  - `/metrics` should remain restricted by nginx.
- Existing-host deployments install the versioned Nginx site from the selected
  release, run `nginx -t`, and reload Nginx. Do not let the live site drift from
  `nginx/aipg-api.conf`.
- `/health` and `/v1/status/network` must report the reviewed immutable commit;
  `GRID_BUILD_COMMIT`, when set, must be the full 40-character release SHA.
- Secrets belong in `/etc/aipg/grid.env` with restrictive permissions, never in
  git, command argv, or logs.
- Host and image builds install only binary wheels from
  `requirements-grid.lock` with `--require-hashes`; production must never
  resolve floating source requirements, build an sdist, or upgrade pip during
  a release.
- Deployment scripts may be destructive on fresh VMs. Do not run them locally
  from an agent without explicit user approval.

## Work Guidance

- When adding services, document ports, health checks, restart behavior, and
  firewall/nginx impact.
- `GRID_SALT` stays server-side. The developer console has no local DB/salt path
  and must not receive it.
- `GRID_SIWE_ALLOWED_DOMAINS` is the exact frontend authority allowlist for
  wallet-login challenges. Keep `GRID_LEGACY_SIWE_VERIFY_ENABLED=0`; it is an
  emergency client-migration switch, not a permanent compatibility mode.
- Validator probe leases must exceed the worker-response timeout and keep a
  small bounded retry budget. Do not make targeted validation an unlimited
  free-inference path.
- `VALIDATOR_TEXT_GROUP_MIN_INTERVAL_SECONDS` limits creation of real text
  workloads per worker/model (default one hour; Core enforces a five-minute
  floor). `VALIDATOR_HISTORY_RETENTION_DAYS` and
  `VALIDATOR_HISTORY_SWEEP_SECONDS` bound finalized assignment/group machinery;
  signed attestations are preserved. Keep `env.template`, `config.py`, and the
  validator runbook aligned when changing these controls.
- Keep `VALIDATOR_SEALED_ASSIGNMENTS_ENABLED=0` through the compatible-node
  rollout. Merge and upgrade the validator fleet first, deploy the compatible
  Core second, verify old unsealed operation, then enable the flag in a
  supervised evidence-only canary. Roll back the flag, not the database, if a
  node cannot verify terminal disclosure. This flag never enables routing,
  rewards, strikes, or slashing.
- Apply Alembic `0024` before code that issues media assignments; it adds the
  group execution lease and shared frozen-witness columns read by that path.
- Apply Alembic `0026` before any code that reads assignment compensation or
  enables paid text audits. Keep `VALIDATOR_PAID_AUDIT_ENABLED=0` unless the
  reviewed wallet cohort, positive global daily/hourly, per-validator daily,
  per-worker daily, and per-job den caps, Postgres migration, stale-hold
  sweeper, replay path, and supervised settlement/ACK canary are all proven.
- `VALIDATOR_MEDIA_PROBE_ENABLED` is not a standalone launch switch. Keep it off
  until the reviewed bond contract/verifier/minimum, finalized reference sync,
  governed deterministic recipe/model digest, independent operators, immutable
  R2 witness retention, and supervised preview gates are all proven.
- Do not enable the backup timer merely because its unit was installed. Run one
  backup, restore it into the generated scratch database, migrate with the exact
  candidate release, and inspect the proof first.
- If you rename Base/contract env vars, update `docs/`, `grid_api/services/*`,
  and any SDK examples in the same change.

## Verification

- `nginx -t` on target host after nginx changes.
- `systemd-analyze verify` on target host when changing units.
- `systemctl start aipg-postgres-backup.service` followed by
  `scripts/prove_postgres_restore.sh` on the target host before enabling its
  timer.
- Local docs-only safety: `git diff --check`.

## Child DOX Index

- None - leaf.

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
- `VALIDATOR_COHORT_RUNBOOK.md` - privacy-safe intake, opaque common-control
  review, public per-node status verification, 72-hour qualification,
  verification, expiry, and incident handling for independent preview
  operators.
- `nginx/aipg-api.conf` - Grid routes, exact OAuth metadata routes, optional
  reviewed exact-route overlays, restricted metrics, public docs/health, and
  static `410 Gone` responses for retired API paths.
- `systemd/aipg-gridapi.service` - uvicorn Grid API unit.
- `systemd/aipg-payout.{service,timer}` - custodial payout one-shot and hourly
  scheduler. The service invokes the wrapper from the selected release.
- `systemd/aipg-postgres-backup.{service,timer}` - hardened root-only daily
  backup scheduler; existing hosts enable it only after a supervised restore
  proof.

## Local Contracts

- Env names in `env.template`, systemd, code, and docs must match exactly.
- Promotional spending requires both the global emergency gate and a non-empty
  exact `GRID_PROMO_SPENDABLE_CAMPAIGNS` allowlist. Never use or emulate a
  wildcard; enable reviewed builder cohorts independently of welcome grants.
- Public route split is intentional:
  - `/v1/*`, `/`, `/health`, `/docs`, and `/openapi.json` -> Grid API.
  - The two exact OAuth `/.well-known/*` metadata routes -> Grid API; all other
    well-known paths remain under the static fallback.
  - `/etc/nginx/aipg-api.d/*.conf` may add reviewed exact locations such as
    `/v1/mcp`; no overlay may add a broad prefix proxy.
  - `/v1/oauth/introspect` returns an exact public `404`; the co-located MCP
    process reaches it only through loopback Uvicorn transport.
  - `/api/v2/*` and `/v2/*` -> static `410 Gone`; no legacy process.
  - `/metrics` should remain restricted by nginx.
- Existing-host deployments install the versioned Nginx site from the selected
  release, preserve reviewed files in `/etc/nginx/aipg-api.d`, run `nginx -t`,
  and reload Nginx. Do not let the base live site drift from
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
- `VALIDATOR_PAIRING_ENABLED` is a separate default-off account-visibility gate.
  Apply Alembic `0030` before enablement and ship the matching Console and local
  node-app consent flows first. The audience and approval URL must be explicit
  HTTPS values. Rollback disables the flag and preserves node identities and
  association tables. This gate never activates validator economics.
  A supervised pilot may keep that global flag off and set
  `VALIDATOR_PAIRING_CANARY_ACCOUNTS` (JSON array, maximum ten canonical UUIDs)
  plus `VALIDATOR_PAIRING_CANARY_UNTIL` (timezone-aware ISO deadline, at most
  24 hours ahead at startup). Include both the test node and test human accounts.
  Public capability advertisement remains false. Remove test links before
  expiry, then clear the allowlist; an expired pilot retains links but cannot
  read or remove them until deliberately reauthorized. Keep IDs private. Full
  rollback clears the pilot as well as disabling the global flag.
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
- `OAUTH_AUTHORIZATION_RETENTION_SECONDS`,
  `OAUTH_UNUSED_CLIENT_RETENTION_SECONDS`, and `OAUTH_STATE_SWEEP_SECONDS`
  bound unauthenticated OAuth operational storage. Keep at least one hour of
  retention and do not disable cleanup during an OAuth rollback.
- Keep `VALIDATOR_SEALED_ASSIGNMENTS_ENABLED=0` through the compatible-node
  rollout. Merge and upgrade the validator fleet first, deploy the compatible
  Core second, verify old unsealed operation, then enable the flag in a
  supervised evidence-only canary. Roll back the flag, not the database, if a
  node cannot verify terminal disclosure. This flag never enables routing,
  rewards, strikes, or slashing.
- Apply Alembic `0024` before code that issues media assignments; it adds the
  group execution lease and shared frozen-witness columns read by that path.
- Apply Alembic `0027` before starting the bond-sync loop or selecting bonded
  references. It adds the finalized block/facet proofs and durable sync cursor;
  deploying the code first will fail and must not be attempted.
- Apply Alembic `0028` before reviewing worker common-control groups or selecting
  media candidates/references under the independent-control policy. It adds the
  private identity-bound review table and intentionally backfills no trust;
  verify the table remains empty after a dark deployment.
- Apply Alembic `0029` before starting any release with compensated-audit
  terminal integration, even while scheduling is off: every ordinary terminal
  checks the private audit table. Deploying code first would break ordinary
  settlement. A dark migration must leave both audit tables empty and create no
  counters, jobs, ledger rows, or worker acknowledgements. Schema presence is
  not permission to enable scheduling. No scheduler or runtime flag can create
  an audit hold yet; explicit default-off configuration and scheduler review
  remain separate gates.
- `VALIDATOR_MEDIA_PROBE_ENABLED` is not a standalone launch switch. Keep it off
  until the reviewed bond contract/verifier/minimum, finalized reference sync,
  governed deterministic recipe/model digest, independent operators, immutable
  R2 witness retention, and supervised preview gates are all proven.
- `VALIDATOR_MEDIA_BOND_SYNC_ENABLED` is independently dark. Its Diamond
  address, reviewed verifier version, primary Base RPC, independently operated
  confirmation RPC, bounds, and interval must be set together. The exact facet
  runtime is compiled into the Core release and must never be supplied through
  operator configuration. The
  cache refreshes only when both RPCs return the same complete finalized
  snapshot and prior finalized anchor. A fault immediately invalidates that
  authority's cached eligibility. Enabling the loop does not enable media assignments or
  create/activate a reference row.
- Do not enable the backup timer merely because its unit was installed. Run one
  backup, restore it into the generated scratch database, migrate with the exact
  candidate release, and inspect the proof first.
- The backup one-shot stays UID 0 for its protected state directory and restore
  tooling, but its primary group must be `aipg`. Its empty capability set
  removes DAC bypass, while immutable releases live below `0750 aipg:aipg`
  directories; changing the group back to `root` makes the unit unable to
  execute its own versioned script.
- If you rename Base/contract env vars, update `docs/`, `grid_api/services/*`,
  and any SDK examples in the same change.

## Verification

- `nginx -t` on target host after nginx changes.
- `systemd-analyze verify` on target host when changing units.
- `systemctl start aipg-postgres-backup.service` followed by
  `scripts/prove_postgres_restore.sh` on the target host before enabling its
  timer.
- `grep -qx 'Group=aipg' deploy/systemd/aipg-postgres-backup.service`.
- Local docs-only safety: `git diff --check`.

## Child DOX Index

- None - leaf.

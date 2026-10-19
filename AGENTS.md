# grid-core (deployed as system-core) - DOX root

## Purpose

AI Power Grid core runtime: FastAPI coordinator, worker dispatch, prepaid demand
billing, worker den ledger, Base-chain settlement hooks, deployment assets, and
integration SDKs.

This repository uses the DOX AGENTS.md hierarchy. Before editing, read this file,
then walk from the repository root to every target path and read each nested
AGENTS.md on that route. The nearest AGENTS.md is the local contract; parent docs
remain binding for broader rules. After meaningful changes, update the closest
owning AGENTS.md and any affected parent Child DOX Index.

## Ownership

- `grid_api/` - live v2 FastAPI Grid coordinator for `/v1`, worker WebSockets,
  credits, ledger, den, settlement, recipes, and chain sync.
- `horde/` - frozen historical Flask source retained for license and migration
  archaeology. Grid code must not import it; production does not install or run
  it. `/api/v2` retirement responses are static Nginx responses.
- `alembic/` - grid-owned database migrations. Must match `grid_api/v2/schema.py`.
- `deploy/`, `docker/`, root Docker/systemd scripts - production and local
  runtime wiring. Production selects immutable releases through
  `/home/aipg/current`; the historical mutable checkout is not deployable.
- `docs/` - architecture, economics, blockchain, and migration specs.
- `core-integration-package/` - Base contract ABIs, sample contracts, and JS SDKs
  for ModelRegistry, RecipeVault, and JobAnchor.
- `recipes/`, `styles/` - curated JSON data loaded by `grid_api.services.recipes`
  and `grid_api.services.styles`.
- `sdk/modelvault-worker-sdk/` - TypeScript SDK package for worker/model vault
  integration.
- `sql_statements/` - frozen historical SQL; not run by Grid bootstrap.
- `tests/` - inherited integration tests; `grid_api/**/tests/` owns current unit
  and router tests.
- Root-owned files include `server.py`, `server_grid_api.py`, `requirements*.txt`,
  `requirements-grid.lock`, `pyproject.toml`, `Dockerfile`,
  `docker-compose.yaml`, root READMEs, assets, and one-off utility scripts.
- `.github/workflows/secret-scan.yml`, `.gitleaks.toml`, and
  `.gitleaksignore` enforce checksum-verified tracked-tree and full reachable
  history scanning on pushes and pull requests. Historical exceptions are
  exact reviewed fingerprints, never broad path/rule exclusions; CI also proves
  that an example-labelled synthetic private key is rejected.

## Local Contracts

- Keep hot inference off-chain. Use Base for consensus-critical registry,
  staking/bonding, signed receipt roots, settlement, and audit anchors; never add
  per-request chain calls in a user request path.
- Production runs only the FastAPI `/v1` coordinator (`grid_api`); workers connect
  at `/v1/workers/ws`. Do not restore legacy key lookup, table writes, Flask
  processes, or polling routes.
- Money paths must be fail-closed in live mode, idempotent by durable refs, and
  covered by tests. Explicit `GRID_CHARGING_MODE` takes precedence over the
  legacy `GRID_CHARGING_ENABLED` fallback. The legacy flag being `0` does not
  prove dry-run when a mode is configured; verify the selected request cohort
  and actual running configuration.
- The x402 minimum candidate requires migration `0046` before startup, even
  dark. New successful x402 requests alone use the owner-approved disclosed
  USD 0.001 minimum, snapshotted before dispatch. Old receipts keep zero minimum;
  ordinary prepaid tariffs and failed jobs are unchanged. Keep public admission
  off until exact-source CI, production restore/migration and a fresh capped
  real-worker payment/replay proof pass. Retain pricing-aware terminals on rollback.
- Production Python dependencies resolve from `requirements-grid.txt` into the
  reviewed, hash-locked Python 3.12/Linux `requirements-grid.lock`. Docker and
  host deploys consume only matching binary wheels from the lock and do not
  upgrade pip during release construction; dependency updates must regenerate
  it and pass the CI lock-drift and `pip-audit` gates.
- Retain the reviewed PyJWT 2.15.1, aiohttp 3.14.4, multidict 6.9.1 and urllib3 2.8.0
  security floors in both dependency declarations. Updating these libraries
  still requires authentication/funding/worker regression tests; never waive
  audit findings to unblock a feature release.
  October19's aiohttp advisories supersede the earlier3.14.3 clearance.
  Core uses FastAPI/uvicorn, not an aiohttp HTTP server; dependency presence
  alone does not establish exposure to the server-specific denial-of-service
  prerequisites. Upgrade the transitive client dependency rather than waive
  the audit or silently alter production locks in place.
- Treat `grid_ledger` and `grid_credit_ledger` as append-only economic records.
  Do not mutate historical ledger rows except via an explicit audited migration.
- Worker/model claims are untrusted until verified by registry sync, signed
  receipts, validators, or settlement review. Do not make payout/slashing logic
  depend only on worker self-report.
- Generated caches, venvs, pyc files, zip/tar artifacts, and screenshots are not
  source contracts. Do not extend docs from generated output unless the source is
  unavailable and that fact is documented.
- Preserve SPDX/license headers where they already exist.

## Work Guidance

- Prefer `rg` / `rg --files` for exploration.
- Use the existing stack: Python/FastAPI/SQLAlchemy/Redis for core, Foundry
  or JS SDKs only inside their owning contract/SDK areas.
- Keep env names synchronized across code, `deploy/env.template`, systemd,
  docs, and examples.
- For any endpoint change, update the owning router/API doc and add a contract
  test where practical.
- For any schema change, update `grid_api/v2/schema.py`, Alembic migrations,
  affected services, and tests together.
- For any Base-chain change, update the relevant contract/ABI/SDK docs and keep
  hot-path code on cached/offline reads.
- If you add or remove a durable folder boundary, create/remove/update its
  AGENTS.md and refresh this Child DOX Index.

## Verification

- `Media codec runtime` is a separate candidate/provenance workflow for the
  pinned static upload codecs. Its Linux source-decoder controls and manifest
  checks do not replace ordinary PostgreSQL/release checks or qualify the
  Gallery host. Read `deploy/MEDIA_CODEC_RUNTIME.md` before selecting tools.

- Credit-first release CI uses reviewed media-worker
  `6b80591350ade4da9f7e052aa2d6dec2810ce560`. All four actual appearance
  adapter, two NVFP4 startup and two625-frame source cases must pass without
  skips; the complete suite uses that same
  source. Synthetic HTTP does not establish a funded GPU/storage canary.
  Long BF16 sources require `recipe-media-sources-625-v1` on one worker,
  never the union of short-source workers. Native qualification and matched
  consumer rollout remain separate from CI protocol compatibility.

- Full Python sanity: `pytest` from `system-core`.
- Dependency gate:
  `pip-audit -r requirements-grid.lock --require-hashes --no-deps --disable-pip`;
  investigate every skipped non-PyPI dependency separately.
- Grid-focused: `pytest grid_api/`.
- Deployment candidates must pass the PostgreSQL 16 backup/restore proof in
  pull-request CI; this is part of the required `tests` context, not a
  post-merge-only check.
- PR/main operations checks exclude the unused Google Chrome apt source on
  their disposable runners before installing PostgreSQL client and ShellCheck.
  Keep package integrity checks enabled; this must not alter production hosts.
- Pull-request and `main` CI must inject a disposable
  `VALIDATOR_SHADOW_TEST_DB_URL` into the full Grid suite so the shadow-run
  idempotency, conflicting-write, single-run, and live eligibility tests execute
  on PostgreSQL 16 instead of reporting skipped coverage.
- PR and main CI also start an isolated Unix-socket Redis for the shadow-outbox
  reclaim tests. Rejected-event acknowledgement must wait for durable observer
  error recording; Redis retries and SQL/report behavior have separate proofs.
- PR and main CI install FFmpeg and require both `ffmpeg` and `ffprobe` before
  the dedicated media-source decoder tests. Missing executables must fail the
  job, not silently skip MP4 validation. Record decoder versions in the log;
  this gate does not qualify production packaging or an OS decoder sandbox.
- Service units: `pytest grid_api/services/`.
- Required PR/main tests additionally build pinned, reviewed validator/Console
  commits and run the real PostgreSQL/Core/Console/node compensation handoff.
  Cross-repo source pins are an explicit compatibility baseline; update them
  with reviewed client changes, never silently follow moving branches.
- PR/main CI also pins both video SDK candidates and runs their optional
  generated/audio/video qualification on SQLite and PostgreSQL 16. JavaScript
  uses actual localhost HTTP to the isolated Core router, not mocked fetch;
  scoped fixture keys, synthetic media and test balances never reach production.
  One POST, GET-only recovery after reply loss/rollback and 401/403/404 isolation
  are required. This does not prove GPU output, funded storage or video x402.
  Python fixture pin `840df33c71beb69008ffc9951cda740f0a3076ee` places
  Ingredients-only qualification in an explicit expiring synthetic pilot;
  ordinary BF16 fixture cases retain public admission. The runtime SDK is
  unchanged from the previous pin; never widen production admission to fix a
  synthetic private-profile test.
- Router billing/settlement coverage: `pytest grid_api/routers/`.
- Legacy smoke tests live under `tests/` and may skip without external services.
- For docs-only changes, run at least `git diff --check` and inspect the DOX
  chain for stale indexes.
- Secret gate: `gitleaks detect --source . --no-git --config .gitleaks.toml
  --redact` followed by `gitleaks git . --log-opts=HEAD --config
  .gitleaks.toml --redact` from a full clone.

## Child DOX Index

- [alembic/AGENTS.md](alembic/AGENTS.md) - grid-owned DB migrations.
- [core-integration-package/AGENTS.md](core-integration-package/AGENTS.md) - Base
  contract ABIs, sample contracts, and JS SDKs.
- [deploy/AGENTS.md](deploy/AGENTS.md) - production systemd/nginx/bootstrap env.
- [docs/AGENTS.md](docs/AGENTS.md) - architecture and migration documentation.
- [grid_api/AGENTS.md](grid_api/AGENTS.md) - live FastAPI Grid coordinator.
- [horde/AGENTS.md](horde/AGENTS.md) - legacy Flask/Horde API and compatibility
  runtime.
- [recipes/AGENTS.md](recipes/AGENTS.md) - curated ComfyUI recipe JSON.
- [scripts/AGENTS.md](scripts/AGENTS.md) - payout timer entrypoint, monitoring,
  and one-off operator/developer scripts.
- [sdk/modelvault-worker-sdk/AGENTS.md](sdk/modelvault-worker-sdk/AGENTS.md) -
  TypeScript ModelVault worker SDK package.
- [sql_statements/AGENTS.md](sql_statements/AGENTS.md) - legacy SQL migrations,
  cron, and stored procedures.
- [styles/AGENTS.md](styles/AGENTS.md) - curated style preset JSON.
- [tests/AGENTS.md](tests/AGENTS.md) - legacy integration smoke tests.

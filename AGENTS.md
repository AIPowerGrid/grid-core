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
- Candidate `0048` shared receipt ownership is mandatory before updated deposit
  or text-x402 reconciliation writers run. Drain/quiesce those writers, migrate,
  and upgrade every replica/offline runner together; old writers do not consult
  the new claims. Retain claim-aware writers/history on rollback after use.
- Production Python dependencies resolve from `requirements-grid.txt` into the
  reviewed, hash-locked Python 3.12/Linux `requirements-grid.lock`. Docker and
  host deploys consume only matching binary wheels from the lock and do not
  upgrade pip during release construction; dependency updates must regenerate
  it and pass the CI lock-drift and `pip-audit` gates.
- Retain the reviewed PyJWT 2.15.1, multidict 6.9.1 and urllib3 2.8.0
  security floors in both dependency declarations. Updating these libraries
  still requires authentication/funding/worker regression tests; never waive
  audit findings to unblock a feature release.
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

- New private binding-v3 video quotes commit upfront/EIP-3009 terms. Fresh
  signatures and both SDK callbacks require explicit upfront acceptance;
  existing v1/v2 identities retain recovery and v2 historical proof, never
  retrofit or another send. The focused quote/signature/envelope/proof/journal/
  prepaid/refund matrix passes 659 with ten explicit SQLite skips and real
  PostgreSQL counterparts. Cross-SDK isolated HTTP/SQL/signature/consent checks
  pass 68. New SDK pins are local-only until authorized publication; exact-
  revision Linux/PostgreSQL 16 CI and selected-facilitator flow handling remain
  required. No sender, public activation or production configuration changes.
  Full local Python 3.13/PostgreSQL 14 regression passes 3,767 with 43 explicit
  skips and 139 inherited warnings. This is not exact-source Linux CI or a
  live token/facilitator/GPU payment canary.
  A test-only follow-up executes the strict header's original v3/upfront
  capability in the copied-contract fork. Altered flow produces no attempt/
  transfer; exact payment, recovery, refund and both replay reverts pass.
  The combined matrix passes 225 with one SQLite skip; unchanged-code repeat
  passes 11 with the same skip. The full-suite result above is the prior
  runtime revision, not a rerun of this test-only extension or remote CI.

- Prior implicit-flow v2 envelope qualification used the locked SDK's real
  header encoder and verify/settle request shapes, plus actual offline ECDSA
  on both Base domains. The envelope-only matrix passes 136 cases; combined
  envelope/signature/journal coverage passes 281 with three explicit SQLite
  skips and real PostgreSQL counterparts. Headers/types/requirements/resource
  ambiguity, mutation, expiry and redacted failures reject. No route, sender,
  SQL, funding or dispatch authority is introduced. The finalized full local
  suite passes 3,739 with 43 explicit skips; its 141 warnings include the 139
  inherited warnings plus two JUnit/optional-fork compatibility warnings.
  Exact-source Linux/PostgreSQL 16 CI remains required for the new candidate.

- Private `0052` adds reviewed, unsigned Safe refund intents and atomic
  confirmed-refund receipt closure. Nonces are unique by chain/Safe, never
  automatically replaced; outgoing receipts use the existing `video_x402`
  `refund:<quote UUID>` ownership namespace. Preparation is not sending and
  `refund_due_at` remains historical after repayment; do not erase it to restore
  worker backing. No signer, route, loop, price or public flag is enabled.
  Apply before private refund tooling; retain every intent/proof on rollback.
  Actual live Safe/token/payment/refund and client qualification remain release gates.
  The focused money matrix passes 374 with 11 explicit skips; the full local
  Python 3.13/PostgreSQL 14 Grid suite passes 3,585 with 41 skips and 139 inherited
  warnings. Scratch genesis-through-0052/schema parity passes. Those historical
  chain/Safe fixtures masked incorrect non-indexed ExecutionSuccess encoding.
  The correction requires the exact indexed transaction-hash topic and zero-
  payment data. An opt-in local Anvil/Base fork executes copied Safe/USDC
  contracts and proves authorization, lost-hash recovery, failed-job refund,
  both replay reverts and zero credit/reward movement on scratch PostgreSQL.
  No production keys, live transfers or real funds are used; two logical
  adapters share one fork, not independent RPCs. The pure/fork matrix passes
  79 with one SQLite skip. The corrected full local suite passes 3,603 with
  43 explicit skips and 139 inherited warnings; separate Redis recovery
  passes two cases. Exact-revision Linux/PostgreSQL 16 CI remains required.

- Candidate `0051` privately commits a proved/owned exact-USDC video payment,
  its external reservation, frozen job and original input authority together.
  Closed/expired/capacity-rejected admission records only an unsent refund
  obligation. Existing terminal, stale release, outbox and reward selectors
  recognize `video_prepaid`; no credits are drawn/restored. Migrate before all
  financial/backing readers and retain these handlers/history on rollback.
  HTTP sender, automatic watcher scheduling, actual refunds and live
  testnet/GPU/storage remain gates. A separate non-routable recovery helper can
  correlate a lost transaction through a bounded confirmed nonce-log search;
  it proves the entire original transfer before recording its transaction.
  Empty searches never prove absence or permit another send.
  The recovery candidate's full local Python 3.13/PostgreSQL 14 Grid suite
  passes 3,479 with 39 skips and 139 inherited warnings. Its combined focused
  recovery/receipt/journal/shared-claim/prepaid matrix passes 437 with five
  explicit SQLite skips. These use synthetic chain evidence, not real funds;
  fresh exact-commit Linux/Python 3.12/PostgreSQL 16 CI remains required.
  The focused matrix passes 674 cases; the full local suite passes 3,396 with
  38 skips. Ten final test-only terminal-commit/affinity cases additionally pass
  within the 81-case prepaid matrix. Fresh exact-commit CI remains required.

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
- The same pinned SDK gate additionally qualifies generated/audio/video quote
  preparation and wallet callback forwarding on both canonical Base domains.
  Actual preview quotes must block callbacks; a test-only future-enabled copy
  uses new unfunded signatures and Core's verifier through private two-phase
  IPC. Require zero credit/hold/job effects. This is not a wallet UI, live token/
  account/facilitator, replay exclusion, received-payment or refund proof.
- The candidate video receipt verifier additionally uses two exact HTTPS RPC
  destinations, bounded read-only replies, actual ECDSA/official-SDK ABI checks,
  exact USDC authorization/Transfer events and pinned block state. Fixture
  tests are not live RPC, facilitator or purchased-payment qualification. It
  has no runtime hook or economic authority; the separate journal consumes its
  proof without making the pure verifier a payment writer.
- Private migration `0047` and the non-routable video attempt journal persist
  one pre-send winner and immutable transaction/proof recovery per quote. No
  spending signature is retained. SQLite/real PostgreSQL cover reply loss,
  rollback, duplicate races and lock-delayed expiry. Candidate `0048` adds
  shared receipt ownership for new proven evidence and deposit/text writers,
  with rollback and real PostgreSQL cross-rail race tests. Neither the journal
  nor shared ownership grants a funded hold, outbox or refund authority.
- Router billing/settlement coverage: `pytest grid_api/routers/`.
- Candidate `0050` adds repeatable unpaid-input cleanup, independently default
  off. It closes expired, lease-aged, attempt-free staging before any exact-key
  deletion; every payment attempt is excluded. Abandonment is irreversible and
  cleanup ACKs are revisited. Optional staging/cleanup requires this migration;
  disabled startup does not query it. Real storage and funded-job retention
  transfer remain gates. No refund, payment, dispatch or public admission moves.
  Its focused matrix passes 212 cases; the final local full suite passes 3,325
  with 37 skips. A preceding process-test timeout is retained as inconclusive,
  followed by unchanged isolated and full passing reruns. Fresh CI is required.
- Private `0049` quote-input staging is a candidate with 234 focused staging/
  quote/attempt/shared-receipt tests and nine explicit skips. First attempt
  permission now requires its matching ready record; existing attempt recovery
  cannot acquire send permission. It writes no payments or jobs. Exact private
  keys precede uploads; ready recovery refreshes locations only. The local full
  suite passes 3,293 tests; the later merge-retry test passes in a separate
  14-case PostgreSQL credit suite. Fresh exact-commit CI and live storage
  qualification remain pending.
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

# grid-core scripts

## Purpose

Operational and developer entrypoints that sit outside the Python packages.
This directory includes the live hourly payout wrapper, Grid-native checks,
account provisioning tools, and an incomplete testnet model-registry helper.

## Ownership

- `payout_hourly.sh` - production systemd timer entrypoint for custodial AIPG
  payouts and failed-payout reconciliation.
- `deploy_model_registry.py` - incomplete Base Sepolia ModelRegistry scaffold;
  not the production Grid Diamond deployment path.
- `run_tests.sh` - Grid-native retirement, lint, and offline test wrapper.
- `check_retired_runtime.py` - CI guard against reintroducing the retired
  runtime through code, packaging, deploy, or operator scripts.
- `create_service_account.py` - one-time provisioning for bounded frontend or
  backend service principals; prints the new key exactly once. Google audiences
  and exact SIWE authorities are explicit per-service policy.
- `adopt_service_account.py` - transactionally promotes exactly one existing
  labeled API key into a bounded service principal without rotating its key or
  moving its account balance.
- `rotate_service_key.py` - atomically revokes a service's old keys and writes
  one replacement key to a caller-selected, newly created `0600` file. It never
  prints key material to stdout.
- `configure_service_identity.py` - preview-first, digest-bound update of an
  existing service's allowed proof providers, Google audiences, and exact SIWE
  authorities.
- `grant_canary_credit.py` - dry-run-by-default, capped operator credit for one
  allowlisted demand-billing canary.
- `grant_builder_credit.py` - dry-run-by-default, manually reviewed $5-$20
  promotional grants under an immutable, expiring, globally budgeted builder
  cohort. Apply mode requires that exact campaign ID in
  `GRID_PROMO_SPENDABLE_CAMPAIGNS`; it is an operator tool, never a public
  faucet or claim endpoint.
- `verify_demand_canary.py` - read-only reconciliation of one canonical
  account's balance, reservations, purchased-credit refs, and worker ledger
  terminals.
- `review_validator_operator.py` - preview-first, digest-bound candidate,
  verify, or reject transition for an opaque validator control group. It never
  publishes operator identity or grants economic authority.
- `preview_validator_compensation.py` - POSIX-only offline allocation simulation
  over a bounded private reviewer snapshot. It has no network/database/sender,
  rejects conflicting assignment replays, shares caps across an operator's nodes,
  and exclusively creates a private non-sendable output. Stdout is aggregate only.
  It does not authenticate the input or provide durable payment idempotency.
- `review_validator_reference.py` - preview-first, digest-bound quality review
  and activate/pause/revoke workflow for a media reference worker. It never
  fabricates bond evidence or grants economic authority.
- `review_worker_control.py` - preview-first, digest-bound verify/reject/revoke
  workflow for private media-worker common-control groups. It grants no
  economic authority and never publishes the group.
- `check_validator_media_readiness.py` - read-only image/video rollout
  preflight. It reads live worker state and a read-only PostgreSQL snapshot,
  applies the authoritative reference selector without reserving rows, and
  emits only aggregate/model-level blockers. `--require-ready` is the operator
  gate before a supervised non-economic canary; it is not permission to enable
  routing, rewards, strikes, or slashing.
- `report_validator_text_calibration.py` - read-only aggregate report of text
  probe transport states, verdicts, bounded score reasons, finish reasons, and
  model/lane counts. It cannot emit prompts, outputs, nonces, evidence,
  validator or worker identities, wallets, or accounts, and it grants no
  quality, routing, or economic authority.
- `manage_validator_shadow_run.py` - preview-first draft/start/finish control,
  aggregate reporting, and read-only Redis backlog health for the economically
  inert seven-day shadow run. Every mutation requires the exact fixed UTC time
  and gate/state hash from a fresh preview.
- `backup_postgres.sh` - root-only custom-format backup of the Grid-owned
  PostgreSQL schema with checksum, archive validation, locking, and bounded
  local retention. It excludes unrelated extension and legacy schemas.
- `prove_postgres_restore.sh` - restores one verified backup into a guarded
  disposable local database and migrates it with an immutable candidate.

## Local Contracts

- `payout_hourly.sh` moves real funds because it always passes `--send`. Do not
  run, edit, or repoint it casually. Preserve UTC period boundaries, the
  caller-injected environment, payout idempotency, receipt verification, and
  the retry step.
- Systemd owns `/etc/aipg/grid.env`; do not source that file from the shell
  wrapper or print its values.
- The payout wrapper resolves Python from its own immutable release directory;
  never point it back at the historical mutable production checkout.
- `deploy_model_registry.py` is a scaffold with no compiled deployment path.
  Never use it for Base mainnet or describe it as the canonical registry tool.

## Work Guidance

- Money-path changes belong primarily in
  `grid_api/services/settlement/payouts.py`; keep this wrapper thin.
- Add explicit dry-run defaults to any new chain, database, or cleanup tool.
- Read-only audit tools must connect with PostgreSQL
  `default_transaction_read_only=on`; do not initialize schemas or reuse a
  write-capable application session.
- A service with `--provider wallet` must also list every intended exact
  authority with `--siwe-domain`; never grant a wildcard domain.
- Frontend bridge services must not receive `--allow-direct-inference`. Reserve
  that explicit scope for capped service-owned bots or demos that cannot act for
  an end-user account; both per-request and daily ceilings are mandatory.
- Existing-service identity policy changes use
  `configure_service_identity.py`: run without `--apply`, inspect the complete
  policy, then apply with that preview's exact `current_digest`.
- Validator operator review uses `review_validator_operator.py`: assign the
  same opaque `opg_*` id to every registration under common control, preview
  every transition, inspect `eligible_to_apply` and `blocking_reasons`, and
  apply with that exact account- and signer-bound digest. Candidate review
  starts only for an active registration with a fresh heartbeat. An incomplete
  preview reports every blocker; the corresponding apply still fails closed.
  Re-entering `candidate` while a
  qualification is active fails closed on apply without the explicit
  `--restart-qualification` acknowledgement; use that flag on the fresh preview
  and its matching apply. Do not place names, emails, hostnames, IPs, or private
  review notes in the group id or review ref; review refs accept only the
  bounded opaque reference syntax.
- Shadow-run mutation uses `manage_validator_shadow_run.py`. Never apply a
  transition from a stale preview, mutable checkout, abbreviated implementation
  commit, or non-immutable verification reference. Never complete a run until
  its scheduled end has passed and `transport` reports a fully drained outbox.
  Preview and apply both require Core's proven runtime commit to equal the
  run's frozen implementation commit. Apply timestamps must be within five
  minutes of Core's UTC clock; only one run may be `running` at a time.
- Media reference review is two-step: use `review_validator_reference.py` with
  `--action review` to create/update a paused quality row, let the default-off
  chain sync write a fresh exact bond proof, then preview/apply `--action
  activate`. Every apply requires the fresh preview digest. Identity drift
  invalidates cached proof and requires another sync; revocation is terminal.
- Before activating a media reference or using a candidate, run
  `review_worker_control.py --action verify` with the same opaque `opg_*` id for
  every worker under common practical control. Preview first and apply only the
  returned digest. Never place names, emails, hostnames, IPs, wallet addresses,
  or private notes in the group id or review ref. Reviews expire and identity
  drift fails closed; revocation is terminal.
- Before changing either media assignment flag, run
  `check_validator_media_readiness.py --lane image --sync-recipes
  --require-ready` (and later `--lane video`). The command must use the selected
  immutable release and production env. It never lists validator identities,
  worker ids, wallets, control groups, review refs, prompts, or witnesses.
- Service-key rotation requires a new `--output` path on protected local
  storage. The tool writes and fsyncs the replacement before committing the
  rotation, removes the file if the database operation fails, and refuses to
  overwrite an existing file.
- Put reusable logic in the owning service package and test it there.
- Builder credits must remain promotional value: one grant per account and
  campaign, $5-$20 per grant, finite cohort budget, 90-day maximum expiry, and
  explicit `builder:*` review refs. Never expose the grant command as a public
  endpoint or silently move promotional value into purchased balance.
- Backup/restore tools must never print database credentials. Restore proof may
  only create or drop its generated `aipg_restore_proof_*` database.
- A restore proof drops the default `public` schema only after creating and
  selecting its guarded scratch database, because a schema-scoped archive must
  recreate that schema. Never generalize this to a caller-supplied database.
- Backups default to the Grid-owned `public` schema. `AIPG_BACKUP_SCHEMA` may
  select one explicit PostgreSQL identifier for a controlled migration, but a
  backup must never silently absorb unrelated extension schemas such as
  `cron`.
- Invoke Python operator tools through the selected release's `.venv/bin/python`;
  their env-based shebang assumes an already activated virtual environment.

## Verification

- Run `bash -n scripts/payout_hourly.sh` for wrapper edits.
- Run `bash -n scripts/backup_postgres.sh scripts/prove_postgres_restore.sh`
  and exercise both against disposable PostgreSQL before deployment.
- Pull-request CI must run the PostgreSQL 16 backup/restore proof, including a
  decoy non-Grid schema that the archive must exclude. A main-only proof is too
  late to protect a deployment candidate.
- Run focused settlement tests before changing payout invocation or periods.
- Run `scripts/run_tests.sh` from an environment with
  `requirements.dev.txt` installed.
- Run `git diff --check` and inspect commands for leaked secrets.

## Child DOX Index

No child guides are currently required; this file owns `scripts/`.

# grid_api/v2 - grid-owned schema

## Purpose

SQLAlchemy metadata for Grid-owned v2 tables: accounts, API keys, workers, jobs,
completion ledger, prepaid credits, credit ledger, reservations, canonical
identities/account aliases, promotional campaigns/grants, settlement
epochs, per-asset revenue pots (`grid_revenue`), multi-asset payout legs
(`grid_payout_legs`), registered validator identities plus shared probe-group/
assignment/attestation evidence rows, bounded service clients plus their
delegation audit events, private identity-bound media-worker control reviews,
the fail-closed bonded media reference-worker snapshot pool, and private
compensated-audit jobs plus hourly budget counters.

## Ownership

- `schema.py` - canonical in-code table definitions for `grid_*` tables.
- `__init__.py` - package marker.

## Local Contracts

- `schema.py` and `alembic/versions/` must match. `create_all(checkfirst=True)`
  cannot repair existing production tables or add missing constraints.
- Ledger tables are economic truth:
  - `grid_ledger` is one completion event per job (incl. `result_hash` — a real
    content commitment or NULL, never sha256("") — and `worker_sig`, stored ONLY
    when it verifies to the payout wallet).
  - `grid_credit_ledger` is append-only signed micro-USD deltas with unique refs
    (`ref` NOT NULL — money idempotency invariant, alembic 0008).
  - `grid_revenue` is the append-only per-asset distributable pool (idempotent
    on ref, native units) feeding pass-through payouts.
  - `grid_payout_legs` is one row per (period, account, asset) — the multi-asset
    rail's idempotency + audit record (rail, amount, status, external_id, nonce).
- `grid_reservations.free_micro` records how much of a hold came from the daily
  FREE allowance; the free and paid pockets NEVER convert (settlement restores
  free-to-free, refunds paid-to-paid).
- `grid_reservations.promo_micro` and `grid_promo_spends` preserve the durable
  promotional allocation. Campaign grants are unique per canonical account and
  globally budgeted.
- Reservation-time text rates and discount basis points are immutable billing
  evidence; settlement must not use a newer price book for an existing hold.
- `grid_account_identities` is authoritative for login identities; legacy
  wallet/email/oauth columns are compatibility primaries. Backfilled email is
  unverified and cannot authenticate. Aliases retire merged accounts without
  rewriting historical ledgers.
- `grid_accounts.payout_asset`/`payout_aipg_bps` are worker payout preferences
  (NULL → grid defaults); SELECTed on the HOT auth path — their migrations
  (0009) must run before code that reads them.
- `grid_validator_probe_groups` is the shared batch and quorum lifecycle. New
  text v8 groups store a generator/capability envelope, while each assignment
  stores its own randomized challenge. Already-open text v7 groups retain their
  shared challenge until they drain; legacy ungrouped assignments continue to
  own their challenge. Media groups lease exactly one
  candidate-plus-two-reference execution and
  persist one response-committed frozen witness set for independent scoring by
  every assigned validator. Retries are bounded and stale leases reclaimable.
  It targets five distinct registrations and requires three matching verdicts
  within that worker/capability lane by default. This is repeated capability
  sampling, not byte-for-byte reproduction or a quality score.
  `grid_validator_assignments` gates authoritative evidence with Grid-issued
  assignment ids, nonces, and hard-targeted probe evidence hashes. Its attempt
  counter and lease deadline enforce one bounded active probe per assignment.
  Its bounded `probe_result` stores only synthetic validator output and lets the
  assigned validator recover a completed result until its authoritative vote is
  accepted; it is not a customer inference archive.
  `grid_validator_attestations` stores both preview and authoritative evidence.
  Scorecards may aggregate them for
  operator/console visibility, but they must not be treated as economic truth
  until reward/dispute rules are live.
  Finalized assignment and group rows are bounded operational state and may be
  pruned after the configured retention window. Signed attestation rows and
  their canonical payloads remain the durable evidence record; pruning must not
  delete or rewrite them.
- `grid_validators` binds one normalized signing wallet to one canonical account
  and records capabilities, version, and heartbeat. Assignment and attestation
  `validator_id` foreign keys preserve attribution; account uniqueness prevents
  identity rotation. Its opaque `operator_group_id` is maintainer-reviewed
  correlated-control metadata: registrations in one group count once and may
  not occupy multiple seats in one probe group. Candidate qualification uses
  bounded heartbeat samples; verified reviews expire. Group identifiers and
  review references are never public. Group/validator uniqueness on assignments
  and attestations remains the final database guard against duplicate identity
  membership or votes.
- `grid_validator_reference_workers` is derived only from finalized Base bond
  sync plus non-economic quality review. Active selection requires a finalized
  block hash, routed facet and exact reviewed runtime proof, fresh bond, quality,
  worker-presence, account, and payout-wallet evidence; no worker may self-declare
  reference eligibility. `grid_validator_bond_sync_state` is the durable
  authority-scoped finalized cursor and health record. A faulted cursor must
  invalidate its cached eligibility until a later exact sync recovers it.
- `grid_worker_control_reviews` is the private maintainer-reviewed common-
  control map for media workers. A verified row snapshots the current worker
  account and payout wallet, carries one opaque `opg_*` group, and expires.
  Candidate plus two references must have three fresh distinct groups; account
  and wallet separation remains defense in depth. Identity drift, expiry,
  rejection, revocation, or a missing row fails closed. Group ids and review
  references never enter validator challenges or public APIs, and the table
  grants no economic authority.
- `grid_validator_audit_jobs` and `grid_validator_audit_budget_counters` are a
  dark authorization foundation, not a live scheduler. One ordinary UUID job
  may have at most one private audit row. Each hold consumes global, worker,
  validator, and validator/worker-pair caps in integer units; counters freeze
  their cap at first creation in each UTC-hour bucket. Demand and audit holds
  are mutually exclusive. The ordinary worker terminal now commits worker-ledger
  insertion and counter settlement in one caller-owned transaction. The expiry
  sweeper releases only holds without a completion row and quarantines conflicts.
  No scheduler exists, so these tables still cannot originate work.
- Account IDs are UUIDs. Quota identities such as `v2:<uuid>` are not DB foreign
  keys and must not be passed to credit ledger functions.
- New columns need explicit migrations, tests, and backfill/default strategy for
  existing rows.
- Do not store plaintext API keys, private keys, or worker secrets.

## Work Guidance

- Add tables with `grid_` prefixes and keep legacy Horde tables out of this file.
- Prefer portable SQLAlchemy types already used here unless a Postgres-only
  feature is required and documented.
- When changing account/key/worker schema, update `services/accounts.py`,
  `routers/accounts.py`, and worker registration paths together.

## Verification

- `pytest grid_api/services/tests/test_credits_billing.py`.
- `pytest grid_api/services/tests/test_payout_wallet.py`.
- Run Alembic upgrade checks when migration tooling is active in the target env.

## Child DOX Index

- None - leaf.

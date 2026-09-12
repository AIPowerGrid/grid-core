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
compensated-audit jobs plus hourly budget counters, and economically inert
validator shadow-observation records.

## Ownership

- `schema.py` - canonical in-code table definitions for `grid_*` tables.
- `__init__.py` - package marker.

## Local Contracts

- Migration `0042` adds `grid_credits.funded_balance_micro` and the append-only
  `grid_credit_ledger.funded_delta_micro`. Both default to zero: historical
  grants and purchased labels are not proof of external funding. A DB check
  constrains funded balance to the nonnegative spendable subset. The offline
  opening reconciler may append zero-spendable provenance entries after
  replaying verified receipts; it never rewrites historical deltas. Apply the
  migration before all writers, preserve provenance on rollback, and never
  resume a pre-0042 credit writer once funded history exists.

- `grid_payout_periods` is the immutable prospective worker payout plan, added
  empty by `0041`. One unique integer UTC-hour bucket prevents duplicate hourly
  budgets; the commitment binds cutoff, Base token, emission cap, minimum and
  all allocations. The plan and `grid_payouts` allocation rows commit together.
  Existing payout history receives no plan and is excluded from automatic
  accrued/retry sending. Never fabricate a plan to authorize historical backpay.

- Alembic `0040` adds nullable private `grid_reservations.media_client_ref`
  and `media_result` plus an account/ref lookup index. Historical rows stay
  SQL NULL; no result is reconstructed from an unverified worker claim.
  Results commit only with the winning media settlement, never independently
  of the charge/payout. Do not expose these fields in public stats or ledgers.
  The correlation index is deliberately nonunique: callers may reuse progress
  tokens, so reads must reject ambiguity and never interpret it as deduplication.

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
- `grid_validators.heartbeat_window_started_at` and `heartbeat_window_samples`
  are a bounded, server-observed rolling availability window, added by `0035`.
  Keep at most 864 unique five-minute buckets (72 hours). The legacy enrollment
  timestamp and lifetime counters remain untouched. No old totals are converted
  into fictional bucket timestamps; new columns begin NULL/empty. Updating the
  ring and lifetime counters shares the validator row lock and transaction.
- `grid_validator_shadow_runs`, `grid_validator_shadow_observations`,
  `grid_validator_shadow_outcomes`, `grid_validator_shadow_capacity_samples`,
  and `grid_validator_shadow_errors` are the private append-only evidence for a
  reviewed seven-day advisory experiment. They store bounded candidate and
  evidence snapshots plus commitments, never customer prompts/outputs,
  accounts, wallets, signatures, nonces, operator-group ids, or review refs.
  `mutation_attempted` is constrained false and fixed-size commitments are
  enforced by the database. Each observation has a delivery-specific
  `route_ref` plus a stable per-job `job_ref`; both are domain-separated HMACs,
  and neither exposes the raw job id. No production routing or economic path may read
  these tables, and creating them grants no validator authority.
  Alembic `0032`, the single-running-run index in `0033`, and exact-coverage
  commitment migration `0034` must exist before the observer flag can be considered; the flag
  remains off until the independently reviewed three-operator start gate passes.
- `grid_validator_pairings` is one replaceable, expiring slot per registered
  validator. `grid_validator_account_links` stores its current signed human-
  account association and revocation time. These are private operational
  metadata, never authentication identities, account aliases, payout ownership,
  or independence reviews. Neither table replaces `grid_validators.account_id`.
  The link and approved-to-linked transition commit together; no plaintext
  credential is stored. Alembic `0030` creates both empty and backfills nothing.
- `grid_oauth_clients` and `grid_oauth_authorizations` are the dark remote-MCP
  OAuth state. Clients are public and hold no secret. Authorization request and
  code values are stored only as SHA-256 hashes; codes are one-use, short-lived,
  account-bound, redirect-bound, client-bound, resource-bound, and PKCE-bound.
  Migration `0031` must precede any OAuth feature enablement. Authorization
  rows and old clients that never completed an exchange are bounded operational
  state and may be pruned; used clients and account/economic records remain.
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
- `grid_validator_compensation_campaigns`, `_allocations`, and `_work` are
  private approved pilot accounting, added empty by `0036`. Contracts and
  finalized allocations are immutable through the service API. One transaction
  claims each attestation/assignment and operator/probe-group at most once
  across campaigns. Integer token amounts conserve the frozen budget; no float
  conversion or worker payout row is involved. Retained verification facts plus
  the signed attestation preserve the work commitment after operational
  assignment pruning. Account beneficiaries are frozen, but their signing
  wallets are not implicitly payment recipients. Sending is a separate gate.
- `grid_validator_compensation_recipients`, added empty by `0037`, stores one
  immutable, dual-signed and maintainer-approved recipient per allocation.
  Composite beneficiary and unique allocation-hash foreign keys preserve its
  accounting references. The proof commits exact Base token/amount, destination
  and signature window; the service reconciles both commitments on every replay.
  No nonce, transaction or worker-payout row is created. Retain this financial
  consent history on rollback; downgrade refuses a nonempty table.
- `grid_validator_compensation_payments`, added empty by `0038`, binds an
  allocation's approved recipient to one unique signed transaction and nonce.
  Pending/manual-review rows have SQL NULL receipts (not JSON null); sent rows
  retain finalized Transfer proof. Unique allocation, plan, transaction and
  sender/chain/nonce constraints defend replay. Signed bytes are private
  execution capability, not a stored private key; never expose them in an API.
  Downgrade refuses nonempty history. Worker nonce allocation reads this table
  even when validator sending is disabled, so migration precedes runtime code.
- `grid_validator_compensation_requests`, added empty by `0039`, holds one
  replaceable 24-hour wallet/node consent slot per finalized allocation. It
  binds the exact current human/node association; it is not an approved
  recipient or a payment. SQL NULL distinguishes absent consent/signatures.
  Expired/cancelled unbound slots may be replaced, never immutable approved
  recipient/payment rows. Downgrade refuses nonempty pending proof.
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

# grid_api/v2 - grid-owned schema

## Purpose

SQLAlchemy metadata for Grid-owned v2 tables: accounts, API keys, workers, jobs,
completion ledger, prepaid credits, credit ledger, reservations, settlement
epochs, per-asset revenue pots (`grid_revenue`), multi-asset payout legs
(`grid_payout_legs`), and validator assignments/attestation evidence rows.

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
- `grid_accounts.payout_asset`/`payout_aipg_bps` are worker payout preferences
  (NULL → grid defaults); SELECTed on the HOT auth path — their migrations
  (0009) must run before code that reads them.
- `grid_validator_assignments` gates authoritative evidence with Grid-issued
  assignment ids, nonces, and hard-targeted probe evidence hashes.
  `grid_validator_attestations` stores both preview and authoritative evidence.
  Scorecards may aggregate them for
  operator/console visibility, but they must not be treated as economic truth
  until reward/dispute rules are live.
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

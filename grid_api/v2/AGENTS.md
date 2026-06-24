# grid_api/v2 - grid-owned schema

## Purpose

SQLAlchemy metadata for Grid-owned v2 tables: accounts, API keys, workers, jobs,
completion ledger, prepaid credits, credit ledger, and settlement epochs.

## Ownership

- `schema.py` - canonical in-code table definitions for `grid_*` tables.
- `__init__.py` - package marker.

## Local Contracts

- `schema.py` and `alembic/versions/` must match. `create_all(checkfirst=True)`
  cannot repair existing production tables or add missing constraints.
- Ledger tables are economic truth:
  - `grid_ledger` is one completion event per job.
  - `grid_credit_ledger` is append-only signed micro-USD deltas with unique refs.
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

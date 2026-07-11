# alembic - grid database migrations

## Purpose

Migration source for Grid-owned database tables. These migrations must make the
production database match `grid_api/v2/schema.py` without relying on
`create_all(checkfirst=True)` to alter existing tables.

## Ownership

- `env.py` - Alembic environment.
- `script.py.mako` - revision template.
- `versions/` - ordered migration revisions. Current head: `0013`
  (`0009` payout-pref cols, `0010` grid_revenue, `0011` grid_payout_legs,
  `0012` reservations.free_micro, `0013` universal identities, scoped keys,
  promotional grants, and reservations.promo_micro).

## Local Contracts

- **Hot-path columns migrate FIRST:** `0009`'s payout-preference columns are
  SELECTed by `resolve_api_key` on every request — deploying code before the
  migration fails v2 auth GLOBALLY. Prod is create_all + manual ALTER (see the
  prod-schema notes); these migrations keep every other deploy path safe.
- Use `op.batch_alter_table` for column alters so SQLite (gateway-in-a-box)
  can upgrade to head — plain `ALTER COLUMN` broke it once (`0008`).

- Every schema change in `grid_api/v2/schema.py` requires a matching Alembic
  revision, including constraints and indexes.
- Migrations must be idempotent only where Alembic expects them to be; do not
  hide failed DDL with broad exception swallowing.
- Economic constraints matter: unique `grid_ledger.job_id`, non-null credit refs
  for value-moving rows, and FK consistency are money-safety properties.
- Do not edit or depend on generated `__pycache__` files.

## Work Guidance

- Name revisions with the next sequence and a short description.
- Include data backfill or validation steps when tightening nullable columns.
- Keep downgrade honest; if a downgrade is unsafe, state that explicitly in code
  comments.
- After changing migrations, update deploy/runbooks if production upgrade
  commands or order change.

## Verification

- Run migration upgrade/downgrade against a disposable DB when practical.
- At minimum, run `pytest grid_api/services/tests/test_credits_billing.py` for
  credit-ledger or account schema changes.

## Child DOX Index

- None - leaf.

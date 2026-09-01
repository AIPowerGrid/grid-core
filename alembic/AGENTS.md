# alembic - grid database migrations

## Purpose

Migration source for Grid-owned database tables. These migrations must make the
production database match the Grid-owned schema contracts without relying on
`create_all(checkfirst=True)` to alter existing tables.

## Ownership

- `env.py` - Alembic environment.
- `script.py.mako` - revision template.
- `versions/` - ordered migration revisions. Current head: `0033`
  (`0009` payout-pref cols, `0010` grid_revenue, `0011` grid_payout_legs,
  `0012` reservations.free_micro, `0013` universal identities, scoped keys,
  promotional grants, and reservations.promo_micro; `0014` codifies safe DB
  defaults for Grid-native inserts into the optional legacy waiting-prompts
  table; `0015` adds native service clients, expiring-key metadata, and
  reservation-time price snapshots; `0016` reconciles early production
  constraint drift for ledger idempotency and validator evidence; `0017` adds
  immutable Base deposit receipts committed with purchased-credit movements;
  `0018` records x402 reservation provenance and on-chain settlement receipts;
  `0019` adds service SIWE domain binding; `0020` adds registered validator
  identities and binds assignments/attestations to them; `0021` adds bounded,
  reclaimable validator-probe leases; `0022` adds shared probe groups and
  distinct-validator quorum constraints; `0023` adds the fail-closed bonded
  media reference-worker snapshot pool; `0024` adds one leased media execution
  and one committed frozen witness set per probe group; `0025` adds a bounded
  durable assignment result used to recover completed-but-unattested probes;
  `0026` adds opaque operator groups, expiring independence-review
  state, and bounded qualification heartbeat samples; `0027` persists finalized
  WorkerRegistry block/facet proofs plus one authority-scoped sync cursor;
  `0028` adds private, expiring, identity-bound media-worker common-control
  reviews; `0029` adds the private compensated-audit job and hourly budget-
  counter foundation, without enabling audit dispatch or worker payout;
  `0030` adds bounded validator/account pairing slots and signed visibility
  associations, without moving node accounts or enabling any economics;
  `0031` adds empty OAuth public-client and authorization-state tables for the
  disabled-by-default remote MCP authorization foundation; `0032` adds private
  validator shadow-run, observation, outcome, capacity-sample, and bounded-error
  tables without enabling collection, routing, or economics; `0033` adds the
  database-enforced single-running-shadow invariant).

## Local Contracts

- **Hot-path columns migrate FIRST:** `0009`'s payout-preference columns are
  SELECTed by `resolve_api_key` on every request — deploying code before the
  migration fails v2 auth GLOBALLY. Prod is create_all + manual ALTER (see the
  prod-schema notes); these migrations keep every other deploy path safe.
- Use `op.batch_alter_table` for column alters so SQLite (gateway-in-a-box)
  can upgrade to head — plain `ALTER COLUMN` broke it once (`0008`).

- Every schema change in `grid_api/v2/schema.py` requires a matching Alembic
  revision, including constraints and indexes.
- Deploy through `0022` before shared-quorum validator code. Older evidence remains
  readable because its new `validator_id` is nullable; all newly authoritative
  evidence must be registration-bound in application code.
- A validator assignment may have only one active probe lease. Retries are
  bounded, and a late result may update only the matching current job id. A
  completed assignment may replay only its stored result to the same registered
  validator during the attestation grace window.
- `0026` is operational metadata, not a trust grant. Existing registrations
  backfill to `unreviewed`; only the preview-first maintainer review tool may
  move them through candidate qualification to an expiring verified state.
- `grid_validator_reference_workers` is a background-sync cache, not worker
  self-report. Keep it empty until the reviewed cooldown-backed WorkerRegistry
  is deployed; media validation must fail closed when snapshots are stale or
  identities are not independent.
- Apply `0027` before any release that starts the bond-sync loop or selects
  bonded references. Existing rows backfill with NULL proof fields and remain
  ineligible until a successful reviewed sync writes the full proof.
- Apply `0028` before any release with the three-control-domain media selector.
  It intentionally backfills no trust: existing candidates and references stay
  ineligible until a preview-first worker-control review is explicitly applied.
- Apply `0029` before any code may reserve a compensated validator audit. The
  migration creates empty private state only: it backfills no budget, queues no
  work, and grants no validator or worker economic authority.
- Apply `0030` before enabling optional validator-account pairing. The new
  tables start empty, and existing validator/authentication paths do not query
  them. Operational rollback is flag-off, not deleting identity configuration
  or downgrading the database.
- Apply `0031` before deploying remote MCP OAuth code. The migration only adds
  empty tables and indexes; it does not register clients, authorize accounts,
  issue tokens, or enable routes. Keep `GRID_MCP_OAUTH_ENABLED=0` until the
  separate Console consent and remote MCP rollout gates pass.
- Apply `0032` before any validator shadow collection is enabled. A dark
  migration creates empty observer tables only. Production routing and economic
  paths do not query them, and operational rollback is
  `VALIDATOR_SHADOW_OBSERVER_ENABLED=0`, not deleting replay evidence or
  downgrading the database.
- Apply `0033` before starting a shadow run. It prevents overlapping experiments
  even when two operators race the preview/apply workflow.
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

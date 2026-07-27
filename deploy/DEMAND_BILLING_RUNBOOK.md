# Demand billing launch runbook

## Safety invariants

- New work is charged only when `GRID_CHARGING_MODE` selects it.
- Every selected request reserves before dispatch. A terminal worker event
  atomically records supply-side work and settles or releases the demand hold.
- Turning charging off stops new holds but does not abandon existing holds.
- Purchased balance equals the append-only purchased-credit ledger globally.
- Discord alerts are operational hints, not accounting authority. PostgreSQL
  remains the source of truth.

## Rollout modes

`GRID_CHARGING_MODE=off` previews prices without moving value.

`GRID_CHARGING_MODE=allowlist` charges only an account listed in
`GRID_CHARGING_ALLOW_ACCOUNTS` or a service listed in
`GRID_CHARGING_ALLOW_SERVICES`. `GRID_CHARGING_ALLOW_MODELS`, when non-empty,
further restricts the selected cohort to exact model IDs.

`GRID_CHARGING_MODE=on` charges every authenticated request and default-denies
unpriced work. Do not use this mode for the first production test.

The legacy `GRID_CHARGING_ENABLED` boolean is consulted only when
`GRID_CHARGING_MODE` is absent. Keep it `0` once the mode is configured.

## Release gate

From a clean reviewed commit and a disposable Postgres database:

```bash
python -m pip check
python -m alembic upgrade head
python -m alembic check
python -m pytest grid_api -q
```

Run the real Postgres races too:

```bash
export CREDITS_TEST_DB_URL=postgresql+asyncpg://grid_test:grid_test@127.0.0.1/grid_test
export PAYOUTS_TEST_DB_URL="$CREDITS_TEST_DB_URL"
python -m pytest \
  grid_api/services/tests/test_credits_concurrency.py \
  grid_api/services/settlement/tests/test_payouts_lifecycle.py -q
```

The disposable database must not be production or share production tables.

## Dark deployment

1. Deploy Core using the immutable-release procedure in `deploy/README.md`.
2. Keep `GRID_CHARGING_MODE=off`, free/promo spending off, and deposits off.
3. Add `GRID_ALERT_DISCORD_WEBHOOK` only to `/etc/aipg/grid.env`; preserve its
   restrictive permissions and never print the file.
4. Restart Core. Confirm `core_started` arrives without secrets and reports
   `charging_mode=off`.
5. Check `/health`, `/v1/models`, worker reconnects, Core logs, and the billing
   invariant monitor. Any `billing_invariant_failed` alert blocks the canary.
6. Deploy the strict-SIWE Console release and verify Google plus wallet login
   before removing any temporary legacy SIWE compatibility flag.

## Fund one canary

The grant tool is dry-run by default and capped at $10:

```bash
python scripts/grant_canary_credit.py \
  --account-id <canonical-account-uuid> \
  --amount-usd 2.00 \
  --ref canary:<date>
```

After verifying the account, amount, and idempotency ref, repeat with `--apply`.
Run it from the selected production release with the protected service
environment loaded. A repeat with the same ref must print `applied=false` and
must not increase the balance.

## Allowlisted canary

1. Set `GRID_CHARGING_MODE=allowlist`.
2. Put only the canary UUID in `GRID_CHARGING_ALLOW_ACCOUNTS`. Optionally set
   one exact model in `GRID_CHARGING_ALLOW_MODELS`.
3. Restart Core and confirm the startup alert reports one selected account.
4. Verify `GET /v1/account/credits` reports `charging_mode=allowlist` and
   `charging_enabled=true` for the canary. A second account must report false.
5. Submit one short non-streaming request. Verify one held reservation becomes
   settled, the balance decreases by actual grid-counted usage, and the worker
   completion ledger has one row.
6. Submit one stream and disconnect after output begins. Verify the worker
   terminal still settles the reservation.
7. Request work whose maximum quote exceeds the remaining balance. It must
   return `402` before queueing and generate an `insufficient_credit` alert.
8. Retry the canary-credit grant with its original ref. Balance must not move.
9. Confirm no stale holds, no negative balances, no ledger drift, and no
   settlement or service-exposure alerts.

## Alerts

Expected success events:

- Core startup with rollout counts
- new account creation, identified only by an opaque correlation hash
- service-principal provisioning
- verified USDC/ETH credit
- operator canary credit

Important warning/critical events:

- unpriced work selected for live charging
- insufficient balance or service exposure limit
- reservation inconsistency/failure
- settlement, refund, sweeper, or monitor failure
- under-collection and late success after release
- stale/aging monetary holds
- purchased-balance versus ledger mismatch
- Base RPC/oracle deposit failure or wallet mismatch
- unhandled Core HTTP failure and route rate limiting

Repeated alerts are deduplicated for `GRID_ALERT_DEDUPE_SECONDS`; delivery uses a
bounded queue and never blocks account, inference, deposit, or settlement work.

## Kill switch and rollback

Set `GRID_CHARGING_MODE=off` and restart to stop new charges. Do not delete
reservation rows or stop the sweeper: already-held jobs still need to settle or
refund. If code rollback is required, choose a retained release compatible with
the current Alembic schema as described in `deploy/README.md`.

Do not enable free/promotional spending, deposits, or global `on` mode merely
because the allowlisted canary passes. Each expands financial exposure and has
its own explicit launch decision.

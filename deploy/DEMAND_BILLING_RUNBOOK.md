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
`GRID_CHARGING_ALLOW_SERVICES`. Account selection applies to user and delegated
frontend work. Service selection applies only to a direct service principal
that has the exceptional `inference.service_submit` scope; it never selects
users delegated through that service. `GRID_CHARGING_ALLOW_MODELS`, when
non-empty, further restricts the selected cohort to exact model IDs.

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
2. Keep `GRID_CHARGING_MODE=off` and free/promo spending off. The independently
   verified Base USDC deposit rail may remain enabled; keep AIPG, direct ETH,
   and x402 funding dark.
3. Add `GRID_ALERT_DISCORD_WEBHOOK` only to `/etc/aipg/grid.env`; preserve its
   restrictive permissions and never print the file.
4. Restart Core. Confirm `core_started` arrives without secrets and reports
   `charging_mode=off`.
5. Check `/health`, `/v1/models`, worker reconnects, Core logs, and the billing
   invariant monitor. Any `billing_invariant_failed` alert blocks the canary.
6. Deploy immutable Console, Chat, Art, and Music releases. Verify that Google
   plus linked-wallet login resolves to the same canonical account and
   purchased balance on every surface.

## Fund one canary

Use a dedicated canonical account and fund it through the production Console
with approximately `$0.25` of Base USDC. Record:

- canonical account UUID;
- linked funding wallet;
- Base transaction hash and deposit receipt ID;
- purchased balance before and after funding;
- credit-ledger ref and amount.

Retry the same deposit claim. It must return the same receipt without changing
the balance. An operator grant is useful for disposable tests but does not prove
the production funding rail and is not the launch canary.

## Allowlisted canary

1. Set `GRID_CHARGING_MODE=allowlist`.
2. Put only the canary UUID in `GRID_CHARGING_ALLOW_ACCOUNTS` and put only the
   approved production models in `GRID_CHARGING_ALLOW_MODELS`. Leave
   `GRID_CHARGING_ALLOW_SERVICES` empty for delegated Chat, Art, Music, Console,
   and user API-key tests. Add a service ID only for an intentionally
   service-owned workload whose key has `inference.service_submit` and bounded
   per-request/daily exposure.
3. Restart Core and confirm the startup alert reports the expected account,
   service, and model counts.
4. Verify `GET /v1/account/credits` reports `charging_mode=allowlist` and
   `charging_enabled=true` for the canary. A second account must report false.
5. Run one successful request through Chat Completions, Responses, and
   Anthropic Messages. Include a streaming request and a disconnect after
   output begins.
6. Run Krea text-to-image, Z-Image, one supported image-to-image request, Music,
   and video. Record each frontend quote, job ID, reservation, terminal state,
   actual charge, and worker ledger row.
7. For every request, verify the hold exists before dispatch, success settles
   exactly once, and the purchased balance plus active promotional/daily
   pockets move in the documented spending order.
8. Force one worker failure or timeout per lifecycle family (text,
   passthrough, media). Each hold must release exactly once without a worker
   payout.
9. Request work whose maximum quote exceeds the remaining balance. It must
   return `402` before queueing and generate an `insufficient_credit` alert.
10. Retry one completed request and one terminal event. Neither may
    double-charge or double-pay.
11. Confirm no stale holds, negative balances, ledger drift, settlement errors,
    or service-exposure alerts.
    Reconcile the recorded job IDs from the selected immutable release without
    using browser cookies, API keys, or a write-capable database session:

    ```bash
    .venv/bin/python scripts/verify_demand_canary.py \
      --account-id "$CANARY_ACCOUNT_ID" \
      --success "$CHAT_JOB_ID" \
      --success "$IMAGE_JOB_ID" \
      --success "$MUSIC_JOB_ID" \
      --failure "$FORCED_FAILURE_JOB_ID" \
      --absent "$INSUFFICIENT_JOB_ID" \
      --allow-service aipg-chat \
      --allow-service aipg-art \
      --allow-service aipg-music
    ```

    Exit `0` and `"ok": true` are required. The tool uses a PostgreSQL
    read-only session and fails on account/global ledger drift, negative
    balances, stale holds, invalid pocket splits, wrong account/service
    attribution, missing terminal evidence, or inconsistent per-job credit
    movements.
12. Leave the same allowlist active for 24 hours of normal first-party use.
    Reconcile funding receipts, balances, credit ledger, reservations, worker
    completion ledger, and alerts before expanding the cohort.

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

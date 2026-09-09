# Demand billing launch runbook

## Safety invariants

- New work is charged only when `GRID_CHARGING_MODE` selects it.
- Every selected request reserves before dispatch. A terminal worker event
  atomically records supply-side work and settles or releases the demand hold.
- Turning charging off stops new holds but does not abandon existing holds.
- Purchased balance equals the append-only purchased-credit ledger globally.
- Discord alerts are operational hints, not accounting authority. PostgreSQL
  remains the source of truth.

Current launch evidence and incomplete gates are tracked in
`DEMAND_BILLING_LAUNCH_2026_09_08.md`; this runbook alone is not proof of deployment.

## Prospective reward cutover

Keep automatic payouts paused while reconciling demand and supply. Select and
record one future timezone-aware `WORKER_REWARDS_PAID_ONLY_SINCE` boundary after
in-flight work is accounted for. Retain that exact value across subsequent
deployments and rollback; never move it to rewrite historical obligations.

After the boundary, unrestricted reward DEN comes only from positive settled
purchased-credit consumption or externally settled x402 work. A mixed-pocket
job contributes the purchased fraction of its DEN. Free/promo work and jobs
without a valid settled reservation contribute zero, not a redistributed share.
Any future compensation for those workloads requires a separate capped budget.

Custodial allocations cap post-boundary SmolLM-family work at 0.5% of the
period's emission budget across the entire network, not per account or wallet.
At the existing 208.33 AIPG/hour budget this is at most 1.04165 AIPG/hour
(24.9996/day for 24 nonoverlapping hourly periods). Below-cap work keeps its
smaller proportional share. Excess stays unspent. Preview reports unallocated
value explicitly. A cap is not permission to increase budgets, backfill overlapping
periods, or pay disputed history. Paid model-substitution fraud remains a
separate validator concern.

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
unpriced work. It also requires an explicit `GENERATION_ENABLED_PATHS` JSON
list; an omitted list rejects startup before dependencies or background tasks
start. This applies to global charging through the legacy boolean too. `[]`
is a valid closed rollout. Select only paths with recorded successful canaries;
the explicit-list guard cannot establish that evidence for you. Dark and
allowlisted modes retain their existing admission defaults. Do not use global
mode for the first production test.

The legacy `GRID_CHARGING_ENABLED` boolean is consulted only when
`GRID_CHARGING_MODE` is absent. Keep it `0` once the mode is configured.
An invalid explicit mode rejects startup and authorization; it must never
silently turn charging off. Test configuration before selecting a release.

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
   Pin the reviewed launch policy explicitly in `/etc/aipg/grid.env`:

   ```dotenv
   GRID_WELCOME_GRANT_MICRO=100000
   GRID_WELCOME_BUDGET_MICRO=500000000
   GRID_FREE_DAILY_MICRO=10000
   GRID_FREE_HOLDER_BONUS_MICRO=0
   GRID_PROMO_SPENDABLE_LIVE=0
   GRID_PROMO_SPENDABLE_CAMPAIGNS=
   GRID_FREE_SPENDABLE_LIVE=0
   ```

   These values mean `$0.10` once for verified Google, a `$500` global welcome
   ceiling, `$0.01/day` for verified Google, and no wallet-only holder faucet.
   Promotional spending additionally requires one or more reviewed exact
   campaign IDs; the global live flag alone must leave every campaign dark.
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

For a public billing incident, stop accepting new generation, not just new
charges. Gate the exact generation routes at the reverse proxy first, then set
`GENERATION_ENABLED_PATHS=[]` and restart using the drained immutable-release
procedure. Preserve the selected charging mode and all economic policy. Verify
every generation family rejects before reserve/dispatch, then remove the
temporary proxy gate only when the application admission gate is confirmed.

`GRID_CHARGING_MODE=off` alone is NOT an inference kill switch: it permits
uncharged generation. Use it only for an explicitly authorized preview cohort
or while public generation remains closed. Never use it as the sole rollback
for a global billing outage.

Do not delete reservation rows or stop the sweeper: already-held jobs still
need to settle or refund. Keep payout senders paused. If code rollback is
required, choose a retained release compatible with the current Alembic schema
as described in `deploy/README.md`. Retain the exact prospective reward cutoff
and additive payout plans; older senders must not run against frozen periods.

Do not enable free/promotional spending, deposits, or global `on` mode merely
because the allowlisted canary passes. Each expands financial exposure and has
its own explicit launch decision.

## Global activation

This procedure does not waive the release, identity, canary or 24-hour
same-cohort observation gates above. The launch record must identify which
gates are proven and which remain open. An elapsed clock without reconciliation
is not sufficient.

1. Confirm the exact deployed commit, Alembic revision, all serving processes,
   seven reviewed generation capabilities, healthy workers, and stopped payout
   service/timer. Reconcile balances, reservations, completion/reward backing,
   funding receipts and alerts. Preserve private evidence; do not log secrets.
2. Snapshot the protected environment and take a fresh verified database backup.
   Retain the prior compatible API release. Record the prospective cutoff and
   current service ceilings, promotion campaign allowlist and free-credit state.
3. Gate new generation at the proxy and drain queued/in-flight work. Change
   only `GRID_CHARGING_MODE` from `allowlist` to `on`; leave the legacy boolean
   at `0`, explicit admitted paths, cohort lists, reward cutoff, budgets and
   funding configuration untouched. Retained cohort lists do not narrow `on`.
4. Validate typed configuration before restart. After restart verify the actual
   supervisor and serving-worker environments, immutable commit and public
   health. Keep the proxy gate until those checks pass. If they do not, follow
   the closed-generation rollback above, not an uncharged public fallback.
5. With generation reopened, test an ordinary account outside the former
   allowlist and an empty bounded direct service. Both must report global `on`
   and reject insufficient credit before queueing. A bridge without delegated
   identity must still reject authentication. Inspect economic rows and queue
   evidence; an HTTP error alone does not prove no dispatch.
6. Use only the remaining approved canary spend for successful work. Reconcile
   the exact account, hold, charge, refund, terminal and purchased-backed reward
   for each enabled modality. Confirm Chat, Art, Music and Console show the same
   current balance and charging state after reload; stale preview text is not
   acceptable. Unverified image batches, img2img, timelines and 3D stay closed.
7. Any invariant failure closes generation and preserves evidence. A depleted
   user's normal 402 is expected, not a reason to turn charging off. Monitor
   service caps and operational alerts; do not expand grants or caps to mask
   failures.
8. Payout activation is separate: reconcile frozen prospective allocations,
   historical pending nonces and treasury state, then perform the separately
   reviewed supervised send/replay before enabling the hourly timer. Global
   billing activation itself must not move worker funds.

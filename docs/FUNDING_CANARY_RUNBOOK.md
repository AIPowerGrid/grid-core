# Base funding canary runbook

Status: **commands and schema flow tested on disposable Postgres; real Base
transfers require operator authorization.**

This runbook is the release gate for accepting Base USDC. It does not authorize
a production deploy, a treasury change, or a transfer. AIPG, direct ETH, and
x402 remain separate flags and must not be enabled by accident.

## Invariants

- Purchased balances and ledger movements are integer micro-USD.
- Credits buy Grid service. They are non-transferable and nonwithdrawable.
- One Base transaction creates at most one immutable deposit receipt and one
  positive credit-ledger movement.
- The claiming wallet must already be linked to the authenticated account.
- A duplicate claim is a read-only idempotent success.
- Worker payout from x402 work remains blocked until the payment row is
  durably `settled`.

## 1. Dark deploy

Deploy migration `0018` before the application release. Keep every new rail
dark:

```dotenv
GRID_DEPOSITS_ENABLED=0
GRID_AIPG_DEPOSITS_ENABLED=0
GRID_ETH_CONVERSION_MODE=disabled
GRID_X402_ENABLED=0
```

Run from the immutable release directory:

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic check
```

Expected: current revision `0018` and `No new upgrade operations detected`.
Confirm Core health and ordinary free/paid inference before changing a funding
flag.

## 2. USDC preflight

Use a dedicated, monitored Base treasury address. Core needs only its public
address and RPC URL; it must not hold the treasury or payout private key.

The production target is Base Safe
[`0xd19a391FAB4aeFd5f59e4be4918364f57b9c5346`](https://basescan.org/address/0xd19a391FAB4aeFd5f59e4be4918364f57b9c5346).
Read-only Base verification on 2026-07-27 found deployed bytecode, two distinct
owners, and threshold 2. It is dark-configured in Core; this entry does not
authorize enabling deposits or sending the canary.

```dotenv
GRID_BASE_CHAIN_ID=8453
GRID_BASE_RPC=<monitored Base RPC>
GRID_DEPOSIT_CONFIRMATIONS=3
GRID_DEPOSIT_MIN_MICRO=10000
GRID_USDC_MAX_DEPOSIT_MICRO=10000000000
GRID_USDC_ACCOUNT_DAILY_MICRO=25000000000
GRID_USDC_NETWORK_DAILY_MICRO=100000000000
GRID_USDC_CONTRACT=0x833589fcd6edb6e08f4c7c32d4f71b54bda02913
GRID_USDC_TREASURY=0xd19a391FAB4aeFd5f59e4be4918364f57b9c5346
GRID_DEPOSITS_ENABLED=1
GRID_AIPG_DEPOSITS_ENABLED=0
GRID_ETH_CONVERSION_MODE=disabled
GRID_X402_ENABLED=0
```

Restart Core and verify authenticated
`GET /v1/account/deposits/config` reports:

- chain id `8453`;
- USDC enabled with 6 decimals;
- the intended treasury and canonical USDC contract;
- credits non-transferable and nonwithdrawable;
- AIPG unavailable and ETH direct-send disabled.

Stop if any value differs.

## 3. $0.01 USDC canary

1. Sign in to the Console with the operator account.
2. Link the Base wallet that will send the canary.
3. Open Funding and send exactly `0.01 USDC` to the displayed treasury.
4. Wait for the configured confirmations and claim the transaction.
5. Record the account id, transaction hash, pre/post balance, receipt id, block,
   source wallet, and timestamp in the release evidence.

The UI must show one immutable USDC receipt and a `+0.01` purchased-balance
change. BaseScan must show canonical Base USDC moving directly from the linked
wallet to the configured treasury.

Database evidence:

```sql
SELECT account_id, chain_id, asset, token_address, tx_hash, block_number,
       from_address, treasury_address, amount_raw, amount_decimals,
       price_micro, price_source, credited_micro, refund_address, status
FROM grid_deposits
WHERE chain_id = 8453 AND asset = 'USDC' AND tx_hash = '<lowercase tx hash>';

SELECT account_id, delta_micro, reason, ref
FROM grid_credit_ledger
WHERE ref = 'base:8453:usdc:<lowercase tx hash>';
```

Expected: exactly one row from each query, `amount_raw=10000`,
`price_micro=1000000`, `credited_micro=10000`, `delta_micro=10000`, and the
same account id.

For that account, prove the purchased-balance cache equals its append-only
ledger:

```sql
SELECT c.balance_micro, l.ledger_micro
FROM grid_credits c
JOIN (
  SELECT account_id, SUM(delta_micro) AS ledger_micro
  FROM grid_credit_ledger
  WHERE account_id = '<account uuid>'
  GROUP BY account_id
) l ON l.account_id = c.account_id;
```

Expected: `balance_micro = ledger_micro`.

## 4. Replay proof

Submit the same transaction hash through
`POST /v1/account/deposits/claim` using the same authenticated account.
Expected: `already_claimed=true`, the same receipt, no balance change, and still
exactly one deposit row and one ledger row.

Attempting to claim the transaction from a different account must fail because
the on-chain sender is not that account's linked wallet.

## 5. x402 canary

Do this only after the USDC deposit canary passes. Start on Base Sepolia with
matching test USDC, facilitator, network, and treasury values. Use a fresh
low-value payer and `GRID_X402_MAX_AUTH_MICRO=50000` ($0.05 maximum).

Evidence required:

1. Initial request returns a valid x402 payment requirement.
2. Paid retry produces one non-streaming text completion.
3. Reservation records grid-counted actual usage below the authorization.
4. Payment transitions `verified -> settling -> reported -> settled`.
5. Base receipt proves canonical USDC from payer to recipient for exactly
   `settled_micro`.
6. One repeated authorization cannot open or settle a second job.
7. Worker payout excludes `verified`, `settling`, `reported`, and
   `manual_review`, then includes only independently proven `settled`.
8. A forced ambiguous failure enters `manual_review` and alerts.

For an ambiguous real transfer, reconcile only with the exact Base transaction:

```bash
.venv/bin/python -m grid_api.services.x402_payments \
  --reconcile-job <job-uuid> \
  --tx <base-transaction-hash>
```

Repeat on Base mainnet with the same $0.05 ceiling only after Sepolia passes.
Do not raise the ceiling or add streaming/media until an automated chain
indexer/reconciler is deployed.

## 6. Holds

- **AIPG:** keep dark until a named price-epoch owner, expiry alert, refund
  owner, and conservative exposure caps are operational.
- **ETH:** Core can verify `swap_receipt` and credit only actual canonical USDC
  proceeds, but the Console still needs a reviewed quote/router adapter and
  transaction-intent binding.
- **Cards:** later adapter into the same non-transferable credit ledger; no
  separate balance system.

## Rollback

Set `GRID_DEPOSITS_ENABLED=0` and `GRID_X402_ENABLED=0`, then restart Core.
This stops new claims and x402 requests without deleting receipts or changing
existing balances. Never roll back by deleting economic rows. Investigate any
credited mistake through the refund/adjustment process with a new durable ledger
reference.

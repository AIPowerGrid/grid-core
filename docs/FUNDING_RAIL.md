# Demand-side funding rail

Status: **implemented, migration-gated, and default-off.** USDC is the launch
rail. AIPG is code-complete behind a separate switch and expiring price epoch.
Direct ETH is conversion-gated and must remain disabled for normal production
until the Grid can turn it into USDC without carrying an open ETH/USD position.

Operational release evidence is defined in
[FUNDING_CANARY_RUNBOOK.md](FUNDING_CANARY_RUNBOOK.md).

All rails fund one integer micro-USD purchased-credit balance. Credits buy Grid
services; they are non-transferable and non-withdrawable. Operator-reviewed
refunds go back to the recorded source address.

## Production registry

- **Network:** Base mainnet, chain ID `8453`.
- **Canonical USDC:** `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`.
- **Customer funding Safe:**
  [`0xd19a391FAB4aeFd5f59e4be4918364f57b9c5346`](https://basescan.org/address/0xd19a391FAB4aeFd5f59e4be4918364f57b9c5346),
  verified as a deployed 2-of-2 Safe on 2026-07-27.
- **Operational status:** dark-configured; no funding rail is enabled until its
  supervised canary and rollback checks pass.

The Safe is an operational custody endpoint, not a protocol contract. Core
stores its public address only and must never receive Safe signer material.

## Invariants

Every successful Base claim commits one SQL transaction containing:

1. An immutable `grid_deposits` receipt: chain, asset, token, transaction,
   block, sender, treasury, raw amount/decimals, valuation source/time/block,
   credited micro-USD, and refund address.
2. The idempotent `grid_credit_ledger` movement.
3. The updated `grid_credits` balance cache.

Failure rolls back all three. `(chain_id, asset, tx_hash)` and the credit
ledger reference are unique, so retries cannot double-credit. The transaction
sender must be the authenticated account's linked wallet and the ERC-20
transfer must be a direct transfer from that wallet to the configured treasury.

## API

- `GET /v1/account/deposits/config` - signed-in wallet, enabled assets,
  addresses, valuation terms, and limits for the Console.
- `GET /v1/account/deposits` - immutable funding history for the account.
- `POST /v1/account/deposits/claim` - claim direct Base USDC.
- `POST /v1/account/deposits/claim-aipg` - claim guarded Base AIPG.
- `POST /v1/account/deposits/claim-eth` - direct ETH pilot; unavailable unless
  the operator explicitly selects `buffered`.
- `POST /v1/account/deposits/claim-eth-converted` - credit actual canonical
  Base USDC delivered to the treasury by a linked-wallet ETH swap; available
  only in `swap_receipt` mode.

Each claim accepts `{ "tx_hash": "0x..." }`, waits for
`GRID_DEPOSIT_CONFIRMATIONS`, verifies that `GRID_BASE_RPC` reports the expected
chain id, and is safe to retry.

## USDC launch

Native Circle USDC on Base credits 1:1. Its six base-unit decimals are already
micro-USD, so there is no oracle or rounding conversion. Transaction,
account/day, and network/day caps bound operational mistakes and accounting
exposure.

```dotenv
GRID_DEPOSITS_ENABLED=1
GRID_USDC_TREASURY=0x...
GRID_BASE_RPC=https://...
GRID_DEPOSIT_CONFIRMATIONS=3
GRID_USDC_MAX_DEPOSIT_MICRO=10000000000
GRID_USDC_ACCOUNT_DAILY_MICRO=25000000000
GRID_USDC_NETWORK_DAILY_MICRO=100000000000
```

Roll out with a linked operator wallet and a small real transfer first. Verify
the Base transaction, `grid_deposits`, credit ledger, balance, duplicate-claim
no-op, and one paid inference reservation before exposing the button broadly.

## Guarded AIPG

The AIPG/USDC Base pool is too thin for a spot price to be a credit oracle.
Core therefore accepts no autonomous pool quote. An operator must publish a
conservative valuation epoch with an as-of time, expiry, and optional source
block. Core applies a further haircut and enforces transaction, account/day,
and network/day USD exposure caps under a database lock. The transfer itself
must be mined inside that epoch; historical treasury transfers cannot acquire
credit under a newer price.

```dotenv
GRID_AIPG_DEPOSITS_ENABLED=1
GRID_AIPG_TREASURY=0x...
GRID_AIPG_CREDIT_PRICE_MICRO=1200
GRID_AIPG_PRICE_EPOCH=2026-07-27-a
GRID_AIPG_PRICE_AS_OF=2026-07-27T13:00:00Z
GRID_AIPG_PRICE_VALID_UNTIL=2026-07-28T13:00:00Z
GRID_AIPG_PRICE_BLOCK=12345678
GRID_AIPG_DEPOSIT_HAIRCUT_BPS=300
GRID_AIPG_MAX_DEPOSIT_MICRO=100000000
GRID_AIPG_ACCOUNT_DAILY_MICRO=100000000
GRID_AIPG_NETWORK_DAILY_MICRO=500000000
```

`GRID_AIPG_CREDIT_PRICE_MICRO` is micro-USD per whole AIPG. The example is
$0.0012/AIPG before the 3% haircut; it is an example, not a live price. A missing,
future, stale, or expired epoch disables the rail with no fallback.

Received AIPG stays in the AIPG treasury for reviewed network uses such as
worker/validator rewards. The funding path does not market-sell it.

## ETH policy

The production ETH policy is **pay with ETH, credit actual USDC proceeds**.
With `GRID_ETH_CONVERSION_MODE=swap_receipt`, a linked wallet executes an ETH
swap whose recipient is the Grid's USDC treasury, then submits that transaction
hash. Core verifies the transaction spent native ETH, verifies canonical Base
USDC was delivered to the treasury, and credits exactly those six-decimal USDC
proceeds. The immutable receipt records ETH as the source asset, raw ETH value,
effective execution price, swap block/time, USDC-backed credited amount, and
the linked source/refund address.

Core intentionally does not choose or trust a DEX quote: a future Console quote
adapter may use a reviewed router, but the money invariant depends only on
confirmed canonical USDC received. This leaves no fixed-dollar liability backed
by volatile ETH and requires no ETH oracle in request billing. The same
per-transaction, per-account/day, and network/day ETH funding caps apply to the
actual USDC proceeds; an over-cap transfer is not silently credited and requires
operator-reviewed refund handling.

The existing direct-ETH verifier is retained only as a tightly capped
`GRID_ETH_CONVERSION_MODE=buffered` pilot. It applies a Chainlink valuation
haircut and the same transaction/account/network caps, but the operator still
owns treasury conversion risk. Keep it disabled for public launch. Even when
that operator-only claim path is enabled, the Console does not offer a direct
ETH transfer button.

## x402 agent payments

`POST /v1/x402/chat/completions` is the accountless agent rail. It is
code-complete but default-off. The client receives an x402 `402 Payment
Required`, authorizes up to `GRID_X402_MAX_AUTH_MICRO` in Base USDC, and retries
with the payment signature. Core then:

1. verifies the authorization through the configured facilitator;
2. writes an external reservation plus `grid_x402_payments` receipt before
   dispatch;
3. rejects requests whose maximum grid quote exceeds the signed ceiling;
4. settles worker-side usage from grid-counted prompt/completion tokens;
5. durably moves the receipt to `settling` with that exact amount before asking
   the facilitator to touch chain;
6. asks the facilitator to transfer only that actual amount; and
7. records facilitator success as `reported` with its transaction hash; then
8. independently verifies the exact canonical-USDC Base transfer before
   promoting it to `settled`.

No Grid account, API key, free allowance, promotional grant, or purchased
balance is created. An authorized-but-unsettled x402 job is excluded from worker
payout aggregation. This prevents a valid signature followed by failed
on-chain settlement from creating a payable worker reward.

The first route is deliberately non-streaming and text-only. x402's current
FastAPI middleware buffers the full response before settlement; advertising
streaming would turn SSE into a delayed response. Streaming, media, and
Anthropic/Responses compatibility require a separate stream-aware adapter.

```dotenv
GRID_X402_ENABLED=1
GRID_X402_NETWORK=eip155:8453
GRID_X402_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402
GRID_X402_PAY_TO=0x...
GRID_X402_MAX_AUTH_MICRO=1000000
GRID_X402_DEFAULT_MAX_TOKENS=4096
GRID_X402_STALE_SECONDS=900
CDP_API_KEY_ID=...
CDP_API_KEY_SECRET=...
```

An ambiguous facilitator/database outcome is never retried automatically.
`settling` and unproven `reported` rows age into `manual_review`, remain excluded
from worker payouts, and emit a critical operator alert. Reconcile only after
proving the exact canonical-USDC transfer from the recorded payer to recipient:

```bash
python -m grid_api.services.x402_payments \
  --reconcile-job <job-uuid> \
  --tx <base-transaction-hash>
```

The command verifies Base chain/confirmations, token, payer, recipient, and the
exact grid-counted amount before recording revenue. It is idempotent and rejects
a different transaction for an already settled job.

Base-mainnet startup fails closed without CDP facilitator credentials. Before
enabling, run a Base Sepolia end-to-end payment, prove actual-amount settlement,
exercise handler/facilitator/database failures, then run a small mainnet canary.
The implementation follows the x402 `upto` flow documented in the
[x402 seller quickstart](https://docs.x402.org/getting-started/quickstart-for-sellers)
and request-bound JWT authentication documented by
[Coinbase CDP](https://docs.cdp.coinbase.com/api-reference/v2/authentication).

## Rollout order

1. Deploy code with `GRID_DEPOSITS_ENABLED=0`,
   `GRID_AIPG_DEPOSITS_ENABLED=0`, `GRID_ETH_CONVERSION_MODE=disabled`, and
   `GRID_X402_ENABLED=0`.
2. Run Alembic through `0018`, then require `alembic check` to report no drift.
3. Configure dedicated monitored Base treasury addresses and an RPC with chain
   id `8453`. Do not reuse a payout hot-wallet private key in Core or Console.
4. Enable USDC only. Use a linked operator wallet for a minimum-size canary;
   prove one receipt, one credit movement, one balance increase, and a
   duplicate-claim no-op before exposing Console funding.
5. Keep AIPG dark until the price-epoch owner, expiry alert, refund owner, and
   low transaction/account/network caps are operational. The Console validates
   known minimum and per-transaction limits before opening a transfer.
6. Keep direct ETH out of the public Console. Integrate and review a quote/router
   adapter for `swap_receipt`; Core already credits only actual canonical USDC
   proceeds, independent of the quoted route.
7. Prove x402 on Base Sepolia, then run a low-ceiling Base mainnet canary.
   Worker payout must remain excluded until the USDC receipt is settled.
8. Keep the exact-transfer operator reconciler and low limits for canaries. Add
   an automated chain indexer/reconciler before raising limits or adding
   streaming/media routes. Card funding remains a later adapter into the same
   non-transferable credit ledger.

## Limitations

- V0 is claim-based rather than a chain indexer.
- Transfers from exchanges cannot be claimed because the transaction sender is
  not the linked wallet.
- Direct USDC/AIPG claims reject contract-routed token transfers. ETH
  `swap_receipt` may use a contract route because credit is based only on
  canonical USDC actually delivered to the treasury; the Console still needs a
  reviewed quote/router adapter and transaction-intent binding.
- AIPG price epochs are operational input. They require an owner, monitoring,
  and expiry automation before broad limits are raised.
- Card top-ups remain a future adapter into the account credit model.
- A post-settlement database failure can leave paid x402 revenue pending manual
  reconciliation. The API fails the response, flags the attempt, blocks worker
  payout, and provides an exact-transfer operator reconciler. An automated chain
  indexer/reconciler is required before raising x402 limits.

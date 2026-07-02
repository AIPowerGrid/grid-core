# Demand-side funding rail — USDC deposits → credits

Status: **USDC deposit rail built + deployed DORMANT (2026-07-02).** Activates when
a treasury address is configured. This is step 1 of the dollar loop
(see GRID_ECONOMICS.md; the buyback/token-sink is a separate design).

## The flow

1. User sends **USDC on Base** to the grid treasury address.
2. User calls `POST /v1/account/deposits/claim` `{ "tx_hash": "0x…" }` with their
   API key.
3. The grid (`services/deposits.py`) verifies on-chain via a Base RPC:
   - the tx succeeded and has ≥ `GRID_DEPOSIT_CONFIRMATIONS` confirmations,
   - it contains a **USDC Transfer to the treasury**,
   - the **sender == the account's SIWE wallet** (so a transfer can't be claimed
     by another account),
   then credits the balance **1:1** — USDC has 6 decimals, so its base-unit value
   IS micro-USD; no oracle, no conversion.
4. `credits.credit(ref="usdc:<tx>")` moves the balance and is **idempotent on the
   tx hash**, so a deposit can never double-credit.

Balance is then spent by the existing reservation/settlement metering
(`credits.py`, dark until `GRID_CHARGING_ENABLED=1`).

## Activate it (what's needed to go live)

Set on prod (`/etc/aipg/grid.env`) and restart:
```
GRID_DEPOSITS_ENABLED=1
GRID_USDC_TREASURY=0x…            # the Base wallet that receives user USDC (OWNER TO PROVIDE)
GRID_BASE_RPC=https://…           # a Base mainnet RPC (default: https://mainnet.base.org)
# optional: GRID_DEPOSIT_CONFIRMATIONS=3, GRID_USDC_CONTRACT=<override for testnet>
```
Until `GRID_USDC_TREASURY` is set the claim endpoint returns **503** (safe dormant).

## Design notes / limits

- **Self-custody only (V0):** the deposit must come FROM the account's linked
  wallet. Users paying from an exchange (different `from`) can't claim — they'll
  use the card/Stripe path (not built yet). Fine for the crypto-native early users.
- **Claim-flow, not a watcher:** V0 verifies a user-submitted tx hash rather than
  continuously indexing the chain. Simpler + robust; a background deposit watcher
  (auto-credit without the claim call) is a later upgrade.
- **Non-USDC (ETH/cbBTC/AIPG):** out of scope here — those swap to USDC at the
  door before crediting (a future deposit-widget/DEX step), per credits.py's note.

## What's next in the dollar loop

- **Card path (Stripe)** for the non-crypto majority → same credit balance.
- **Flip charging on** (`GRID_CHARGING_ENABLED=1`) after the pre-flight balance
  gate + den-input clamps + refund path land.
- **Payout + buyback sink:** revenue → worker USDC + revenue-funded AIPG buyback
  that pays the worker AIPG slice and burns the surplus (see the token-sink design).

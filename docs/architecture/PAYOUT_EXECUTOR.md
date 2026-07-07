# Multi-asset payout executor — leg-routed (Base + Stripe)

Status: **design / not built.** The distribution engine (`services/settlement/revenue.py`)
is done and dark; this is the *sender* that takes its output and moves the money.

## The model it serves

Pro-rata pass-through: each account earns its den-share of every asset pot
(`compute_multiasset_payouts` → `{account_id, payout_wallet, amounts: {USDC, ETH, AIPG}}`).
The executor's only job is to move each **leg** through the right rail. **No conversion**
happens in the executor — same basket in, same basket out.

## Two rails, one router

```
for account in payouts:
    for asset, amount in account.amounts.items():
        rail = rail_for(asset, account)      # picks Base or Stripe
        rail.pay(account, asset, amount, ref=(period_id, account_id, asset))
```

**`BaseRail`** (on-chain, = today's `payouts.py` generalized):
- ERC-20 transfer (AIPG, on-chain USDC) or native send (ETH) to `account.payout_wallet`.
- Nonce-bound, Transfer-proven, idempotent per `(period, account, asset)`. This is the
  existing custodial-payout machinery, just parameterized by token+native instead of
  AIPG-only.
- **You own compliance here:** OFAC-screen `payout_wallet` before sending; 1099 the USD
  FMV; hold the treasury keys.

**`StripeRail`** (fiat + USDC, the licensed leg — see PAYOUT_COMPLIANCE):
- Stripe **Connect** transfer / **Bridge** stablecoin payout to the account's
  `stripe_account_id`. Pays out in **fiat or USDC**, cross-border.
- **Stripe owns compliance here:** KYC of the payee, tax forms, money-transmission
  licensing. That's the whole reason to use it.
- Async: submit → store the Stripe transfer id → reconcile status via webhook.

## USDC is dual-rail (the useful nuance)

USDC is both a Stripe stablecoin *and* a Base ERC-20, so its leg can go **either** way,
chosen by the worker's payout preference:
- **"USDC to my bank / as fiat"** → `StripeRail` (Stripe KYC's + converts + pays).
- **"USDC to my wallet"** → `BaseRail` (plain Base USDC transfer, no Stripe needed).

ETH and AIPG have no Stripe path — always `BaseRail`.

So `rail_for(asset, account)`:
| asset | rail |
|---|---|
| ETH, AIPG | Base (to `payout_wallet`) |
| USDC | Stripe if `account.stripe_account_id` + prefers fiat/off-chain; else Base to `payout_wallet` |
| fiat leg (Stripe-collected card revenue) | Stripe → fiat/USDC |

## Identity: two payout targets per account

- `payout_wallet` (Base) — already built; for ETH/AIPG/on-chain-USDC.
- `stripe_account_id` (NEW) — the account's Stripe **connected account**; set via a
  Stripe Connect onboarding link (Stripe runs the KYC). Null → the USDC/fiat leg has no
  Stripe destination and **accrues** (same "no wallet → accrue" pattern the code already
  has), or falls back to on-chain USDC if a `payout_wallet` exists.

## Data model

Generalize the payout record from AIPG-only to **per leg**. Either add columns to
`grid_payouts` or add `grid_payout_legs`:

```
(period_id, account_id, asset, rail, amount, status, external_id, created, paid)
   external_id = tx_hash        (Base rail)
               = stripe_txfr_id (Stripe rail)
   idempotent on (period_id, account_id, asset)
```

`grid_revenue` (built) feeds the pots; `grid_payout_legs` records what went out. Both
key by the same `period_id` so the explorer/audit can reconcile revenue-in → paid-out
per asset.

## Idempotency & reconciliation

- Each leg keyed by `(period, account, asset)` — a re-run never double-pays (mirrors the
  current nonce/`grid_payouts` idempotency).
- Base rail: proven by the on-chain Transfer receipt (as today).
- Stripe rail: transfer id + webhook status (`paid` / `failed`); a failed leg re-queues.

## Fees

- **Stripe fees** (Connect payout + Bridge conversion) come out of that leg or the
  protocol slice — model in `economics.py`.
- **On-chain gas** comes from the treasury (as today).
- Neither is a per-worker conversion fee — we abandoned that; the pass-through model
  charges no conversion fee (see PAYOUT_COMPLIANCE / the economics note).

## Compliance split (why the hybrid is the point)

| leg | KYC | tax (1099) | licensing | OFAC screen |
|---|---|---|---|---|
| Stripe (fiat/USDC) | Stripe | Stripe | Stripe | Stripe |
| Base (ETH/AIPG/USDC) | you | you | (distribution, not transmission) | **you** |

The regulated-heavy fiat/USDC volume rides Stripe's licenses; you keep only the
low-liability on-chain pass-through legs.

## Phasing

1. **Base rail multi-asset** — generalize `payouts.py` to send N assets (AIPG + ETH +
   on-chain USDC) by the engine's amounts + `grid_payout_legs`. No Stripe. + **OFAC
   screening** on `payout_wallet`. This is buildable now (gated on treasury + charging).
2. **Intake feed** — wire `record_revenue` to consumption (the one money-policy call).
3. **Stripe rail** — once the Stripe/Bridge account is approved: `stripe_account_id`,
   Connect onboarding, the `StripeRail` adapter + webhook reconcile.

Gated on: funded treasury (Base), charging live, and the Stripe underwriting conversation.

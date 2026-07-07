# ⚠️ CONFIG-OF-RECORD / NOT YET CONSUMED (2026-06-23): this captures the money-split
# decisions as tunable knobs + pure helpers. The settlement/payout path that will call
# these is still a stub (services/settlement/bot.py) — wire it intentionally (+ tests)
# at worker-payout go-live. Nothing here moves money on its own. See tasks #72/#73.

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Grid economics — how customer revenue splits, and how workers get paid.

The model (decided 2026-06-23):

* CUSTOMERS pay in USD (USDC primary; ETH/cbBTC swapped to USDC at the door;
  AIPG accepted at the peg, optionally with a bonus). Credits are micro-USD.
* Revenue splits 85 / 3 / 12 — generator (worker) / sentinel / protocol — the
  same split as docs/architecture/ECONOMICS.md. All three are knobs below.
* WORKERS are paid mostly in USDC plus a slice of AIPG (WORKER_AIPG_SHARE_BPS).
  They live off the USDC, so the AIPG isn't rent money they're forced to dump —
  lower sell pressure, long-term alignment. The AIPG slice is valued at payout
  time (market price), so a worker always receives their full den-share value.
* The AIPG slice is SOURCED from (a) AIPG taken in via AIPG-denominated payments
  and a founder/treasury GRANT to the reward pool, then (b) topped up by ON-MARKET
  buybacks with a portion of USDC revenue. The protocol is a net AIPG *buyer*,
  never a seller — it never swaps AIPG→USDC, and never routes protocol revenue to
  a founder wallet in exchange for tokens (that would be founder-dump, not buy
  pressure). Buybacks are TWAP'd + capped per epoch so thin AIPG liquidity can't
  wreck fills; the cap quietly leans payouts USDC-heavy when a buy would move the
  market too far.

Launch posture (safe defaults below): accept USD, pay workers 100% USDC
(WORKER_AIPG_SHARE_BPS=0), AIPG bonus + buyback OFF. Turn the AIPG slice +
buyback on once payout volume exists and AIPG liquidity can absorb it.

Everything is integer micro-USD (USD × 1e6) and basis points (1% = 100 bps), so
splits are exact — no float drift on the money path.
"""

import os

BPS = 10_000  # basis-point denominator (100% = 10_000 bps)


def _env_bps(name: str, default: int) -> int:
    """Read a bps knob from env, clamped to [0, 10000]."""
    try:
        return max(0, min(BPS, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


# ── Revenue split (must sum to <= 100%; worker pool is the remainder) ──
# Target is 85/3/12. Take rate is a growth dial at launch — running the protocol
# fee lower early (e.g. GRID_PROTOCOL_FEE_BPS=300) is a supply-acquisition wedge;
# it ratchets UP toward 1200 gracefully as volume/lock-in grow, never down.
PROTOCOL_FEE_BPS = _env_bps("GRID_PROTOCOL_FEE_BPS", 1200)  # 12% → protocol
SENTINEL_FEE_BPS = _env_bps("GRID_SENTINEL_FEE_BPS", 300)   # 3%  → verification

# ── Worker payout currency split ──
# Share of a worker's payout VALUE paid in AIPG (the rest in USDC). Launch=0
# (all USDC); target 5000 (50/50). The buyback cap auto-throttles this down when
# AIPG liquidity is thin, so it's safe to target high.
WORKER_AIPG_SHARE_BPS = _env_bps("GRID_WORKER_AIPG_SHARE_BPS", 0)

# ── Payout asset choice + conversion fee (P1: preference storage only; the
#    batched DEX swap that consumes it is P2) ──
# A worker picks the asset they're paid in. USDC (treasury-held) and AIPG (the
# flywheel asset) are PAR — delivered fee-free. USDS/ETH require a batched swap
# out of treasury USDC, so they carry the conversion fee below.
PAYOUT_ASSETS = ("USDC", "USDS", "ETH", "AIPG")
PAYOUT_PAR_ASSETS = ("USDC", "AIPG")  # fee-free
DEFAULT_PAYOUT_ASSET = os.getenv("GRID_DEFAULT_PAYOUT_ASSET", "USDC").upper()
# Fee (bps) on converting a payout into a NON-par asset. Covers the batched swap
# (DEX fee + gas + slippage) plus a thin margin; MUST stay below what a worker
# would pay swapping solo, or they'll just take free USDC and DIY. Batching per
# period is what makes that inequality hold. Fees route to the AIPG buyback when
# GRID_PAYOUT_FEE_TO_BUYBACK is on (net-buyer flywheel).
PAYOUT_CONVERSION_FEE_BPS = _env_bps("GRID_PAYOUT_CONVERSION_FEE_BPS", 50)  # 0.5%
PAYOUT_FEE_TO_BUYBACK = os.getenv("GRID_PAYOUT_FEE_TO_BUYBACK", "1").lower() in ("1", "true", "yes", "on")
# Whether the payout RAIL actually consumes payout_asset yet (the P2 batched
# swap). OFF until then — the live rail (payouts.py) still settles a fixed AIPG
# budget by den. The account API surfaces this as payout.active so nothing
# implies USDC/ETH/USDS payouts before the swap ships.
PAYOUT_ASSET_ROUTING_ENABLED = os.getenv("GRID_PAYOUT_ASSET_ROUTING", "0").lower() in ("1", "true", "yes", "on")


def is_par_asset(asset: str | None) -> bool:
    """Par assets deliver the owed USD value 1:1, fee-free."""
    return (asset or "").upper() in PAYOUT_PAR_ASSETS


def conversion_fee_bps(asset: str | None) -> int:
    """0 for par assets (USDC/AIPG); the conversion fee for swap assets (USDS/ETH)."""
    return 0 if is_par_asset(asset) else PAYOUT_CONVERSION_FEE_BPS

# ── Customer incentive to pay in AIPG ──
# Extra credit granted for paying a top-up in AIPG (drives customer-side AIPG
# demand). A real revenue haircut — keep modest. Launch=0 (off).
AIPG_PAYMENT_BONUS_BPS = _env_bps("GRID_AIPG_PAYMENT_BONUS_BPS", 0)

# ── Buyback (on-market AIPG buys to source the payout slice / add buy pressure) ──
BUYBACK_ENABLED = os.getenv("GRID_BUYBACK_ENABLED", "0").lower() in ("1", "true", "yes")
# Hard ceiling on USDC spent buying AIPG per settlement epoch (micro-USD). The
# overflow is paid to workers in USDC instead, so a thin book can't force bad
# fills. Default 0 = no buyback budget until set.
BUYBACK_MAX_PER_EPOCH_MICRO = int(os.getenv("GRID_BUYBACK_MAX_PER_EPOCH_MICRO", "0"))


def _bps_of(amount_micro: int, bps: int) -> int:
    """Floor(amount * bps / 10000) in integer micro units — no float drift."""
    return (int(amount_micro) * bps) // BPS


def protocol_fee_micro(gross_micro: int) -> int:
    """Protocol's cut of gross revenue, in micro-USD."""
    return _bps_of(gross_micro, PROTOCOL_FEE_BPS)


def sentinel_fee_micro(gross_micro: int) -> int:
    """Sentinel (verification) cut of gross revenue, in micro-USD."""
    return _bps_of(gross_micro, SENTINEL_FEE_BPS)


def worker_pool_micro(gross_micro: int) -> int:
    """What's left for generators after protocol + sentinel cuts (micro-USD).

    Computed as the remainder (not a third bps cut) so the three parts always
    sum back to exactly `gross_micro` — no rounding dust escapes the split.
    """
    g = int(gross_micro)
    return g - protocol_fee_micro(g) - sentinel_fee_micro(g)


def split_revenue(gross_micro: int) -> dict:
    """Full split of one gross amount → {protocol, sentinel, worker} micro-USD.
    The three values sum to exactly gross_micro."""
    g = int(gross_micro)
    protocol = protocol_fee_micro(g)
    sentinel = sentinel_fee_micro(g)
    return {"protocol": protocol, "sentinel": sentinel, "worker": g - protocol - sentinel}


def split_worker_payout(value_micro: int) -> dict:
    """A worker's payout value → {usdc, aipg_value} micro-USD.

    `aipg_value` is the USD VALUE to deliver in AIPG (the settlement layer
    converts to an AIPG amount at payout-time market price); `usdc` is paid
    directly. Sums to exactly value_micro.
    """
    v = int(value_micro)
    aipg_value = _bps_of(v, WORKER_AIPG_SHARE_BPS)
    return {"usdc": v - aipg_value, "aipg_value": aipg_value}


def aipg_payment_bonus_micro(paid_micro: int) -> int:
    """Bonus credit (micro-USD) granted on top of an AIPG-denominated top-up."""
    return _bps_of(paid_micro, AIPG_PAYMENT_BONUS_BPS)

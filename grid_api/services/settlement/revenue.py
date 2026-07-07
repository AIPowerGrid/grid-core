# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pro-rata pass-through payouts — distribute the revenue BASKET, no conversion.

Take in what customers actually pay (USDC / ETH / AIPG, per asset), keep the
protocol + sentinel slices, and pay each earning account its den-share of EACH
asset pot. A worker who did 10% of the period's den gets 10% of the USDC pot,
10% of the ETH pot, and 10% of the AIPG pot — the same basket that came in, in
the form it arrived.

Why this shape:
* No swaps, no USD target, no treasury FX position → workers share the real
  revenue basket, and the grid never converts one asset to another on anyone's
  behalf. That "distribution, not conversion" property is what keeps it out of
  exchange / money-transmitter territory (see payout-compliance notes). It also
  reinforces the independent-participant framing (share of proceeds, not a wage).
* AIPG reaches workers only when CUSTOMERS choose to pay in AIPG — organic, no
  treasury market-buying, no wash trade.

⚠️ THE INTAKE FEED IS THE ONE MONEY-POLICY DECISION — and it's gated on live
charging. The pool must be EARNED revenue (credits CONSUMED), attributed to the
asset it was paid in (deposit-lineage), NOT raw deposits: paying from prepaid
deposits would over-distribute for a whale who deposits but barely spends.
`record_revenue()` is the append point; wire it to the consumption/settlement
path when charging goes live. Dark until then — no treasury + charging off = the
pool is empty, so preview shows zero and nothing is paid. The DISTRIBUTION math
below is asset-agnostic and fully tested regardless.
"""

import logging
from decimal import Decimal

import sqlalchemy as sa

from ...database import new_session
from ...v2.schema import revenue as revenue_t
from .. import economics

logger = logging.getLogger("grid_api.revenue")


# ── intake (append-only, idempotent on ref) ──────────────────────────────────

async def record_revenue(period_id: str, asset: str, amount, source: str, ref: str) -> bool:
    """Append `amount` of `asset` to the distributable pool for `period_id`.
    Idempotent on `ref` (the charge/settlement id) — a retried recognition never
    double-counts. Returns True if a new row was written, False on duplicate.

    `source` documents what recognized it ('consumption' when charging is live;
    'deposit'/'manual' only for bootstrap/backfill — see the module note)."""
    if amount is None or Decimal(str(amount)) <= 0 or not ref or not asset:
        return False
    async with await new_session() as s:
        try:
            await s.execute(
                sa.insert(revenue_t).values(
                    period_id=period_id, asset=asset.upper(),
                    amount=Decimal(str(amount)), source=source, ref=ref,
                )
            )
            await s.commit()
            return True
        except sa.exc.IntegrityError:
            await s.rollback()
            return False  # duplicate ref — already counted


async def revenue_pots(period_id: str) -> dict[str, float]:
    """Gross revenue per asset for a period: {asset -> total native units}."""
    async with await new_session() as s:
        rows = (
            await s.execute(
                sa.select(revenue_t.c.asset, sa.func.sum(revenue_t.c.amount))
                .where(revenue_t.c.period_id == period_id)
                .group_by(revenue_t.c.asset)
            )
        ).all()
    return {asset: float(total) for asset, total in rows if total}


def worker_pots(gross_pots: dict[str, float]) -> dict[str, float]:
    """Apply the worker share (revenue minus protocol + sentinel) to each asset
    pot. The protocol/sentinel slices stay in the treasury in that same asset."""
    return {a: economics.worker_share_of(g) for a, g in gross_pots.items() if g > 0}


# ── distribution (pure — the tested core) ─────────────────────────────────────

def compute_multiasset_payouts(rows: list[dict], pots: dict[str, float], *, dust: float = 1e-12) -> list[dict]:
    """Split each asset pot across accounts pro-rata by den. `pots` are the
    WORKER pots (already net of the protocol slice). Returns
    [{account_id, payout_address, den, share, amounts:{asset: amt}, payable}]
    sorted by den desc; an account with no wallet is `payable=False` (accrues).

    Pure + deterministic: same den + pots → same split. Conservation holds — the
    per-asset sum of `amounts` over all rows equals that pot (modulo float dust)."""
    total_den = sum(float(r["den"]) for r in rows)
    if total_den <= 0 or not pots:
        return []
    out = []
    for r in rows:
        share = float(r["den"]) / total_den
        amounts = {a: pot * share for a, pot in pots.items() if pot * share > dust}
        if not amounts:
            continue
        addr = r.get("payout_address")
        out.append({
            "account_id": r["account_id"],
            "payout_address": addr,
            "den": float(r["den"]),
            "share": share,
            "amounts": amounts,
            "payable": bool(addr),
        })
    return sorted(out, key=lambda x: x["den"], reverse=True)


async def preview_multiasset(period_id: str, rows: list[dict]) -> dict:
    """Dry-run: the per-asset pots for a period and how they'd split by den.
    `rows` = aggregate_den_by_account output (account_id, den, payout_address)."""
    gross = await revenue_pots(period_id)
    pots = worker_pots(gross)
    pay = compute_multiasset_payouts(rows, pots)
    assets = sorted(set(gross) | {a for p in pay for a in p["amounts"]})
    return {
        "period_id": period_id,
        "gross_pots": gross,
        "worker_pots": pots,
        "worker_share_bps": economics.worker_share_bps(),
        "assets": assets,
        "accounts": len(rows),
        "total_den": sum(float(r["den"]) for r in rows),
        "payouts": pay,
        "n_payable": sum(1 for p in pay if p["payable"]),
    }

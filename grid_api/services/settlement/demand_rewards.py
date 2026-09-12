# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Demand-backed emission ceilings. No sender, exchange, or market-price claim."""

from decimal import Decimal, ROUND_DOWN, localcontext
from uuid import UUID


def bound_allocations(allocations, purchased_micro, *, price_micro_per_aipg, worker_share_bps):
    """Clip each DEN allocation to its own purchased work; never redistribute.

    The caller must bind an approved valuation window into the frozen period.
    A configured valuation is not proof of token market value or Sybil safety.
    Return Decimal money so the cap cannot round upward through a float.
    """
    if type(price_micro_per_aipg) is not int or not 1 <= price_micro_per_aipg <= 10**15:
        raise ValueError("invalid emission valuation")
    if type(worker_share_bps) is not int or not 0 < worker_share_bps < 10000:
        raise ValueError("invalid emission worker share")
    backing = {}
    for key, value in purchased_micro.items():
        canonical = str(UUID(str(key)))
        if canonical in backing or type(value) is not int or not 0 <= value <= 10**28:
            raise ValueError("invalid or duplicate purchased-work backing")
        backing[canonical] = value
    seen, out = set(), []
    with localcontext() as ctx:
        ctx.prec = 60
        for row in allocations:
            key = str(UUID(str(row["account_id"])))
            value = Decimal(str(row["aipg"]))
            if key in seen or not value.is_finite() or not 0 <= value <= 10**28:
                raise ValueError("invalid or duplicate emission allocation")
            seen.add(key)
            consumed = backing.get(key, 0)
            cap = Decimal(consumed * worker_share_bps) / (10000 * price_micro_per_aipg)
            bounded = min(value, cap).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if bounded > 0:
                out.append({**row, "aipg": bounded, "purchased_micro": consumed})
    return out

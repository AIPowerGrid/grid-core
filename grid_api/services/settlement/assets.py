# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Payout asset registry — how each payout asset moves on the Base rail.

The pass-through payout distributes native amounts of USDC / ETH / AIPG; the
sender needs, per asset, whether it's an ERC-20 (contract + decimals) or the
native coin. Addresses default to Base mainnet and are env-overridable. Stripe
handles fiat/USDC off-chain separately (see PAYOUT_EXECUTOR.md) — this registry
is only the on-chain rail.
"""

import os

# Base mainnet defaults (override via env).
AIPG_ADDRESS = os.getenv("AIPG_TOKEN_ADDRESS", "0xa1c0deCaFE3E9Bf06A5F29B7015CD373a9854608")
USDC_ADDRESS = os.getenv("USDC_TOKEN_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")

# kind: "erc20" (transfer via contract) | "native" (value send). amounts are in
# whole units of the asset; the sender scales by `decimals`.
ASSET_SPECS: dict[str, dict] = {
    "AIPG": {"kind": "erc20", "address": AIPG_ADDRESS, "decimals": 18},
    "USDC": {"kind": "erc20", "address": USDC_ADDRESS, "decimals": 6},
    "ETH":  {"kind": "native", "address": None, "decimals": 18},
}


def spec(asset: str) -> dict | None:
    return ASSET_SPECS.get((asset or "").upper())


def supported(asset: str) -> bool:
    return (asset or "").upper() in ASSET_SPECS


def to_base_units(asset: str, amount: float) -> int:
    """Whole-unit amount → integer base units (wei-equivalent) for this asset."""
    s = spec(asset)
    if not s:
        raise ValueError(f"unknown payout asset {asset!r}")
    return int(round(amount * (10 ** s["decimals"])))

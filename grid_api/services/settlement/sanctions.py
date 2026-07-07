# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OFAC / sanctions screening for payout addresses.

Before ANY payout leaves the treasury, the destination address is screened.
Paying a sanctioned wallet is strict-liability bad, independent of whether we're
an MSB — so this gates the on-chain rail (and any future rail) regardless.

Two layers, cheapest first:
1. **Local denylist** — `GRID_SANCTIONS_DENYLIST` (comma-separated addresses),
   always authoritative. Zero I/O, always available. Seed it with the OFAC SDN
   crypto addresses (and anything you learn of); refresh out of band.
2. **On-chain oracle** (optional) — the industry-standard Chainalysis sanctions
   oracle `isSanctioned(address)`, if `GRID_SANCTIONS_ORACLE` (+ an RPC) is set.
   Covers the maintained SDN set without you curating it.

Fail posture is FAIL-CLOSED for money: a hit blocks; and if the oracle is
configured but unreachable, we return `error` so the caller HOLDS the payout for
review rather than paying blind. A clear screen requires either "no oracle
configured" (denylist-only mode) or a definitive oracle "not sanctioned".
"""

import logging
import os

logger = logging.getLogger("grid_api.sanctions")


def _denylist() -> set[str]:
    raw = os.getenv("GRID_SANCTIONS_DENYLIST", "")
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


ORACLE_ADDRESS = os.getenv("GRID_SANCTIONS_ORACLE", "")  # Chainalysis oracle, if used
ORACLE_RPC = os.getenv("GRID_SANCTIONS_ORACLE_RPC", "") or os.getenv("BASE_RPC_URL", "")

# Minimal ABI for the Chainalysis sanctions oracle.
_ORACLE_ABI = [{
    "name": "isSanctioned", "type": "function", "stateMutability": "view",
    "inputs": [{"name": "addr", "type": "address"}],
    "outputs": [{"name": "", "type": "bool"}],
}]


def denylisted(address: str) -> bool:
    """Local denylist check — no I/O, always authoritative."""
    return bool(address) and address.strip().lower() in _denylist()


def _oracle_configured() -> bool:
    return bool(ORACLE_ADDRESS and ORACLE_RPC)


async def _oracle_sanctioned(address: str) -> bool | None:
    """True/False from the on-chain oracle, or None if not configured / unreachable."""
    if not _oracle_configured():
        return None
    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(ORACLE_RPC))
        c = w3.eth.contract(address=Web3.to_checksum_address(ORACLE_ADDRESS), abi=_ORACLE_ABI)
        return bool(c.functions.isSanctioned(Web3.to_checksum_address(address)).call())
    except Exception as e:
        logger.warning("sanctions oracle unreachable for %s: %s", address, e)
        return None


async def screen(address: str) -> dict:
    """Screen a payout address. Returns
    {address, sanctioned: bool, source: 'denylist'|'oracle'|'clear', hold: bool}.

    `hold=True` means "don't pay, needs manual review" — used when the oracle is
    configured but couldn't give a definitive answer (fail-closed on money)."""
    if not address:
        # Not sanctioned — just unpayable. Hold rather than send to nowhere.
        return {"address": address, "sanctioned": False, "source": "empty", "hold": True}
    addr = address.strip().lower()
    if addr in _denylist():
        return {"address": addr, "sanctioned": True, "source": "denylist", "hold": False}
    oracle = await _oracle_sanctioned(address)
    if oracle is True:
        return {"address": addr, "sanctioned": True, "source": "oracle", "hold": False}
    if oracle is None and _oracle_configured():
        # oracle wanted, but unreachable → don't pay blind
        return {"address": addr, "sanctioned": False, "source": "error", "hold": True}
    return {"address": addr, "sanctioned": False, "source": "clear", "hold": False}


def payable_status(result: dict) -> str | None:
    """Map a screen() result to a terminal payout status if it must NOT be paid,
    else None (clear to pay). Keeps the money path from ever sending on a hit or
    an unverified address."""
    if result.get("sanctioned"):
        return "blocked_sanctions"
    if result.get("hold"):
        return "review_sanctions"
    return None

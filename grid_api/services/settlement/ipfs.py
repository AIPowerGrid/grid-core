# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""IPFS pinning helper for the settlement bot.

Pins the canonical [worker, den] list for a settlement period to an IPFS
pinning service (Pinata, Web3.Storage, or self-hosted) and returns an
`ipfs://<cid>` URI.

The URI is included in the on-chain `DenReport` so anyone can independently:
  1. Fetch the JSON via any IPFS gateway
  2. Rebuild the Merkle tree
  3. Verify the on-chain root matches

This is the only piece worth lifting from `grid-rewards-sentry/main.py`'s
`web3_base` branch — the rest of that branch (mint-based payouts, single hot
wallet) was rejected for the fixed-supply / treasury-funded model.
"""

import json
import logging
import os
from typing import Iterable, Optional, TypedDict

import httpx

logger = logging.getLogger("grid_api.settlement.ipfs")


class WorkerDenEntry(TypedDict):
    address: str  # 0x-prefixed checksum address
    den: int      # raw integer den earned this period


class SettlementSnapshot(TypedDict):
    period_id: int
    period_length_seconds: int
    total_den: int
    pool_allocation_wei: int
    entries: list[WorkerDenEntry]
    timestamp: str  # ISO8601 UTC


async def pin_settlement_snapshot(
    snapshot: SettlementSnapshot,
    *,
    pin_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout_seconds: float = 30.0,
) -> str:
    """Pin a period's full settlement snapshot to IPFS.

    Returns "ipfs://<cid>" on success, or "" on failure. The settlement bot
    should NOT abort a settlement on IPFS failure — the on-chain commit is the
    source of truth; IPFS is auditability nice-to-have. Just log loudly and
    submit with an empty URI.

    Defaults to env vars `IPFS_PIN_URL` and `IPFS_API_KEY` so the bot can be
    configured without code changes.

    Compatible response formats:
      - Pinata:        {"IpfsHash": "Qm...", ...}
      - Web3.Storage:  {"cid": "bafy..."}
      - Self-hosted:   {"cid": "..."}
    """
    pin_url = pin_url or os.getenv("IPFS_PIN_URL")
    api_key = api_key or os.getenv("IPFS_API_KEY")

    if not pin_url:
        logger.warning("IPFS_PIN_URL not configured; settlement will commit without audit URI")
        return ""

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(pin_url, json=snapshot, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        logger.warning(f"IPFS pin failed (continuing without URI): {e}")
        return ""

    cid = data.get("cid") or data.get("IpfsHash") or data.get("Hash")
    if not cid:
        logger.warning(f"IPFS pin returned no CID; response keys: {list(data.keys())}")
        return ""

    return f"ipfs://{cid}"


def build_settlement_snapshot(
    period_id: int,
    period_length_seconds: int,
    pool_allocation_wei: int,
    entries: Iterable[WorkerDenEntry],
    timestamp_iso: str,
) -> SettlementSnapshot:
    """Build a canonical settlement snapshot ready for IPFS pinning.

    Entries are sorted by address (lowercased for stable ordering) so the
    resulting JSON is deterministic — independent verifiers building the same
    snapshot from the same DB rows get bit-identical output.
    """
    sorted_entries = sorted(
        ({"address": e["address"], "den": int(e["den"])} for e in entries),
        key=lambda e: e["address"].lower(),
    )
    total_den = sum(e["den"] for e in sorted_entries)

    return SettlementSnapshot(
        period_id=period_id,
        period_length_seconds=period_length_seconds,
        total_den=total_den,
        pool_allocation_wei=pool_allocation_wei,
        entries=sorted_entries,
        timestamp=timestamp_iso,
    )


def settlement_snapshot_json(snapshot: SettlementSnapshot) -> str:
    """Canonical JSON serialization. `sort_keys=True` so the same snapshot
    always serializes to the same bytes (which is what IPFS hashes)."""
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))

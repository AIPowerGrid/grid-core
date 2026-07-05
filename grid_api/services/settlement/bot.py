# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Settlement bot — STUB (FUTURE trustless rail, NOT the live payout system).

Workers are paid TODAY by the custodial CLI `payouts.py` in this same package
(hourly `aipg-payout` systemd timer; treasury hot wallet sends AIPG pro-rata to
den). This bot is its planned trustless replacement (Merkle root → on-chain
report → worker-called claim) and is not deployed. See GO_LIVE.md.

Runs a daily loop:
  1. Wait until the current period ends (today 23:59:59 UTC, default)
  2. Aggregate worker den from the system-core DB for the closed period
  3. Build the canonical snapshot, pin to IPFS
  4. Build the Merkle tree and submit `DenReporter.reportPeriod(...)` via the
     team multisig (Safe Transaction Service)
  5. Paginate workers into batches of <= 200 and submit `PaymentRouter.claimBatch(...)`
     from the bot's hot wallet (gas only — no funds)
  6. Persist per-period state so a crash/restart doesn't double-report or skip

This file is a stub. The shape of the loop is real; the integrations with the
DB schema, Safe multisig, and contract addresses are marked TODO. They depend on:
  - Final DB schema for per-period den aggregation (not yet written)
  - Diamond deployment address on Base mainnet (not yet deployed)
  - Team multisig address + Safe API setup (not yet done)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .ipfs import (
    SettlementSnapshot,
    WorkerDenEntry,
    build_settlement_snapshot,
    pin_settlement_snapshot,
)
from .merkle import MerkleTree, build_tree

logger = logging.getLogger("grid_api.settlement.bot")


# ============ CONFIG ============

PERIOD_LENGTH_SECONDS = int(os.getenv("SETTLEMENT_PERIOD_SECONDS", "86400"))     # 1 day default
BATCH_SIZE = int(os.getenv("SETTLEMENT_BATCH_SIZE", "150"))                       # <= 200 (contract cap)
MAX_GWEI = float(os.getenv("MAX_GWEI", "0.06"))                                   # Base gas cap
SAFE_BACKOFF_SECONDS = int(os.getenv("SETTLEMENT_GAS_BACKOFF", "60"))             # retry wait when gas is too high
GRID_DIAMOND_ADDRESS = os.getenv("GRID_DIAMOND_ADDRESS", "")                      # TODO: set after deployment
SAFE_ADDRESS = os.getenv("SETTLEMENT_SAFE_ADDRESS", "")                           # TODO: team multisig
BOT_HOT_WALLET_PK = os.getenv("SETTLEMENT_BOT_PK", "")                            # TODO: gas wallet (no funds)


# ============ MAIN LOOP ============


async def run() -> None:
    """Settlement loop. Sleep until next period boundary, settle, repeat."""
    logger.info(
        "settlement bot starting; period=%ds batch_size=%d max_gwei=%.4f",
        PERIOD_LENGTH_SECONDS,
        BATCH_SIZE,
        MAX_GWEI,
    )

    if not _config_ok():
        logger.error("settlement bot misconfigured; refusing to start (see env vars)")
        return

    while True:
        try:
            period_id = _current_period_id()
            last_settled = await _load_last_settled_period_id()

            # If we missed periods (downtime, etc.), catch up one at a time.
            target = last_settled + 1 if last_settled is not None else period_id - 1
            if target < period_id:
                logger.info("settling closed period %d (current=%d)", target, period_id)
                await _settle_period(target)
                await _save_last_settled_period_id(target)
                continue   # don't sleep yet — there may be more catch-up to do

            # Caught up. Sleep until the current period ends.
            wait_seconds = _seconds_until_period_end(period_id)
            logger.info("caught up; sleeping %ds until period %d ends", wait_seconds, period_id)
            await asyncio.sleep(max(wait_seconds, 1))

        except Exception:
            logger.exception("settlement loop error; backing off 60s")
            await asyncio.sleep(60)


async def _settle_period(period_id: int) -> None:
    """Settle a single closed period end-to-end."""
    logger.info("[period %d] aggregating worker den", period_id)
    entries = await _aggregate_den_for_period(period_id)
    if not entries:
        logger.info("[period %d] no den earned; skipping", period_id)
        return

    pool_allocation = await _get_period_allocation_wei()
    if pool_allocation == 0:
        logger.warning("[period %d] period allocation is zero; skipping", period_id)
        return

    # 1. Build canonical snapshot
    snapshot = build_settlement_snapshot(
        period_id=period_id,
        period_length_seconds=PERIOD_LENGTH_SECONDS,
        pool_allocation_wei=pool_allocation,
        entries=entries,
        timestamp_iso=datetime.now(timezone.utc).isoformat(),
    )

    # 2. Pin to IPFS (best-effort)
    logger.info("[period %d] pinning %d entries to IPFS", period_id, len(snapshot["entries"]))
    ipfs_uri = await pin_settlement_snapshot(snapshot)
    if not ipfs_uri:
        logger.warning("[period %d] IPFS pin failed; submitting with empty URI", period_id)

    # 3. Build Merkle tree
    tree = build_tree((e["address"], e["den"]) for e in snapshot["entries"])
    logger.info("[period %d] merkle root=0x%s", period_id, tree.root.hex())

    # 4. Submit reportPeriod via team multisig
    await _submit_report_period(
        period_id=period_id,
        den_root=tree.root,
        total_den=snapshot["total_den"],
        ipfs_uri=ipfs_uri,
    )

    # 5. Paginate and submit claimBatch from bot hot wallet
    await _push_payouts(period_id, tree)

    logger.info("[period %d] settled %d workers", period_id, len(tree.entries))


async def _push_payouts(period_id: int, tree: MerkleTree) -> None:
    """Submit PaymentRouter.claimBatch in chunks of BATCH_SIZE, respecting MAX_GWEI."""
    entries = tree.entries
    for offset in range(0, len(entries), BATCH_SIZE):
        chunk = entries[offset : offset + BATCH_SIZE]

        # Respect gas cap. Loop until the network is cheap enough.
        while True:
            gwei = await _current_base_fee_gwei()
            if gwei <= MAX_GWEI:
                break
            logger.info(
                "[period %d] base fee %.4f gwei > cap %.4f; sleeping %ds",
                period_id, gwei, MAX_GWEI, SAFE_BACKOFF_SECONDS,
            )
            await asyncio.sleep(SAFE_BACKOFF_SECONDS)

        workers = [e.address for e in chunk]
        den = [e.den for e in chunk]
        proofs = [e.proof for e in chunk]

        logger.info(
            "[period %d] claimBatch offset=%d size=%d", period_id, offset, len(chunk),
        )
        await _submit_claim_batch(period_id, workers, den, proofs)


# ============ TIME HELPERS ============


def _current_period_id() -> int:
    return int(datetime.now(timezone.utc).timestamp()) // PERIOD_LENGTH_SECONDS


def _seconds_until_period_end(period_id: int) -> int:
    end_ts = (period_id + 1) * PERIOD_LENGTH_SECONDS
    return max(end_ts - int(datetime.now(timezone.utc).timestamp()), 0)


# ============ CONFIG VALIDATION ============


def _config_ok() -> bool:
    missing = []
    if not GRID_DIAMOND_ADDRESS:
        missing.append("GRID_DIAMOND_ADDRESS")
    if not SAFE_ADDRESS:
        missing.append("SETTLEMENT_SAFE_ADDRESS")
    if not BOT_HOT_WALLET_PK:
        missing.append("SETTLEMENT_BOT_PK")
    if missing:
        logger.error("missing required env vars: %s", ", ".join(missing))
        return False
    return True


# ============ TODO: INTEGRATIONS ============


async def _aggregate_den_for_period(period_id: int) -> list[WorkerDenEntry]:
    """TODO: pull rolled-up [worker_address, sum_den] for `period_id` from DB.

    Likely query (sketch — actual table TBD):
        SELECT worker_address, SUM(den) AS den
        FROM den_events
        WHERE period_id = :period_id AND den > 0
        GROUP BY worker_address
        ORDER BY worker_address;

    Note: `den_events` doesn't exist yet. Either add a roll-up trigger to the
    existing job logging, or compute on the fly with `WHERE created_at
    BETWEEN :start AND :end` against the per-job table.
    """
    raise NotImplementedError("DB schema for per-period den aggregation not yet defined")


async def _get_period_allocation_wei() -> int:
    """TODO: read `RewardPool.periodAllocation()` from the Diamond.

    Cheap eth_call; cache for the loop iteration. Returns wei (token has 18 dec).
    """
    raise NotImplementedError("Web3 client wiring not yet added")


async def _current_base_fee_gwei() -> float:
    """TODO: read current Base fee from the Base RPC and return as gwei.

    Cheap eth_call; throttle to once per ~10s to avoid hammering the RPC.
    """
    raise NotImplementedError("Web3 client wiring not yet added")


async def _submit_report_period(
    *,
    period_id: int,
    den_root: bytes,
    total_den: int,
    ipfs_uri: str,
) -> None:
    """TODO: submit DenReporter.reportPeriod(...) via Safe Transaction Service.

    1. Build the calldata against the Diamond address.
    2. Propose the tx to the team Safe via safe-eth-py.
    3. Poll for execution. On success, log the tx hash; on failure, alert ops.

    This MUST go through the multisig — direct submission from a hot wallet
    would defeat the trust model (a compromised bot could fake snapshots).
    """
    raise NotImplementedError("Safe multisig integration not yet added")


async def _submit_claim_batch(
    period_id: int,
    workers: list[str],
    den: list[int],
    proofs: list[list[bytes]],
) -> None:
    """TODO: submit PaymentRouter.claimBatch(...) from bot hot wallet.

    Gas only — the contract pulls AIPG from RewardPool, the bot never holds
    funds. If the tx reverts, log and continue; partial-batch progress is
    preserved on-chain via the `periodClaimed` flag per worker.
    """
    raise NotImplementedError("Web3 client wiring not yet added")


async def _load_last_settled_period_id() -> int | None:
    """TODO: read from a tiny `settlement_state` table or a JSON file on disk."""
    raise NotImplementedError("Settlement state persistence not yet defined")


async def _save_last_settled_period_id(period_id: int) -> None:
    """TODO: write to the same store as _load_last_settled_period_id."""
    raise NotImplementedError("Settlement state persistence not yet defined")


# ============ ENTRYPOINT ============


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()

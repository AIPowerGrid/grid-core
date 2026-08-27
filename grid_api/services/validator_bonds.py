# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Finalized Base bond snapshots for dark media-reference eligibility.

The synchronizer is deliberately outside inference request paths. It verifies
the configured Diamond, every WorkerRegistry selector route, and the reviewed
facet runtime at one finalized block before updating pre-reviewed reference
rows atomically. It never creates trust rows, changes quality review, or affects
routing, rewards, strikes, payouts, or slashing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from web3 import Web3

from .._abi import WORKER_REGISTRY_ABI
from ..config import GridSettings, get_settings
from ..database import new_session
from ..v2.schema import validator_bond_sync_state as sync_state_t
from ..v2.schema import validator_reference_workers as references_t
from ..v2.schema import workers as workers_t

# Exact candidate selector set from aipg-smart-contracts WorkerRegistry. A
# partial or mixed facet cut is not a valid bond authority.
WORKER_REGISTRY_SELECTORS = (
    "0xfe40c4bf",  # cancelUnbond()
    "0x5990dc2b",  # getMinBond()
    "0x5c50c356",  # getTotalBonded()
    "0x0c64afb2",  # getUnbondInfo(address)
    "0xc011b1c3",  # getWorker(address)
    "0x62e6e84d",  # getWorkerAt(uint256)
    "0x4d7599f1",  # getWorkerCount()
    "0xab0e7d53",  # isSlashEvidenceUsed(bytes32)
    "0xc5689dbf",  # isWorkerActive(address)
    "0x86796f13",  # registerWorker(uint256)
    "0x6eaae824",  # setMinBond(uint256)
    "0x114eaf55",  # setUnbondingPeriod(uint256)
    "0x773850c2",  # slash(address,uint256,bytes32,string)
    "0x5df6a6bc",  # unbond()
    "0x6cf6d675",  # unbondingPeriod()
    "0x66eb9cec",  # withdrawBond()
)

# Verifier identities are code-reviewed release contracts, not operator labels.
# Adding one requires a Core release that names the exact smart-contract commit
# and compiled runtime. Environment variables may select a supported verifier;
# they cannot redefine what that verifier means.
REVIEWED_WORKER_REGISTRY_RUNTIMES = {
    "worker-registry-v2-957685a": (
        "0x10cb9fb1b441747142df35545d69e705e81543516937c7a7b08c3df2ccbb5db2"
    ),
}

_BOND_SYNC_LOCK_KEY = 0x4150494756424F4E  # "AIPGVBON"


class BondSyncError(RuntimeError):
    """The chain snapshot is not safe to persist."""


@dataclass(frozen=True)
class WorkerBond:
    wallet: str
    amount_raw: int
    active: bool
    slashed: bool
    unbonding_at: int


@dataclass(frozen=True)
class FinalizedBondSnapshot:
    chain_id: int
    diamond_address: str
    facet_address: str
    facet_runtime_hash: str
    finalized_block: int
    finalized_block_hash: str
    workers: Mapping[str, WorkerBond]


def rpc_sources_are_distinct(primary: str, confirmation: str) -> bool:
    """Return whether two HTTP(S) RPC URLs resolve through different hosts."""
    parsed = [urlsplit(value.strip()) for value in (primary, confirmation)]
    return all(
        item.scheme in {"http", "https"} and bool(item.hostname)
        for item in parsed
    ) and parsed[0].hostname.lower() != parsed[1].hostname.lower()


def reviewed_runtime_hash(verifier_version: str) -> str | None:
    """Return the immutable runtime approved for one named verifier release."""
    return REVIEWED_WORKER_REGISTRY_RUNTIMES.get(verifier_version.strip())


def _normalize_address(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if not Web3.is_address(text):
        raise BondSyncError(f"{field} is not a 20-byte address")
    return text


def _normalize_hash(value: Any, *, field: str) -> str:
    if isinstance(value, bytes):
        text = "0x" + value.hex()
    else:
        text = str(value or "").strip().lower()
        if len(text) == 64:
            text = "0x" + text
    if len(text) != 66 or not text.startswith("0x"):
        raise BondSyncError(f"{field} is not a 32-byte hash")
    try:
        int(text[2:], 16)
    except ValueError as exc:
        raise BondSyncError(f"{field} is not a 32-byte hash") from exc
    return text


def _default_web3_factory(rpc_url: str, timeout_seconds: int) -> Web3:
    return Web3(
        Web3.HTTPProvider(
            rpc_url,
            request_kwargs={"timeout": timeout_seconds},
        ),
    )


def read_finalized_bond_snapshot(
    *,
    rpc_url: str,
    expected_chain_id: int,
    diamond_address: str,
    expected_facet_runtime_hash: str,
    reference_wallets: tuple[str, ...],
    max_workers: int,
    rpc_timeout_seconds: int,
    finalized_block: int | None = None,
    anchor_block: int | None = None,
    anchor_block_hash: str | None = None,
    web3_factory: Callable[[str, int], Any] = _default_web3_factory,
) -> FinalizedBondSnapshot:
    """Read and verify one internally consistent finalized chain snapshot.

    When ``finalized_block`` is supplied, the provider must report a finalized
    tip at or above that height and every read is pinned to that exact block.
    This lets independent providers with slightly different finalized tips
    prove agreement on their shared finalized history.
    """
    if not rpc_url.strip():
        raise BondSyncError("Base RPC is not configured")
    if expected_chain_id <= 0:
        raise BondSyncError("expected chain id must be positive")
    if not 1 <= max_workers <= 100_000:
        raise BondSyncError("max workers must be between 1 and 100000")
    if not 1 <= rpc_timeout_seconds <= 120:
        raise BondSyncError("RPC timeout must be between 1 and 120 seconds")

    normalized_diamond = _normalize_address(diamond_address, field="bond contract")
    normalized_runtime = _normalize_hash(
        expected_facet_runtime_hash,
        field="facet runtime hash",
    )
    w3 = web3_factory(rpc_url, rpc_timeout_seconds)
    if not w3.is_connected():
        raise BondSyncError("Base RPC is unavailable")
    chain_id = int(w3.eth.chain_id)
    if chain_id != expected_chain_id:
        raise BondSyncError("Base RPC chain id does not match configuration")

    finalized_tip = w3.eth.get_block("finalized")
    finalized_tip_number = int(finalized_tip["number"])
    if finalized_tip_number < 0:
        raise BondSyncError("finalized block is invalid")
    snapshot_block = finalized_tip_number if finalized_block is None else int(finalized_block)
    if snapshot_block < 0 or snapshot_block > finalized_tip_number:
        raise BondSyncError("requested block is not finalized by this provider")
    if (anchor_block is None) != (anchor_block_hash is None):
        raise BondSyncError("finality anchor block and hash must be supplied together")
    if anchor_block is not None:
        anchor_number = int(anchor_block)
        if anchor_number < 0 or anchor_number > finalized_tip_number:
            raise BondSyncError("prior finality anchor is not finalized by this provider")
        expected_anchor_hash = _normalize_hash(
            anchor_block_hash,
            field="prior finalized block hash",
        )
        anchor = w3.eth.get_block(anchor_number)
        if int(anchor["number"]) != anchor_number:
            raise BondSyncError("Base RPC returned the wrong prior finalized block")
        if _normalize_hash(anchor["hash"], field="prior finalized block hash") != expected_anchor_hash:
            raise BondSyncError("prior finalized block hash changed")
    block = (
        finalized_tip
        if snapshot_block == finalized_tip_number
        else w3.eth.get_block(snapshot_block)
    )
    if int(block["number"]) != snapshot_block:
        raise BondSyncError("Base RPC returned the wrong finalized block")
    finalized_hash = _normalize_hash(block["hash"], field="finalized block hash")
    diamond = w3.eth.contract(
        address=Web3.to_checksum_address(normalized_diamond),
        abi=WORKER_REGISTRY_ABI,
    )

    routed_facets: set[str] = set()
    for selector in WORKER_REGISTRY_SELECTORS:
        routed = diamond.functions.moduleAddress(bytes.fromhex(selector[2:])).call(
            block_identifier=snapshot_block,
        )
        routed_facets.add(_normalize_address(routed, field="routed facet"))
    if len(routed_facets) != 1:
        raise BondSyncError("WorkerRegistry selectors do not route to one facet")
    facet_address = routed_facets.pop()

    facet_code = bytes(
        w3.eth.get_code(
            Web3.to_checksum_address(facet_address),
            block_identifier=snapshot_block,
        ),
    )
    if not facet_code:
        raise BondSyncError("routed WorkerRegistry facet has no code")
    runtime_hash = _normalize_hash(Web3.keccak(facet_code), field="facet runtime hash")
    if runtime_hash != normalized_runtime:
        raise BondSyncError("WorkerRegistry facet runtime hash is not reviewed")

    normalized_wallets: list[str] = []
    seen_wallets: set[str] = set()
    for wallet in reference_wallets:
        normalized = _normalize_address(wallet, field="reviewed reference wallet")
        if normalized in seen_wallets:
            raise BondSyncError("reviewed reference wallets contain a duplicate")
        seen_wallets.add(normalized)
        normalized_wallets.append(normalized)
    if len(normalized_wallets) > max_workers:
        raise BondSyncError("reviewed reference wallet count exceeds the configured bound")

    workers: dict[str, WorkerBond] = {}
    zero_address = "0x" + "0" * 40
    for wallet in normalized_wallets:
        raw = diamond.functions.getWorker(
            Web3.to_checksum_address(wallet),
        ).call(block_identifier=snapshot_block)
        if not isinstance(raw, list | tuple) or len(raw) != 8:
            raise BondSyncError("WorkerRegistry returned an unexpected worker tuple")
        returned = _normalize_address(raw[0], field="worker tuple address")
        if returned == zero_address:
            has_nonzero_state = any(int(raw[index]) != 0 for index in (1, 2, 3, 4, 7))
            if has_nonzero_state or bool(raw[5]) or bool(raw[6]):
                raise BondSyncError("WorkerRegistry returned inconsistent unknown worker state")
            continue
        if returned != wallet:
            raise BondSyncError("WorkerRegistry query and worker tuple disagree")
        amount_raw = int(raw[1])
        active = bool(raw[5])
        slashed = bool(raw[6])
        unbonding_at = int(raw[7])
        if amount_raw < 0 or unbonding_at < 0:
            raise BondSyncError("WorkerRegistry returned negative state")
        if active and (slashed or unbonding_at != 0 or amount_raw == 0):
            raise BondSyncError("WorkerRegistry returned inconsistent active state")
        workers[wallet] = WorkerBond(
            wallet=wallet,
            amount_raw=amount_raw,
            active=active,
            slashed=slashed,
            unbonding_at=unbonding_at,
        )

    return FinalizedBondSnapshot(
        chain_id=chain_id,
        diamond_address=normalized_diamond,
        facet_address=facet_address,
        facet_runtime_hash=runtime_hash,
        finalized_block=snapshot_block,
        finalized_block_hash=finalized_hash,
        workers=workers,
    )


def read_quorum_bond_snapshot(
    *,
    rpc_url: str,
    confirmation_rpc_url: str,
    expected_chain_id: int,
    diamond_address: str,
    expected_facet_runtime_hash: str,
    reference_wallets: tuple[str, ...],
    max_workers: int,
    rpc_timeout_seconds: int,
    anchor_block: int | None = None,
    anchor_block_hash: str | None = None,
    single_reader: Callable[..., FinalizedBondSnapshot] = read_finalized_bond_snapshot,
) -> FinalizedBondSnapshot:
    """Require two distinct RPC sources to agree on the complete snapshot."""
    primary_url = rpc_url.strip()
    confirmation_url = confirmation_rpc_url.strip()
    if not primary_url or not confirmation_url:
        raise BondSyncError("two Base RPC sources are required")
    if not rpc_sources_are_distinct(primary_url, confirmation_url):
        raise BondSyncError("Base RPC sources must use distinct HTTP(S) hosts")
    common = {
        "expected_chain_id": expected_chain_id,
        "diamond_address": diamond_address,
        "expected_facet_runtime_hash": expected_facet_runtime_hash,
        "reference_wallets": reference_wallets,
        "max_workers": max_workers,
        "rpc_timeout_seconds": rpc_timeout_seconds,
        "anchor_block": anchor_block,
        "anchor_block_hash": anchor_block_hash,
    }
    primary = single_reader(rpc_url=primary_url, **common)
    confirmation = single_reader(rpc_url=confirmation_url, **common)
    if primary.finalized_block != confirmation.finalized_block:
        common_block = min(primary.finalized_block, confirmation.finalized_block)
        primary = single_reader(
            rpc_url=primary_url,
            finalized_block=common_block,
            **common,
        )
        confirmation = single_reader(
            rpc_url=confirmation_url,
            finalized_block=common_block,
            **common,
        )
    if primary != confirmation:
        raise BondSyncError("independent Base RPC snapshots do not agree")
    return primary


async def apply_reference_bond_snapshot(
    session: AsyncSession,
    *,
    snapshot: FinalizedBondSnapshot,
    verifier_version: str,
    verified_at: datetime | None = None,
) -> dict[str, int]:
    """Refresh only existing reviewed reference rows in one DB transaction."""
    normalized_verifier = verifier_version.strip()
    reviewed_runtime = reviewed_runtime_hash(normalized_verifier)
    if reviewed_runtime is None:
        raise BondSyncError("bond verifier version is not reviewed by this Core release")
    if snapshot.facet_runtime_hash != reviewed_runtime:
        raise BondSyncError("snapshot runtime does not match the named verifier")
    current = verified_at or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)

    rows = (
        (
            await session.execute(
                sa.select(
                    references_t.c.worker_id,
                    references_t.c.model,
                    references_t.c.modality,
                    references_t.c.account_id,
                    references_t.c.payout_wallet,
                    references_t.c.bond_finalized_block,
                    workers_t.c.account_id.label("worker_account_id"),
                    workers_t.c.wallet.label("worker_payout_wallet"),
                )
                .outerjoin(workers_t, workers_t.c.id == references_t.c.worker_id)
                .where(
                    sa.or_(
                        references_t.c.bond_contract.is_(None),
                        sa.and_(
                            references_t.c.bond_chain_id == snapshot.chain_id,
                            sa.func.lower(references_t.c.bond_contract)
                            == snapshot.diamond_address,
                        ),
                    ),
                )
                .with_for_update(of=references_t),
            )
        )
        .mappings()
        .all()
    )
    if any(
        row["bond_finalized_block"] is not None
        and int(row["bond_finalized_block"]) > snapshot.finalized_block
        for row in rows
    ):
        raise BondSyncError("finalized bond snapshot moved backwards")

    updated = inactive = 0
    for row in rows:
        wallet_text = str(row["payout_wallet"] or "").strip().lower()
        bond = snapshot.workers.get(wallet_text)
        worker_wallet = str(row["worker_payout_wallet"] or "").strip().lower()
        identity_matches = bool(
            row["worker_account_id"] == row["account_id"]
            and worker_wallet
            and worker_wallet == wallet_text,
        )
        is_active = bool(
            identity_matches
            and bond
            and bond.active
            and not bond.slashed
            and bond.unbonding_at == 0
            and bond.amount_raw > 0,
        )
        if not identity_matches:
            status_reason = "identity_mismatch"
        elif bond is None:
            status_reason = "not_registered"
        elif bond.slashed:
            status_reason = "slashed"
        elif bond.unbonding_at:
            status_reason = "unbonding"
        elif not bond.active or bond.amount_raw == 0:
            status_reason = "inactive"
        else:
            status_reason = "active"
        values = {
            "bond_contract": snapshot.diamond_address,
            "bond_chain_id": snapshot.chain_id,
            "bond_finalized_block": snapshot.finalized_block,
            "bond_finalized_block_hash": snapshot.finalized_block_hash,
            "bond_facet_address": snapshot.facet_address,
            "bond_facet_runtime_hash": snapshot.facet_runtime_hash,
            "bond_amount_raw": Decimal(bond.amount_raw if bond else 0),
            "bond_active": is_active,
            "bond_slashed": bool(bond and bond.slashed),
            "bond_verifier_version": normalized_verifier,
            "bond_status_reason": status_reason,
            "bond_verified_at": current,
            "updated": current,
        }
        await session.execute(
            sa.update(references_t)
            .where(
                references_t.c.worker_id == row["worker_id"],
                references_t.c.model == row["model"],
                references_t.c.modality == row["modality"],
            )
            .values(**values),
        )
        updated += 1
        if not is_active:
            inactive += 1
    return {
        "reference_rows": len(rows),
        "updated": updated,
        "inactive": inactive,
        "matched_workers": len(snapshot.workers),
        "finalized_block": snapshot.finalized_block,
    }


async def _try_sync_lock(session: AsyncSession) -> bool:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return True
    return bool(
        await session.scalar(
            sa.text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": _BOND_SYNC_LOCK_KEY},
        ),
    )


async def _sync_state_row(
    session: AsyncSession,
    *,
    chain_id: int,
    bond_contract: str,
) -> Mapping[str, Any] | None:
    return (
        (
            await session.execute(
                sa.select(sync_state_t)
                .where(
                    sync_state_t.c.chain_id == chain_id,
                    sa.func.lower(sync_state_t.c.bond_contract) == bond_contract,
                )
                .with_for_update(),
            )
        )
        .mappings()
        .one_or_none()
    )


async def _write_sync_state(
    session: AsyncSession,
    *,
    chain_id: int,
    bond_contract: str,
    verifier_version: str,
    status: str,
    status_reason: str,
    now: datetime,
    prior: Mapping[str, Any] | None,
    snapshot: FinalizedBondSnapshot | None,
) -> None:
    values = {
        "verifier_version": verifier_version,
        "status": status,
        "status_reason": status_reason[:64],
        "consecutive_failures": 0 if status == "healthy" else int((prior or {}).get("consecutive_failures") or 0) + 1,
        "last_attempt_at": now,
        "last_success_at": now if status == "healthy" else (prior or {}).get("last_success_at"),
        "updated": now,
    }
    if snapshot is not None:
        values.update(
            facet_address=snapshot.facet_address,
            facet_runtime_hash=snapshot.facet_runtime_hash,
            finalized_block=snapshot.finalized_block,
            finalized_block_hash=snapshot.finalized_block_hash,
        )
    if prior is None:
        await session.execute(
            sa.insert(sync_state_t).values(
                chain_id=chain_id,
                bond_contract=bond_contract,
                created=now,
                **values,
            ),
        )
    else:
        await session.execute(
            sa.update(sync_state_t)
            .where(
                sync_state_t.c.chain_id == chain_id,
                sa.func.lower(sync_state_t.c.bond_contract) == bond_contract,
            )
            .values(**values),
        )


async def _invalidate_bond_eligibility(
    session: AsyncSession,
    *,
    chain_id: int,
    bond_contract: str,
    now: datetime,
) -> int:
    result = await session.execute(
        sa.update(references_t)
        .where(
            references_t.c.bond_active.is_(True),
            references_t.c.bond_chain_id == chain_id,
            sa.func.lower(references_t.c.bond_contract) == bond_contract,
        )
        .values(
            bond_active=False,
            bond_status_reason="sync_faulted",
            bond_verified_at=None,
            updated=now,
        ),
    )
    return int(result.rowcount or 0)


async def sync_reference_bonds_once(
    *,
    settings: GridSettings | None = None,
    snapshot_reader: Callable[..., FinalizedBondSnapshot] = read_quorum_bond_snapshot,
) -> dict[str, Any]:
    """Run one default-off finalized snapshot and atomic cache refresh."""
    config = settings or get_settings()
    if not config.validator_media_bond_sync_enabled:
        return {"status": "disabled"}
    rpc_url = config.base_rpc_url.get_secret_value() if config.base_rpc_url else ""
    confirmation_rpc_url = (
        config.validator_media_bond_confirmation_rpc_url.get_secret_value()
        if config.validator_media_bond_confirmation_rpc_url
        else ""
    )
    verifier_version = config.validator_media_bond_verifier_version.strip()
    expected_runtime = reviewed_runtime_hash(verifier_version)
    if expected_runtime is None:
        raise BondSyncError("bond verifier version is not reviewed by this Core release")
    bond_contract = _normalize_address(config.validator_media_bond_contract, field="bond contract")
    now = datetime.now(UTC)
    async with await new_session() as session:
        async with session.begin():
            if not await _try_sync_lock(session):
                return {"status": "skipped", "reason": "another sync is running"}
            prior = await _sync_state_row(
                session,
                chain_id=config.validator_media_bond_chain_id,
                bond_contract=bond_contract,
            )
            raw_wallets = (
                await session.execute(
                    sa.select(references_t.c.payout_wallet)
                    .where(
                        sa.or_(
                            references_t.c.bond_contract.is_(None),
                            sa.and_(
                                references_t.c.bond_chain_id
                                == config.validator_media_bond_chain_id,
                                sa.func.lower(references_t.c.bond_contract)
                                == bond_contract,
                            ),
                        ),
                    )
                    .distinct(),
                )
            ).scalars().all()
            reference_wallets = tuple(
                sorted(
                    {
                        str(wallet).strip().lower()
                        for wallet in raw_wallets
                        if Web3.is_address(str(wallet).strip())
                    },
                ),
            )
            try:
                try:
                    snapshot = await asyncio.to_thread(
                        snapshot_reader,
                        rpc_url=rpc_url,
                        confirmation_rpc_url=confirmation_rpc_url,
                        expected_chain_id=config.validator_media_bond_chain_id,
                        diamond_address=bond_contract,
                        expected_facet_runtime_hash=expected_runtime,
                        reference_wallets=reference_wallets,
                        max_workers=config.validator_media_bond_max_workers,
                        rpc_timeout_seconds=config.validator_media_bond_rpc_timeout_seconds,
                        anchor_block=(prior or {}).get("finalized_block"),
                        anchor_block_hash=(prior or {}).get("finalized_block_hash"),
                    )
                except BondSyncError:
                    raise
                except Exception as exc:
                    raise BondSyncError("bond snapshot read failed") from exc
                result = await apply_reference_bond_snapshot(
                    session,
                    snapshot=snapshot,
                    verifier_version=verifier_version,
                    verified_at=now,
                )
            except BondSyncError as exc:
                invalidated = await _invalidate_bond_eligibility(
                    session,
                    chain_id=config.validator_media_bond_chain_id,
                    bond_contract=bond_contract,
                    now=now,
                )
                await _write_sync_state(
                    session,
                    chain_id=config.validator_media_bond_chain_id,
                    bond_contract=bond_contract,
                    verifier_version=verifier_version,
                    status="faulted",
                    status_reason="snapshot_verification_failed",
                    now=now,
                    prior=prior,
                    snapshot=None,
                )
                return {
                    "status": "faulted",
                    "reason": str(exc),
                    "invalidated": invalidated,
                }
            await _write_sync_state(
                session,
                chain_id=config.validator_media_bond_chain_id,
                bond_contract=bond_contract,
                verifier_version=verifier_version,
                status="healthy",
                status_reason="quorum_verified",
                now=now,
                prior=prior,
                snapshot=snapshot,
            )
            return {"status": "synced", **result}

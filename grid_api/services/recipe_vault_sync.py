# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verified finalized RecipeVault snapshots for the off-path recipe cache.

The chain is a governance/provenance input, not an inference dependency. Two
independent Base RPC providers must agree on one finalized block, every
RecipeVault selector must route to one reviewed facet runtime, and the complete
bounded record set must match before recipe parsing begins.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from web3 import Web3

from .._abi import RECIPEVAULT_ABI
from .validator_bonds import rpc_sources_are_distinct

RECIPE_VAULT_SELECTORS = (
    "0xa6658f46",  # canRecipeCreateNFTs(uint256)
    "0x98f9ecf8",  # getCreatorRecipes(address)
    "0x4d0af193",  # getMaxWorkflowBytes()
    "0xf8d12a41",  # getRecipe(uint256)
    "0xc03ce167",  # getRecipeByRoot(bytes32)
    "0x1650ac6d",  # getTotalRecipes()
    "0xb7471fc2",  # isRecipePublic(uint256)
    "0xfa8e6b0b",  # setMaxWorkflowBytes(uint256)
    "0xb2c93a4b",  # storeRecipe(bytes32,bytes,bool,bool,uint8,string,string)
    "0x63927510",  # updateRecipePermissions(uint256,bool,bool)
)

# Environment may select one reviewed verifier label but cannot redefine its
# runtime. This is the governed facet merged in aipg-smart-contracts 30c1d6d.
REVIEWED_RECIPE_VAULT_RUNTIMES = {
    "recipe-vault-v1-30c1d6d": (
        "0x4c585d77c8dfd729bb6a93e6d2451c6a39584c7f10eb4a66691e7a70a7c88c60"
    ),
}


class RecipeVaultSyncError(RuntimeError):
    """The chain snapshot is not safe to use."""


@dataclass(frozen=True)
class OnchainRecipeRecord:
    recipe_id: int
    recipe_root: str
    workflow_data: bytes
    creator: str
    can_create_nfts: bool
    is_public: bool
    compression: int
    created_at: int
    name: str
    description: str


@dataclass(frozen=True)
class FinalizedRecipeSnapshot:
    chain_id: int
    diamond_address: str
    facet_address: str
    facet_runtime_hash: str
    finalized_block: int
    finalized_block_hash: str
    finalized_block_timestamp: int
    records: tuple[OnchainRecipeRecord, ...]


def reviewed_runtime_hash(verifier_version: str) -> str | None:
    return REVIEWED_RECIPE_VAULT_RUNTIMES.get(verifier_version.strip())


def _normalize_address(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if not Web3.is_address(text):
        raise RecipeVaultSyncError(f"{field} is not a 20-byte address")
    return text


def _normalize_hash(value: Any, *, field: str) -> str:
    if isinstance(value, bytes):
        text = "0x" + value.hex()
    else:
        text = str(value or "").strip().lower()
        if len(text) == 64:
            text = "0x" + text
    if len(text) != 66 or not text.startswith("0x"):
        raise RecipeVaultSyncError(f"{field} is not a 32-byte hash")
    try:
        int(text[2:], 16)
    except ValueError as exc:
        raise RecipeVaultSyncError(f"{field} is not a 32-byte hash") from exc
    return text


def _default_web3_factory(rpc_url: str, timeout_seconds: int) -> Web3:
    return Web3(
        Web3.HTTPProvider(
            rpc_url,
            request_kwargs={"timeout": timeout_seconds},
        ),
    )


def read_finalized_recipe_snapshot(
    *,
    rpc_url: str,
    expected_chain_id: int,
    diamond_address: str,
    expected_facet_runtime_hash: str,
    max_records: int,
    max_workflow_bytes: int,
    rpc_timeout_seconds: int,
    max_finalized_age_seconds: int,
    finalized_block: int | None = None,
    web3_factory: Callable[[str, int], Any] = _default_web3_factory,
    now_unix: Callable[[], float] = time.time,
) -> FinalizedRecipeSnapshot:
    """Read one complete, internally consistent RecipeVault snapshot."""
    if not rpc_url.strip():
        raise RecipeVaultSyncError("Base RPC is not configured")
    if expected_chain_id <= 0:
        raise RecipeVaultSyncError("expected chain id must be positive")
    if not 1 <= max_records <= 10_000:
        raise RecipeVaultSyncError("max records must be between 1 and 10000")
    if not 1 <= max_workflow_bytes <= 1024 * 1024:
        raise RecipeVaultSyncError("max workflow bytes must be between 1 and 1048576")
    if not 1 <= rpc_timeout_seconds <= 120:
        raise RecipeVaultSyncError("RPC timeout must be between 1 and 120 seconds")
    if not 60 <= max_finalized_age_seconds <= 86_400:
        raise RecipeVaultSyncError("max finalized age must be between 60 and 86400 seconds")

    normalized_diamond = _normalize_address(diamond_address, field="RecipeVault Diamond")
    normalized_runtime = _normalize_hash(
        expected_facet_runtime_hash,
        field="RecipeVault facet runtime hash",
    )
    w3 = web3_factory(rpc_url, rpc_timeout_seconds)
    if not w3.is_connected():
        raise RecipeVaultSyncError("Base RPC is unavailable")
    chain_id = int(w3.eth.chain_id)
    if chain_id != expected_chain_id:
        raise RecipeVaultSyncError("Base RPC chain id does not match configuration")

    finalized_tip = w3.eth.get_block("finalized")
    finalized_tip_number = int(finalized_tip["number"])
    snapshot_block = finalized_tip_number if finalized_block is None else int(finalized_block)
    if snapshot_block < 0 or snapshot_block > finalized_tip_number:
        raise RecipeVaultSyncError("requested block is not finalized by this provider")
    block = finalized_tip if snapshot_block == finalized_tip_number else w3.eth.get_block(snapshot_block)
    if int(block["number"]) != snapshot_block:
        raise RecipeVaultSyncError("Base RPC returned the wrong finalized block")
    block_hash = _normalize_hash(block["hash"], field="finalized block hash")
    block_timestamp = int(block.get("timestamp", 0))
    age_seconds = int(now_unix()) - block_timestamp
    if block_timestamp <= 0 or age_seconds < -60 or age_seconds > max_finalized_age_seconds:
        raise RecipeVaultSyncError("finalized block is outside the configured freshness window")

    diamond = w3.eth.contract(
        address=Web3.to_checksum_address(normalized_diamond),
        abi=RECIPEVAULT_ABI,
    )
    routed_facets: set[str] = set()
    for selector in RECIPE_VAULT_SELECTORS:
        routed = diamond.functions.moduleAddress(bytes.fromhex(selector[2:])).call(
            block_identifier=snapshot_block,
        )
        routed_facets.add(_normalize_address(routed, field="routed RecipeVault facet"))
    if len(routed_facets) != 1:
        raise RecipeVaultSyncError("RecipeVault selectors do not route to one facet")
    facet_address = routed_facets.pop()
    facet_code = bytes(
        w3.eth.get_code(
            Web3.to_checksum_address(facet_address),
            block_identifier=snapshot_block,
        ),
    )
    if not facet_code:
        raise RecipeVaultSyncError("routed RecipeVault facet has no code")
    runtime_hash = _normalize_hash(Web3.keccak(facet_code), field="RecipeVault facet runtime hash")
    if runtime_hash != normalized_runtime:
        raise RecipeVaultSyncError("RecipeVault facet runtime hash is not reviewed")

    total = int(diamond.functions.getTotalRecipes().call(block_identifier=snapshot_block))
    if total < 0 or total > max_records:
        raise RecipeVaultSyncError("RecipeVault record count exceeds the configured bound")
    records: list[OnchainRecipeRecord] = []
    for requested_id in range(1, total + 1):
        raw = diamond.functions.getRecipe(requested_id).call(block_identifier=snapshot_block)
        if not isinstance(raw, list | tuple) or len(raw) != 10:
            raise RecipeVaultSyncError("RecipeVault returned an unexpected recipe tuple")
        recipe_id = int(raw[0])
        if recipe_id != requested_id:
            raise RecipeVaultSyncError("RecipeVault query and recipe tuple disagree")
        workflow_data = bytes(raw[2])
        if not workflow_data or len(workflow_data) > max_workflow_bytes:
            raise RecipeVaultSyncError("RecipeVault workflow size is outside the configured bound")
        compression = int(raw[6])
        created_at = int(raw[7])
        if compression < 0 or compression > 255 or created_at < 0:
            raise RecipeVaultSyncError("RecipeVault returned invalid numeric state")
        records.append(
            OnchainRecipeRecord(
                recipe_id=recipe_id,
                recipe_root=_normalize_hash(raw[1], field="recipe root"),
                workflow_data=workflow_data,
                creator=_normalize_address(raw[3], field="recipe creator"),
                can_create_nfts=bool(raw[4]),
                is_public=bool(raw[5]),
                compression=compression,
                created_at=created_at,
                name=str(raw[8]),
                description=str(raw[9]),
            ),
        )
    return FinalizedRecipeSnapshot(
        chain_id=chain_id,
        diamond_address=normalized_diamond,
        facet_address=facet_address,
        facet_runtime_hash=runtime_hash,
        finalized_block=snapshot_block,
        finalized_block_hash=block_hash,
        finalized_block_timestamp=block_timestamp,
        records=tuple(records),
    )


def read_quorum_recipe_snapshot(
    *,
    rpc_url: str,
    confirmation_rpc_url: str,
    expected_chain_id: int,
    diamond_address: str,
    expected_facet_runtime_hash: str,
    max_records: int,
    max_workflow_bytes: int,
    rpc_timeout_seconds: int,
    max_finalized_age_seconds: int,
    single_reader: Callable[..., FinalizedRecipeSnapshot] = read_finalized_recipe_snapshot,
) -> FinalizedRecipeSnapshot:
    """Require two distinct RPC providers to agree on one complete snapshot."""
    primary = rpc_url.strip()
    confirmation = confirmation_rpc_url.strip()
    if not primary or not confirmation:
        raise RecipeVaultSyncError("two Base RPC sources are required")
    if not rpc_sources_are_distinct(primary, confirmation):
        raise RecipeVaultSyncError("Base RPC sources must use distinct HTTP(S) hosts")
    common = {
        "expected_chain_id": expected_chain_id,
        "diamond_address": diamond_address,
        "expected_facet_runtime_hash": expected_facet_runtime_hash,
        "max_records": max_records,
        "max_workflow_bytes": max_workflow_bytes,
        "rpc_timeout_seconds": rpc_timeout_seconds,
        "max_finalized_age_seconds": max_finalized_age_seconds,
    }
    first = single_reader(rpc_url=primary, **common)
    second = single_reader(rpc_url=confirmation, **common)
    if first.finalized_block != second.finalized_block:
        common_block = min(first.finalized_block, second.finalized_block)
        first = single_reader(
            rpc_url=primary,
            finalized_block=common_block,
            **common,
        )
        second = single_reader(
            rpc_url=confirmation,
            finalized_block=common_block,
            **common,
        )
    if second != first:
        raise RecipeVaultSyncError("Base RPC sources disagree on the RecipeVault snapshot")
    return first

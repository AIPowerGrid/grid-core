# ⚠️ UNWIRED / SHIP-DARK (2026-06-22 audit): no live request-path code imports this module.
# It is built but NOT active — do NOT assume billing/slashing/registry-sync runs. Wire it
# intentionally (+ tests) before relying on it. See task #62.

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Keep den.MODEL_REGISTRY in sync with the on-chain ModelVault.

ModelVault (Base, grid Diamond module) is the source of truth for which models
are approved and their parameter counts — which drive the den size multiplier.
`den.estimate_model_multiplier` reads `den.MODEL_REGISTRY`; this module refreshes
it from the contract. Until the on-chain read is wired, the seeded registry plus
DEFAULT_MULTIPLIER for unknowns is authoritative — which already removes the
name-parsing gaming vector (a fake "...-405b" model name no longer earns 405x).

Wire `sync_from_modelvault()` into app startup + a periodic refresh once
MODELVAULT_ADDRESS is set.
"""

import logging
import os

from . import den

logger = logging.getLogger("grid_api.model_registry")


async def sync_from_modelvault() -> int:
    """Pull approved models + param counts from ModelVault into den.MODEL_REGISTRY.

    Returns the number of models synced. No-ops (returns 0) if the contract
    isn't configured, leaving the seeded registry in place.
    """
    addr = os.getenv("MODELVAULT_ADDRESS")
    rpc = os.getenv("BASE_RPC_URL")
    if not addr or not rpc:
        logger.info(
            "ModelVault not configured (MODELVAULT_ADDRESS/BASE_RPC_URL) — "
            "using seeded den.MODEL_REGISTRY (%d models)", len(den.MODEL_REGISTRY)
        )
        return 0
    try:
        from web3 import Web3  # noqa: F401
        # TODO(settlement): read the ModelVault Diamond facet and, for each
        # registered model, call den.register_model(name, param_billions).
        # The facet exposes the model list + per-model metadata (see
        # aipg-smart-contracts grid/modules/ModelVault.sol). Once the ABI +
        # enumeration method are confirmed, replace this block:
        #   w3 = Web3(Web3.HTTPProvider(rpc))
        #   c = w3.eth.contract(address=w3.to_checksum_address(addr), abi=MODELVAULT_ABI)
        #   for m in c.functions.listModels().call():
        #       den.register_model(m.name, m.paramBillions)
        logger.warning("ModelVault sync not yet implemented — registry unchanged")
        return 0
    except Exception as e:
        logger.error("ModelVault sync failed: %s", e)
        return 0
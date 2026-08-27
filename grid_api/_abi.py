# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract ABIs and helpers for RecipeVault and WorkerRegistry reads.

The shipped RecipeVault ABI is human-readable (ethers-style strings); web3.py
needs JSON ABI, so the read functions Core calls are provided here as JSON.
WorkerRegistry exposes only the reviewed read surface used by the finalized
bond synchronizer.
"""

import json
import os
import zlib

_ABI_DIR = os.path.join(os.path.dirname(__file__), "abis")


def _load_raw(name: str) -> dict:
    with open(os.path.join(_ABI_DIR, f"{name}.json")) as f:
        return json.load(f)


_RECIPEVAULT_RAW = _load_raw("RecipeVault")
RECIPEVAULT_HUMAN_ABI: list[str] = _RECIPEVAULT_RAW.get("abi", [])
COMPRESSION = {0: "none", 1: "gzip", 2: "brotli"}  # RecipeVault.compression enum

# The Recipe tuple as returned by getRecipe(uint256) / getRecipeByRoot(bytes32):
_RECIPE_TUPLE = {
    "name": "", "type": "tuple", "components": [
        {"name": "recipeId", "type": "uint256"},
        {"name": "recipeRoot", "type": "bytes32"},
        {"name": "workflowData", "type": "bytes"},
        {"name": "creator", "type": "address"},
        {"name": "canCreateNFTs", "type": "bool"},
        {"name": "isPublic", "type": "bool"},
        {"name": "compression", "type": "uint8"},
        {"name": "createdAt", "type": "uint256"},
        {"name": "name", "type": "string"},
        {"name": "description", "type": "string"},
    ],
}

# JSON ABI for just the read methods we call (web3.py-compatible).
RECIPEVAULT_ABI = [
    {"type": "function", "name": "moduleAddress", "stateMutability": "view",
     "inputs": [{"name": "selector", "type": "bytes4"}],
     "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "getTotalRecipes", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "getRecipe", "stateMutability": "view",
     "inputs": [{"name": "recipeId", "type": "uint256"}], "outputs": [_RECIPE_TUPLE]},
    {"type": "function", "name": "getRecipeByRoot", "stateMutability": "view",
     "inputs": [{"name": "recipeRoot", "type": "bytes32"}], "outputs": [_RECIPE_TUPLE]},
]

_WORKER_TUPLE = {
    "name": "",
    "type": "tuple",
    "components": [
        {"name": "workerAddress", "type": "address"},
        {"name": "bondAmount", "type": "uint256"},
        {"name": "totalJobsCompleted", "type": "uint256"},
        {"name": "totalRewardsEarned", "type": "uint256"},
        {"name": "registeredAt", "type": "uint256"},
        {"name": "isActive", "type": "bool"},
        {"name": "isSlashed", "type": "bool"},
        {"name": "unbondingAt", "type": "uint256"},
    ],
}

# Minimal read ABI for the reviewed cooldown-backed WorkerRegistry facet through
# the Grid Diamond. Core verifies selector routing and facet runtime bytecode
# before trusting any returned bond state.
WORKER_REGISTRY_ABI = [
    {
        "type": "function",
        "name": "moduleAddress",
        "stateMutability": "view",
        "inputs": [{"name": "selector", "type": "bytes4"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "getWorkerCount",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getWorkerAt",
        "stateMutability": "view",
        "inputs": [{"name": "index", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "type": "function",
        "name": "getWorker",
        "stateMutability": "view",
        "inputs": [{"name": "worker", "type": "address"}],
        "outputs": [_WORKER_TUPLE],
    },
]


def decompress_workflow(
    data: bytes,
    compression: int,
    *,
    max_output_bytes: int = 256 * 1024,
) -> bytes:
    """Decode legacy workflow bytes without allowing unbounded expansion.

    Governed RecipeVault v1 records must be uncompressed. Gzip remains readable
    only for bounded legacy tooling; Brotli is rejected because the installed
    decoder exposes no reliable pre-allocation output cap.
    """
    if not 1 <= max_output_bytes <= 1024 * 1024:
        raise ValueError("max_output_bytes must be between 1 and 1048576")
    codec = COMPRESSION.get(int(compression))
    if codec is None:
        raise ValueError(f"unknown compression code {compression}")
    if codec == "none":
        output = bytes(data)
        if len(output) > max_output_bytes:
            raise ValueError("workflow exceeds decompressed size limit")
        return output
    if codec == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        output = decoder.decompress(bytes(data), max_output_bytes + 1)
        if len(output) > max_output_bytes or decoder.unconsumed_tail:
            raise ValueError("workflow exceeds decompressed size limit")
        output += decoder.flush(max_output_bytes + 1 - len(output))
        if len(output) > max_output_bytes:
            raise ValueError("workflow exceeds decompressed size limit")
        if not decoder.eof or decoder.unused_data:
            raise ValueError("workflow gzip stream is incomplete or concatenated")
        return output
    if codec == "brotli":
        raise ValueError("brotli workflows are not accepted from untrusted storage")
    raise AssertionError("unreachable compression codec")

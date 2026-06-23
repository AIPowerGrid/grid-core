# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract ABIs + helpers for on-chain reads (RecipeVault, …).

The shipped ABI file (`abis/RecipeVault.json`) is human-readable (ethers-style
strings); web3.py needs JSON ABI, so the read functions we call are provided here
as JSON, hand-transcribed from that file's signatures. The full human-readable
ABI is still loaded for reference / future use.
"""

import gzip
import json
import os

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
    {"type": "function", "name": "totalRecipes", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "getRecipe", "stateMutability": "view",
     "inputs": [{"name": "recipeId", "type": "uint256"}], "outputs": [_RECIPE_TUPLE]},
    {"type": "function", "name": "getRecipeByRoot", "stateMutability": "view",
     "inputs": [{"name": "recipeRoot", "type": "bytes32"}], "outputs": [_RECIPE_TUPLE]},
]


def decompress_workflow(data: bytes, compression: int) -> bytes:
    """Decompress on-chain workflowData per the recipe's compression enum."""
    codec = COMPRESSION.get(int(compression), "none")
    if codec == "none":
        return bytes(data)
    if codec == "gzip":
        return gzip.decompress(data)
    if codec == "brotli":
        import brotli  # optional dep; only needed for brotli-compressed recipes
        return brotli.decompress(data)
    raise ValueError(f"unknown compression code {compression}")

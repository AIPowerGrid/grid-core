# SPDX-FileCopyrightText: 2026 AI Power Grid
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Blockchain configuration for AIPG Web3 integration.
"""

import os


class BlockchainConfig:
    """Configuration for blockchain connections.

    NOTE: Grid Diamond contract is the primary model registry.
    This config is for legacy ModelRegistry (file hash validation) - mostly deprecated.
    See model_reference_blockchain.py for Grid Diamond integration.
    """

    # Base Sepolia (testnet) - LEGACY
    SEPOLIA_RPC_URL = "https://sepolia.base.org"
    SEPOLIA_CHAIN_ID = 84532

    # Base Mainnet
    MAINNET_RPC_URL = "https://mainnet.base.org"
    MAINNET_CHAIN_ID = 8453

    # Grid Diamond contract (Base Mainnet) - PRIMARY MODEL REGISTRY
    GRID_DIAMOND_CONTRACT = "0x79F39f2a0eA476f53994812e6a8f3C8CFe08c609"

    # Legacy ModelRegistry (Sepolia) - DEPRECATED, file hash validation
    MODEL_REGISTRY_SEPOLIA = "0xe660455D4A83bbbbcfDCF4219ad82447a831c8A1"

    @classmethod
    def get_grid_diamond_contract(cls) -> str:
        """Get Grid Diamond contract address (primary model registry)."""
        return os.getenv("GRID_DIAMOND_CONTRACT", cls.GRID_DIAMOND_CONTRACT)

    @classmethod
    def get_grid_diamond_rpc(cls) -> str:
        """Get Grid Diamond RPC URL."""
        return os.getenv("GRID_DIAMOND_RPC_URL", cls.MAINNET_RPC_URL)

    @classmethod
    def get_rpc_url(cls) -> str:
        """Get RPC URL from environment or default to Sepolia (legacy)."""
        return os.getenv("BASE_RPC_URL", cls.SEPOLIA_RPC_URL)

    @classmethod
    def get_model_registry_address(cls) -> str:
        """Get ModelRegistry contract address from environment or default (legacy)."""
        return os.getenv("MODEL_REGISTRY_ADDRESS", cls.MODEL_REGISTRY_SEPOLIA)

    @classmethod
    def is_enabled(cls) -> bool:
        """Check if blockchain validation is enabled (for model checking).

        When true, validates that models are registered in Grid Diamond contract.
        """
        return os.getenv("BLOCKCHAIN_ENABLED", "false").lower() == "true"

    @classmethod
    def get_chain_id(cls) -> int:
        """Get chain ID based on RPC URL."""
        rpc = cls.get_rpc_url()
        if "sepolia" in rpc:
            return cls.SEPOLIA_CHAIN_ID
        return cls.MAINNET_CHAIN_ID

"""
Blockchain configuration for AIPG Web3 integration.
"""

import os


class BlockchainConfig:
    """Configuration for blockchain connections."""

    # Base Sepolia (testnet)
    SEPOLIA_RPC_URL = "https://sepolia.base.org"
    SEPOLIA_CHAIN_ID = 84532

    # Base Mainnet
    MAINNET_RPC_URL = "https://mainnet.base.org"
    MAINNET_CHAIN_ID = 8453

    # Contract addresses (Sepolia)
    MODEL_REGISTRY_SEPOLIA = "0xe660455D4A83bbbbcfDCF4219ad82447a831c8A1"

    @classmethod
    def get_rpc_url(cls) -> str:
        """Get RPC URL from environment or default to Sepolia."""
        return os.getenv("BASE_RPC_URL", cls.SEPOLIA_RPC_URL)

    @classmethod
    def get_model_registry_address(cls) -> str:
        """Get ModelRegistry contract address from environment or default."""
        return os.getenv("MODEL_REGISTRY_ADDRESS", cls.MODEL_REGISTRY_SEPOLIA)

    @classmethod
    def is_enabled(cls) -> bool:
        """Check if blockchain integration is enabled."""
        return os.getenv("BLOCKCHAIN_ENABLED", "false").lower() == "true"

    @classmethod
    def get_chain_id(cls) -> int:
        """Get chain ID based on RPC URL."""
        rpc = cls.get_rpc_url()
        if "sepolia" in rpc:
            return cls.SEPOLIA_CHAIN_ID
        return cls.MAINNET_CHAIN_ID


"""
AIPG Blockchain Integration Module

Provides Web3 integration for model validation and job anchoring on Base chain.
"""

from horde.blockchain.config import BlockchainConfig
from horde.blockchain.model_registry import ModelRegistryClient

__all__ = ["BlockchainConfig", "ModelRegistryClient"]

# SPDX-FileCopyrightText: 2026 AI Power Grid
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Model Registry Client for AIPG Blockchain Integration.
"""

from typing import Optional, Dict, Any, List

from horde.blockchain.config import BlockchainConfig
from horde.logger import logger

# ABI for ModelRegistry contract (subset for needed functions)
MODEL_REGISTRY_ABI = [
    {
        "inputs": [{"internalType": "bytes32", "name": "modelHash", "type": "bytes32"}],
        "name": "isModelExists",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "string", "name": "modelId", "type": "string"}],
        "name": "getModelConstraints",
        "outputs": [
            {"internalType": "bool", "name": "exists", "type": "bool"},
            {"internalType": "uint16", "name": "stepsMin", "type": "uint16"},
            {"internalType": "uint16", "name": "stepsMax", "type": "uint16"},
            {"internalType": "uint16", "name": "cfgMinTenths", "type": "uint16"},
            {"internalType": "uint16", "name": "cfgMaxTenths", "type": "uint16"},
            {"internalType": "uint8", "name": "clipSkip", "type": "uint8"},
            {"internalType": "bytes32[]", "name": "samplerHashes", "type": "bytes32[]"},
            {"internalType": "bytes32[]", "name": "schedulerHashes", "type": "bytes32[]"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


class ModelConstraints:
    """Model constraints from on-chain data."""

    def __init__(
        self,
        exists: bool,
        steps_min: int,
        steps_max: int,
        cfg_min_tenths: int,
        cfg_max_tenths: int,
        clip_skip: int,
        sampler_hashes: List[bytes],
        scheduler_hashes: List[bytes],
    ):
        self.exists = exists
        self.steps_min = steps_min
        self.steps_max = steps_max
        # Scale CFG back from 10x integer
        self.cfg_min = cfg_min_tenths / 10.0
        self.cfg_max = cfg_max_tenths / 10.0
        self.clip_skip = clip_skip
        self.sampler_hashes = sampler_hashes
        self.scheduler_hashes = scheduler_hashes

    def has_constraints(self) -> bool:
        """Check if any constraints are set (non-zero values)."""
        return self.steps_min > 0 or self.steps_max > 0 or self.cfg_min > 0 or self.cfg_max > 0


class ValidationResult:
    def __init__(self, is_valid: bool, reason: str = ""):
        self.is_valid = is_valid
        self.reason = reason


class ModelRegistryClient:
    """Client for interacting with the ModelRegistry smart contract."""

    def __init__(self):
        self._web3 = None
        self._contract = None
        self._initialized = False

    def _ensure_initialized(self) -> bool:
        """Lazy initialization of Web3 connection."""
        if self._initialized:
            return self._web3 is not None

        self._initialized = True

        if not BlockchainConfig.is_enabled():
            logger.debug("Blockchain integration is disabled (env var not set or false)")
            return False

        try:
            from web3 import Web3

            rpc_url = BlockchainConfig.get_rpc_url()
            self._web3 = Web3(Web3.HTTPProvider(rpc_url))

            # Some RPCs (like Base Sepolia) don't support web3_clientVersion which is_connected checks
            # So we just try to get the block number to verify connection
            try:
                self._web3.eth.get_block_number()
            except Exception as e:
                logger.warning(f"Failed to connect to blockchain RPC: {rpc_url} ({e})")
                self._web3 = None
                return False

            contract_address = BlockchainConfig.get_model_registry_address()
            self._contract = self._web3.eth.contract(
                address=Web3.to_checksum_address(contract_address),
                abi=MODEL_REGISTRY_ABI,
            )

            logger.info(f"Connected to ModelRegistry at {contract_address} on {rpc_url}")
            return True

        except ImportError:
            logger.warning("web3 package not installed, blockchain features disabled")
            return False
        except Exception as e:
            logger.error(f"Failed to initialize blockchain client: {e}")
            return False

    def is_model_registered(self, model_hash: str) -> bool:
        """Check if a model is registered on-chain by its hash."""
        if not self._ensure_initialized():
            logger.warning("Blockchain not initialized, allowing model by default")
            return True  # Allow all models if blockchain is disabled

        try:
            # Convert hex string to bytes32
            if model_hash.startswith("0x"):
                model_hash_bytes = bytes.fromhex(model_hash[2:])
            else:
                model_hash_bytes = bytes.fromhex(model_hash)

            result = self._contract.functions.isModelExists(model_hash_bytes).call()
            logger.info(f"On-chain isModelExists({model_hash[:16]}...) = {result}")
            return result
        except Exception as e:
            logger.error(f"Error checking model registration: {e}")
            return True  # Allow on error

    def get_model_constraints(self, model_id: str) -> Optional[ModelConstraints]:
        """Get constraints for a model by its display name/ID."""
        if not self._ensure_initialized():
            return None

        try:
            result = self._contract.functions.getModelConstraints(model_id).call()
            # result = (exists, stepsMin, stepsMax, cfgMinTenths, cfgMaxTenths, clipSkip, samplerHashes, schedulerHashes)
            constraints = ModelConstraints(
                exists=result[0],
                steps_min=result[1],
                steps_max=result[2],
                cfg_min_tenths=result[3],
                cfg_max_tenths=result[4],
                clip_skip=result[5],
                sampler_hashes=result[6],
                scheduler_hashes=result[7],
            )
            logger.info(
                f"Got constraints for '{model_id}': steps={constraints.steps_min}-{constraints.steps_max}, cfg={constraints.cfg_min}-{constraints.cfg_max}"
            )
            return constraints
        except Exception as e:
            logger.warning(f"Could not get constraints for {model_id}: {e}")
            return None

    def validate_parameters(
        self,
        model_id: str,
        steps: int,
        cfg: float,
        sampler: str = None,
        scheduler: str = None,
    ) -> ValidationResult:
        """Validate generation parameters against on-chain constraints."""
        constraints = self.get_model_constraints(model_id)
        if not constraints or not constraints.has_constraints():
            logger.debug(f"No constraints found for {model_id}, allowing")
            return ValidationResult(True)

        # Check steps
        if constraints.steps_min > 0 and steps < constraints.steps_min:
            return ValidationResult(False, f"Steps {steps} < min {constraints.steps_min}")
        if constraints.steps_max > 0 and steps > constraints.steps_max:
            return ValidationResult(False, f"Steps {steps} > max {constraints.steps_max}")

        # Check CFG
        if constraints.cfg_min > 0 and cfg < constraints.cfg_min:
            return ValidationResult(False, f"CFG {cfg} < min {constraints.cfg_min}")
        if constraints.cfg_max > 0 and cfg > constraints.cfg_max:
            return ValidationResult(False, f"CFG {cfg} > max {constraints.cfg_max}")

        # Check sampler (compare hashes)
        if sampler and constraints.sampler_hashes:
            from web3 import Web3

            sampler_hash = Web3.keccak(text=sampler)
            if sampler_hash not in constraints.sampler_hashes:
                return ValidationResult(False, f"Sampler '{sampler}' not allowed for this model")

        # Check scheduler (compare hashes)
        if scheduler and constraints.scheduler_hashes:
            from web3 import Web3

            scheduler_hash = Web3.keccak(text=scheduler)
            if scheduler_hash not in constraints.scheduler_hashes:
                return ValidationResult(False, f"Scheduler '{scheduler}' not allowed for this model")

        return ValidationResult(True)


# Singleton instance
_model_registry_client: Optional[ModelRegistryClient] = None


def get_model_registry() -> ModelRegistryClient:
    """Get the singleton ModelRegistryClient instance."""
    global _model_registry_client
    if _model_registry_client is None:
        _model_registry_client = ModelRegistryClient()
    return _model_registry_client

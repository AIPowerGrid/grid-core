# SPDX-FileCopyrightText: 2024 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Blockchain-based model reference for AI Power Grid.

Fetches models from Grid Diamond contract on Base Mainnet, caches locally.
All request-time lookups use in-memory dict - NO blockchain calls per request.

Architecture:
  - Startup: Fetch from chain → cache to JSON → load into memory
  - Hourly: Background refresh from chain → update cache
  - Per-request: Pure dict lookup (sub-microsecond)
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set

from horde.logger import logger
from horde.threads import PrimaryTimedFunction

# Local cache file path
CACHE_FILE = Path(__file__).parent.parent / "model_cache_blockchain.json"

# Grid Diamond contract config
GRID_CONTRACT = os.getenv("GRID_DIAMOND_CONTRACT", "0x79F39f2a0eA476f53994812e6a8f3C8CFe08c609")
GRID_RPC = os.getenv("GRID_DIAMOND_RPC_URL", "https://base.publicnode.com")

# ABI for getModelCount and getModel
GRID_ABI = [
    {"inputs": [], "name": "getModelCount", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [{"type": "uint256", "name": "modelId"}],
        "name": "getModel",
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "modelHash", "type": "bytes32"},
                    {"name": "modelType", "type": "uint8"},
                    {"name": "fileName", "type": "string"},
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "ipfsCid", "type": "string"},
                    {"name": "downloadUrl", "type": "string"},
                    {"name": "sizeBytes", "type": "uint256"},
                    {"name": "quantization", "type": "string"},
                    {"name": "format", "type": "string"},
                    {"name": "vramMB", "type": "uint32"},
                    {"name": "baseModel", "type": "string"},
                    {"name": "inpainting", "type": "bool"},
                    {"name": "img2img", "type": "bool"},
                    {"name": "controlnet", "type": "bool"},
                    {"name": "lora", "type": "bool"},
                    {"name": "isActive", "type": "bool"},
                    {"name": "isNSFW", "type": "bool"},
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "creator", "type": "address"},
                ],
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

# Model type constants
MODEL_TYPE_TEXT = 0
MODEL_TYPE_IMAGE = 1
MODEL_TYPE_VIDEO = 2


def _fetch_models_from_chain() -> Dict[str, Dict[str, Any]]:
    """
    Fetch all models from the Grid Diamond contract.

    Returns dict in legacy reference format for compatibility.
    This is the ONLY function that makes blockchain calls.
    """
    try:
        from web3 import Web3
    except ImportError:
        logger.error("[MODEL_REF_CHAIN] web3 not installed")
        return {}

    try:
        w3 = Web3(Web3.HTTPProvider(GRID_RPC, request_kwargs={"timeout": 30}))
        contract = w3.eth.contract(address=Web3.to_checksum_address(GRID_CONTRACT), abi=GRID_ABI)

        count = contract.functions.getModelCount().call()
        logger.info(f"[MODEL_REF_CHAIN] Fetching {count} models from chain...")

        models = {}
        for i in range(1, count + 1):
            for attempt in range(3):
                try:
                    m = contract.functions.getModel(i).call()

                    # Skip inactive models
                    if not m[16]:  # isActive
                        break

                    name = m[3]  # display name
                    model_type = m[1]
                    base_model = m[11]

                    # Determine baseline
                    baseline = base_model if base_model else _infer_baseline(name, model_type)

                    # Determine style
                    style = "video" if model_type == MODEL_TYPE_VIDEO else "generalist"

                    # Determine type string
                    if m[14]:  # controlnet
                        type_str = "controlnet"
                    elif m[15]:  # lora
                        type_str = "lora"
                    elif model_type == MODEL_TYPE_VIDEO:
                        type_str = "video"
                    else:
                        type_str = "checkpoint"

                    models[name] = {
                        "name": name,
                        "baseline": baseline,
                        "type": type_str,
                        "style": style,
                        "nsfw": m[17],  # isNSFW
                        "inpainting": m[12],
                        "img2img": m[13],
                        "controlnet": m[14],
                        "lora": m[15],
                        "description": f"{name} model",
                        "version": m[4] or "1.0",
                        # Extra fields from chain
                        "_chain_id": i,
                        "_vram_mb": m[10],
                        "_size_bytes": m[7],
                        "_ipfs_cid": m[5],
                        "_download_url": m[6],
                    }
                    break

                except Exception as e:
                    if "429" in str(e):
                        time.sleep(1.0)  # Rate limit backoff
                    else:
                        logger.warning(f"[MODEL_REF_CHAIN] Error fetching model {i}: {e}")
                        break

            # Rate limiting for public RPC
            time.sleep(0.2)

        logger.info(f"[MODEL_REF_CHAIN] Fetched {len(models)} active models from chain")
        return models

    except Exception as e:
        logger.error(f"[MODEL_REF_CHAIN] Chain fetch failed: {e}")
        return {}


def _infer_baseline(name: str, model_type: int) -> str:
    """Infer baseline from model name if not provided."""
    name_lower = name.lower()

    if model_type == MODEL_TYPE_VIDEO:
        if "wan" in name_lower:
            if "2.1" in name_lower:
                return "wan_2_1"
            return "wan_2_2"
        elif "ltx" in name_lower:
            return "ltx_video"
        elif "cogvideo" in name_lower:
            return "cogvideo"
        elif "mochi" in name_lower:
            return "mochi"
        return "wan_2_2"

    # Image models
    if "flux" in name_lower:
        return "flux_1"
    elif "sdxl" in name_lower or "xl" in name_lower:
        return "stable_diffusion_xl"
    elif "sd2" in name_lower or "2.1" in name_lower:
        return "stable diffusion 2"
    elif "cascade" in name_lower:
        return "stable_cascade"

    return "stable diffusion 1"


def _save_cache(models: Dict[str, Dict[str, Any]]) -> bool:
    """Save models to local cache file."""
    try:
        cache_data = {
            "fetched_at": datetime.utcnow().isoformat(),
            "source": "blockchain",
            "contract": GRID_CONTRACT,
            "models": models,
        }
        with open(CACHE_FILE, "w") as f:
            json.dump(cache_data, f, indent=2)
        logger.info(f"[MODEL_REF_CHAIN] Cached {len(models)} models to {CACHE_FILE}")
        return True
    except Exception as e:
        logger.error(f"[MODEL_REF_CHAIN] Failed to save cache: {e}")
        return False


def _load_cache() -> Optional[Dict[str, Dict[str, Any]]]:
    """Load models from local cache file."""
    try:
        if not CACHE_FILE.exists():
            return None
        with open(CACHE_FILE) as f:
            cache_data = json.load(f)
        models = cache_data.get("models", {})
        fetched_at = cache_data.get("fetched_at", "unknown")
        logger.info(f"[MODEL_REF_CHAIN] Loaded {len(models)} models from cache (fetched: {fetched_at})")
        return models
    except Exception as e:
        logger.error(f"[MODEL_REF_CHAIN] Failed to load cache: {e}")
        return None


class ModelReference(PrimaryTimedFunction):
    """
    Model reference backed by blockchain with local caching.

    All lookups use in-memory dicts - NO blockchain calls during requests.
    """

    quorum = None
    reference: Dict[str, Dict[str, Any]] = {}
    text_reference: Dict[str, Dict[str, Any]] = {}
    stable_diffusion_names: Set[str] = set()
    text_model_names: Set[str] = set()
    nsfw_models: Set[str] = set()
    controlnet_models: Set[str] = set()
    testing_models = {}
    _name_lookup: Dict[str, str] = {}  # lowercase -> canonical name

    def call_function(self):
        """
        Refresh models from blockchain and update local cache.

        Called on startup and every hour by PrimaryTimedFunction.
        """
        logger.info("[MODEL_REF_CHAIN] Starting model refresh...")

        # Try to fetch fresh from chain
        chain_models = _fetch_models_from_chain()

        if chain_models:
            # Success - use chain data and update cache
            _save_cache(chain_models)
            self._populate_from_dict(chain_models)
        else:
            # Chain fetch failed - try loading from cache
            logger.warning("[MODEL_REF_CHAIN] Chain fetch failed, trying cache...")
            cached_models = _load_cache()
            if cached_models:
                self._populate_from_dict(cached_models)
            else:
                # Try fallback to legacy JSON file (for CI/testing environments)
                logger.warning("[MODEL_REF_CHAIN] No cache available, trying legacy JSON fallback...")
                legacy_models = self._load_legacy_json()
                if legacy_models:
                    self._populate_from_dict(legacy_models)
                    logger.info(f"[MODEL_REF_CHAIN] Loaded {len(legacy_models)} models from legacy JSON")
                else:
                    logger.error("[MODEL_REF_CHAIN] No models available from any source!")
                    self.reference = {}
                    self.stable_diffusion_names = set()

        # Load text models (still from legacy source)
        self._load_text_models()

        logger.info(f"[MODEL_REF_CHAIN] Refresh complete: {len(self.stable_diffusion_names)} image models")

    def _populate_from_dict(self, models: Dict[str, Dict[str, Any]]):
        """Populate internal data structures from model dict."""
        self.reference = models
        self.stable_diffusion_names = set()
        self.nsfw_models = set()
        self.controlnet_models = set()
        # Case-insensitive lookup: maps lowercase name -> actual name
        self._name_lookup: Dict[str, str] = {}

        valid_baselines = {
            "stable diffusion 1",
            "stable diffusion 2",
            "stable diffusion 2 512",
            "stable_diffusion_xl",
            "stable_cascade",
            "flux_1",
            "wan_2_1",
            "wan_2_2",
            "ltx_video",
            "cogvideo",
            "mochi",
        }

        for name, model in models.items():
            baseline = model.get("baseline", "")
            if baseline in valid_baselines:
                self.stable_diffusion_names.add(name)
                # Build case-insensitive lookup
                self._name_lookup[name.lower()] = name

            if model.get("nsfw"):
                self.nsfw_models.add(name)

            if model.get("controlnet") or model.get("type") == "controlnet":
                self.controlnet_models.add(name)

        # Log stats
        flux_count = len([m for m in self.stable_diffusion_names if "flux" in m.lower()])
        video_count = len([m for m in self.stable_diffusion_names if models.get(m, {}).get("type") == "video"])
        logger.info(f"[MODEL_REF_CHAIN] Models: {len(self.stable_diffusion_names)} total, {flux_count} FLUX, {video_count} video")

    def _load_text_models(self):
        """Load text models from legacy source."""
        import requests

        for _riter in range(10):
            try:
                self.text_reference = requests.get(
                    os.getenv(
                        "HORDE_IMAGE_LLM_REFERENCE",
                        "https://raw.githubusercontent.com/db0/AI-Horde-text-model-reference/main/db.json",
                    ),
                    timeout=5,
                ).json()

                self.text_model_names = set()
                for model in self.text_reference:
                    self.text_model_names.add(model)
                    if self.text_reference[model].get("nsfw"):
                        self.nsfw_models.add(model)
                break
            except Exception as err:
                logger.error(f"[MODEL_REF_CHAIN] Error loading text models: {err}")

    def _load_legacy_json(self) -> Dict[str, Dict[str, Any]]:
        """Load models from legacy stable_diffusion.json file as fallback."""
        try:
            legacy_path = Path(__file__).parent.parent / "stable_diffusion.json"
            if legacy_path.exists():
                with open(legacy_path) as f:
                    return json.load(f)
        except Exception as err:
            logger.error(f"[MODEL_REF_CHAIN] Error loading legacy JSON: {err}")
        return {}

    # ========== All methods below use in-memory dicts only ==========
    # NO blockchain calls happen here - pure dict lookups

    def get_image_model_names(self):
        return set(self.reference.keys())

    def get_text_model_names(self):
        return set(self.text_reference.keys())

    def get_model_baseline(self, model_name):
        model_details = self.reference.get(model_name, {})
        if not model_details and "[SDXL]" in model_name:
            return "stable_diffusion_xl"
        if not model_details and "[Flux]" in model_name:
            return "flux_1"
        return model_details.get("baseline", "stable diffusion 1")

    def get_all_model_baselines(self, model_names):
        baselines = set()
        for model_name in model_names:
            model_details = self.reference.get(model_name, {})
            baselines.add(model_details.get("baseline", "stable diffusion 1"))
        return baselines

    def get_model_requirements(self, model_name):
        model_details = self.reference.get(model_name, {})
        return model_details.get("requirements", {})

    def get_model_csam_whitelist(self, model_name):
        model_details = self.reference.get(model_name, {})
        return set(model_details.get("csam_whitelist", []))

    def get_text_model_multiplier(self, model_name):
        usermodel = model_name.split("::")
        if len(usermodel) == 2:
            model_name = usermodel[0]
        if not self.text_reference.get(model_name):
            return 1
        multiplier = int(self.text_reference[model_name]["parameters"]) / 1000000000
        logger.debug(f"{model_name} param multiplier: {multiplier}")
        return multiplier

    def has_inpainting_models(self, model_names):
        for model_name in model_names:
            model_details = self.reference.get(model_name, {})
            if model_details.get("inpainting"):
                return True
        return False

    def has_only_inpainting_models(self, model_names):
        if len(model_names) == 0:
            return False
        for model_name in model_names:
            model_details = self.reference.get(model_name, {})
            if not model_details.get("inpainting"):
                return False
        return True

    def normalize_model_name(self, model_name: str) -> Optional[str]:
        """Return the canonical model name for case-insensitive matching.

        E.g., 'Ltxv' -> 'ltxv', 'FLUX.1-DEV' -> 'FLUX.1-dev'
        Returns None if model not found.
        """
        # First check exact match
        if model_name in self.stable_diffusion_names:
            return model_name
        # Then try case-insensitive lookup
        return self._name_lookup.get(model_name.lower())

    def is_known_image_model(self, model_name):
        # Case-insensitive check
        return self.normalize_model_name(model_name) is not None

    def is_known_text_model(self, model_name):
        usermodel = model_name.split("::")
        if len(usermodel) == 2:
            model_name = usermodel[0]
        return model_name in self.get_text_model_names()

    def has_unknown_models(self, model_names):
        if len(model_names) == 0:
            return False
        return any(not self.is_known_image_model(m) for m in model_names)

    def has_nsfw_models(self, model_names):
        if len(model_names) == 0:
            return False
        return any(m in self.nsfw_models for m in model_names)

    def is_video_model(self, model_name):
        """Check if a model is a video generation model."""
        model_details = self.reference.get(model_name, {})
        if model_details.get("style") == "video":
            return True
        if model_details.get("type") == "video":
            return True
        baseline = model_details.get("baseline", "").lower()
        if baseline in ["wan_2_2", "wan_2_1", "ltx_video", "cogvideo", "mochi"]:
            return True
        name_lower = model_name.lower()
        if any(vid in name_lower for vid in ["t2v", "ti2v", "i2v", "ltxv", "video"]):
            return True
        return False


# Initialize - will fetch from chain or load from cache
model_reference = ModelReference(3600, None)  # Refresh every hour
model_reference.call_function()

logger.info(f"[MODEL_REF_CHAIN] Init complete: {len(model_reference.stable_diffusion_names)} models")

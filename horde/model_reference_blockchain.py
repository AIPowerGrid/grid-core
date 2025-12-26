# SPDX-FileCopyrightText: 2024 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Blockchain-based model reference for AI Power Grid.
Uses the ModelVault contract on Base Mainnet as the single source of truth.
"""

import os
from datetime import datetime
from typing import Dict, List, Set, Optional, Any
import logging

from horde.logger import logger
from horde.threads import PrimaryTimedFunction

# Import blockchain client from modelvault module
try:
    from horde.modelvault import (
        get_modelvault_client,
        OnChainModelInfo,
        ModelType,
    )
    BLOCKCHAIN_AVAILABLE = True
except ImportError:
    logger.warning("[MODEL_REFERENCE] Blockchain client not available, will use empty model list")
    BLOCKCHAIN_AVAILABLE = False

def write_debug_log(message: str):
    """Write debug info to a file for troubleshooting."""
    try:
        log_path = "model_reference_debug.log"
        with open(log_path, "a") as f:
            f.write(f"{datetime.utcnow().isoformat()} - {message}\n")
        print(f"[MODEL_REF_DEBUG] {message}")
    except Exception as e:
        print(f"[MODEL_REFERENCE] Could not write debug log: {e}")


class ModelReference(PrimaryTimedFunction):
    """Model reference using blockchain as single source of truth."""
    
    quorum = None
    reference: Dict[str, Dict[str, Any]] = {}
    text_reference: Dict[str, Dict[str, Any]] = {}
    stable_diffusion_names: Set[str] = set()
    text_model_names: Set[str] = set()
    nsfw_models: Set[str] = set()
    controlnet_models: Set[str] = set()
    testing_models = {}
    
    # Cached blockchain models
    _chain_models: Dict[str, OnChainModelInfo] = {}

    def call_function(self):
        """Retrieves model registry from blockchain."""
        write_debug_log("Starting blockchain model reference load")
        logger.warning("[MODEL_REFERENCE] Loading models from blockchain (Base Mainnet)")
        
        if not BLOCKCHAIN_AVAILABLE:
            logger.error("[MODEL_REFERENCE] Blockchain client not available!")
            self.reference = {}
            self.stable_diffusion_names = set()
            return
        
        # Get blockchain client
        client = get_modelvault_client()
        
        if not client.enabled:
            logger.warning("[MODEL_REFERENCE] Blockchain client disabled, using empty model list")
            self.reference = {}
            self.stable_diffusion_names = set()
            return
        
        # Fetch all models from blockchain
        try:
            self._chain_models = client.fetch_all_models()
            write_debug_log(f"Loaded {len(self._chain_models)} models from blockchain")
            logger.warning(f"[MODEL_REFERENCE] Loaded {len(self._chain_models)} models from blockchain")
            
            # Convert blockchain models to legacy reference format
            self.reference = {}
            self.stable_diffusion_names = set()
            self.nsfw_models = set()
            self.controlnet_models = set()
            
            for display_name, model_info in self._chain_models.items():
                # Map blockchain model to legacy format
                baseline = self._get_baseline_for_model(model_info)
                
                self.reference[display_name] = {
                    "name": display_name,
                    "baseline": baseline,
                    "type": self._get_type_for_model(model_info),
                    "nsfw": model_info.is_nsfw,
                    "style": self._get_style_for_model(model_info),
                    "description": model_info.description or f"{display_name} model",
                    "inpainting": model_info.inpainting,
                    "img2img": model_info.img2img,
                    "controlnet": model_info.controlnet,
                    "lora": model_info.lora,
                }
                
                # Add to appropriate sets
                if baseline in {
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
                }:
                    self.stable_diffusion_names.add(display_name)
                
                if model_info.is_nsfw:
                    self.nsfw_models.add(display_name)
                
                if model_info.controlnet:
                    self.controlnet_models.add(display_name)
            
            # Log recognized models
            write_debug_log(f"Recognized {len(self.stable_diffusion_names)} models with valid baselines")
            logger.warning(f"[MODEL_REFERENCE] Recognized {len(self.stable_diffusion_names)} models with valid baselines")
            
            flux_models = [m for m in self.stable_diffusion_names if 'flux' in m.lower()]
            wan_models = [m for m in self.stable_diffusion_names if 'wan' in m.lower()]
            ltx_models = [m for m in self.stable_diffusion_names if 'ltx' in m.lower()]
            
            write_debug_log(f"FLUX models ({len(flux_models)}): {flux_models}")
            write_debug_log(f"WAN models ({len(wan_models)}): {wan_models}")
            write_debug_log(f"LTX models ({len(ltx_models)}): {ltx_models}")
            
        except Exception as e:
            logger.error(f"[MODEL_REFERENCE] Error loading models from blockchain: {e}")
            write_debug_log(f"Error loading blockchain models: {e}")
            # Keep existing models on error
            if not self.reference:
                self.reference = {}
                self.stable_diffusion_names = set()
        
        # Text models - still load from legacy source for now
        # (text models not yet on blockchain)
        self._load_text_models()
    
    def _get_baseline_for_model(self, model_info: OnChainModelInfo) -> str:
        """Map blockchain model to baseline string."""
        name_lower = model_info.display_name.lower()
        file_lower = (model_info.file_name or "").lower()
        
        # Video models
        if model_info.model_type == ModelType.VIDEO_MODEL:
            if "wan" in name_lower or "wan" in file_lower:
                if "2.2" in name_lower or "2_2" in name_lower:
                    return "wan_2_2"
                elif "2.1" in name_lower or "2_1" in name_lower:
                    return "wan_2_1"
                return "wan_2_2"  # Default to 2.2
            elif "ltx" in name_lower or "ltx" in file_lower:
                return "ltx_video"
            elif "cogvideo" in name_lower:
                return "cogvideo"
            elif "mochi" in name_lower:
                return "mochi"
            return "wan_2_2"  # Default video baseline
        
        # Image models
        if "flux" in name_lower or "flux" in file_lower:
            return "flux_1"
        elif "sdxl" in name_lower or "xl" in name_lower:
            return "stable_diffusion_xl"
        elif "sd2" in name_lower or "2.1" in name_lower:
            return "stable diffusion 2"
        elif "cascade" in name_lower:
            return "stable_cascade"
        
        # Default to SD1.5
        return "stable diffusion 1"
    
    def _get_type_for_model(self, model_info: OnChainModelInfo) -> str:
        """Get model type string."""
        if model_info.controlnet:
            return "controlnet"
        elif model_info.lora:
            return "lora"
        elif model_info.model_type == ModelType.VIDEO_MODEL:
            return "video"
        return "checkpoint"
    
    def _get_style_for_model(self, model_info: OnChainModelInfo) -> str:
        """Get model style string."""
        if model_info.model_type == ModelType.VIDEO_MODEL:
            return "video"
        
        # Try to infer from name/description
        name_lower = model_info.display_name.lower()
        desc_lower = (model_info.description or "").lower()
        
        if "anime" in name_lower or "anime" in desc_lower:
            return "anime"
        elif "realistic" in name_lower or "photo" in name_lower or "realistic" in desc_lower:
            return "realistic"
        elif "artistic" in name_lower or "art" in name_lower:
            return "artistic"
        
        return "generalist"
    
    def _load_text_models(self):
        """Load text models from legacy source (not yet on blockchain)."""
        import requests
        
        for _riter in range(10):
            try:
                self.text_reference = requests.get(
                    os.getenv(
                        "HORDE_IMAGE_LLM_REFERENCE",
                        "https://raw.githubusercontent.com/db0/AI-Horde-text-model-reference/main/db.json",
                    ),
                    timeout=2,
                ).json()
                
                self.text_model_names = set()
                for model in self.text_reference:
                    self.text_model_names.add(model)
                    if self.text_reference[model].get("nsfw"):
                        self.nsfw_models.add(model)
                break
            except Exception as err:
                logger.error(f"Error when downloading text models list: {err}")

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

    def is_known_image_model(self, model_name):
        return model_name in self.get_image_model_names()

    def is_known_text_model(self, model_name):
        usermodel = model_name.split("::")
        if len(usermodel) == 2:
            model_name = usermodel[0]
        return model_name in self.get_text_model_names()

    def has_unknown_models(self, model_names):
        if len(model_names) == 0:
            return False
        if any(not self.is_known_image_model(m) for m in model_names):
            return True
        return False

    def has_nsfw_models(self, model_names):
        if len(model_names) == 0:
            return False
        if any(m in model_reference.nsfw_models for m in model_names):
            return True
        return False

    def is_video_model(self, model_name):
        """Check if a model is a video generation model."""
        model_details = self.reference.get(model_name, {})
        # Check style field
        if model_details.get("style") == "video":
            return True
        # Check baseline for known video model architectures
        baseline = model_details.get("baseline", "").lower()
        if baseline in ["wan_2_2", "wan_2_1", "ltx_video", "cogvideo", "mochi"]:
            return True
        # Check model name patterns for video models
        name_lower = model_name.lower()
        if any(vid in name_lower for vid in ["t2v", "ti2v", "i2v", "ltxv", "video"]):
            return True
        return False


# Initialize the model reference
model_reference = ModelReference(3600, None)
model_reference.call_function()

# Log final state
write_debug_log(f"=== BLOCKCHAIN INIT COMPLETE ===")
write_debug_log(f"Total models recognized: {len(model_reference.stable_diffusion_names)}")
print(f"[MODEL_REFERENCE] Blockchain init complete. {len(model_reference.stable_diffusion_names)} models recognized")
logger.warning(f"[MODEL_REFERENCE] Blockchain init complete. {len(model_reference.stable_diffusion_names)} models recognized")

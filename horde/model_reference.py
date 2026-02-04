# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# DEPRECATED: This module is unused in production. All code imports
# model_reference_blockchain instead. Kept for fallback/tests only.

import os

import requests

from horde.logger import logger
from horde.threads import PrimaryTimedFunction


def write_debug_log(message: str) -> None:
    """No-op; kept for compatibility. Use logger if debugging."""


class ModelReference(PrimaryTimedFunction):
    quorum = None
    reference = None
    text_reference = None
    stable_diffusion_names = set()
    text_model_names = set()
    nsfw_models = set()
    controlnet_models = set()
    # Workaround because users lacking customizer role are getting models not in the reference stripped away.
    # However due to a racing or caching issue, this causes them to still pick jobs using those models
    # Need to investigate more to remove this workaround
    testing_models = {}

    def call_function(self):
        """Retrieves to image and text model reference and stores in it a var"""
        # If it's running in SQLITE_MODE, it means it's a test and we never want to grab the quorum
        # We don't want to report on any random model name a client might request
        # ALWAYS use AIPowerGrid reference - ignore any env var override
        # This ensures we always use our own model reference, not Haidra-Org's
        ref_url = "https://raw.githubusercontent.com/AIPowerGrid/grid-image-model-reference/main/stable_diffusion.json"
        env_override = os.getenv("HORDE_IMAGE_COMPVIS_REFERENCE")
        if env_override:
            write_debug_log(f"WARNING: HORDE_IMAGE_COMPVIS_REFERENCE env var is set to '{env_override}' but IGNORING it!")
            logger.warning(f"[MODEL_REFERENCE] Ignoring HORDE_IMAGE_COMPVIS_REFERENCE env var ({env_override})")
        write_debug_log(f"Starting model reference load from: {ref_url}")
        logger.warning(f"[MODEL_REFERENCE] Starting model reference load from: {ref_url}")
        for _riter in range(10):
            try:
                self.reference = requests.get(ref_url, timeout=5).json()
                write_debug_log(f"Loaded {len(self.reference)} models from JSON")
                logger.warning(f"[MODEL_REFERENCE] Loaded {len(self.reference)} models from JSON")
                # Try to load diffusers reference, but don't fail if it's unavailable
                try:
                    # ALWAYS use AIPowerGrid reference - ignore any env var override
                    diffusers_url = "https://raw.githubusercontent.com/AIPowerGrid/grid-image-model-reference/main/diffusers.json"
                    diffusers_response = requests.get(diffusers_url, timeout=2)
                    if diffusers_response.status_code == 200:
                        diffusers = diffusers_response.json()
                        self.reference.update(diffusers)
                    else:
                        logger.warning(f"Diffusers reference returned {diffusers_response.status_code}, skipping")
                except Exception as diffusers_err:
                    logger.warning(f"Could not load diffusers reference: {diffusers_err}")
                # logger.debug(self.reference)
                self.stable_diffusion_names = set()
                for model in self.reference:
                    if self.reference[model].get("baseline") in {
                        # Image models
                        "stable_diffusion_1",
                        "stable_diffusion_2",
                        "stable_diffusion_xl",
                        "stable_cascade",
                        "flux_1",
                        "flux_2",
                        "z_image_turbo",
                        # Video models
                        "wan_2_1",
                        "wan_2_2",
                        "ltx_video",
                        "ltx_video_2",
                        "cogvideo",
                        "mochi",
                        "hunyuan_video",
                    }:
                        self.stable_diffusion_names.add(model)
                        if self.reference[model].get("nsfw"):
                            self.nsfw_models.add(model)
                        if self.reference[model].get("type") == "controlnet":
                            self.controlnet_models.add(model)

                # Debug: Log recognized models - using WARNING to ensure visibility
                write_debug_log(f"Recognized {len(self.stable_diffusion_names)} models with valid baselines")
                logger.warning(f"[MODEL_REFERENCE] Recognized {len(self.stable_diffusion_names)} models with valid baselines")
                flux_models = [m for m in self.stable_diffusion_names if "flux" in m.lower() or "FLUX" in m]
                wan_models = [m for m in self.stable_diffusion_names if "wan" in m.lower()]
                write_debug_log(f"FLUX models ({len(flux_models)}): {flux_models}")
                write_debug_log(f"WAN models ({len(wan_models)}): {wan_models}")
                logger.warning(f"[MODEL_REFERENCE] FLUX models ({len(flux_models)}): {flux_models}")
                logger.warning(f"[MODEL_REFERENCE] WAN models ({len(wan_models)}): {wan_models}")
                # Log baselines found in reference
                baselines_found = set()
                for model in self.reference:
                    baseline = self.reference[model].get("baseline")
                    if baseline:
                        baselines_found.add(baseline)
                write_debug_log(f"Baselines in reference: {baselines_found}")
                write_debug_log(f"ALL recognized models: {list(self.stable_diffusion_names)}")
                logger.warning(f"[MODEL_REFERENCE] Baselines in reference: {baselines_found}")
                break
            except Exception as e:
                write_debug_log(f"Error loading models (attempt {_riter + 1}/10): {e}")
                logger.error(f"[MODEL_REFERENCE] Error loading models (attempt {_riter + 1}/10): {e}")

        for _riter in range(10):
            try:
                self.text_reference = requests.get(
                    os.getenv(
                        "HORDE_IMAGE_LLM_REFERENCE",
                        "https://raw.githubusercontent.com/db0/AI-Horde-text-model-reference/main/db.json",
                    ),
                    timeout=2,
                ).json()
                # logger.debug(self.reference)
                self.text_model_names = set()
                for model in self.text_reference:
                    self.text_model_names.add(model)
                    if self.text_reference[model].get("nsfw"):
                        self.nsfw_models.add(model)
                break
            except Exception as err:
                logger.error(f"Error when downloading known models list: {err}")

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
        return model_details.get("baseline", "stable_diffusion_1")

    def get_all_model_baselines(self, model_names):
        baselines = set()
        for model_name in model_names:
            model_details = self.reference.get(model_name, {})
            baselines.add(model_details.get("baseline", "stable_diffusion_1"))
        return baselines

    def get_model_requirements(self, model_name):
        model_details = self.reference.get(model_name, {})
        return model_details.get("requirements", {})

    def get_model_csam_whitelist(self, model_name):
        model_details = self.reference.get(model_name, {})
        return set(model_details.get("csam_whitelist", []))

    def get_text_model_multiplier(self, model_name):
        # To avoid doing this calculations all the time
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
        # If it's a named model, we check if we can find it without the username
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
        # if self.has_unknown_models(model_names):
        #     return True
        return False

    def is_video_model(self, model_name):
        """Check if a model is a video generation model.

        Video models have "style": "video" in stable_diffusion.json.
        Also checks for known video model baselines.
        """
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


model_reference = ModelReference(3600, None)
model_reference.call_function()

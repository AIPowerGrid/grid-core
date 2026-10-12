# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded LTX-2.5 execution units shared by recipes, HTTP and billing."""

import copy
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SourceMode = Literal["generated", "audio", "video"]


class LtxVideoContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract_schema: Literal["aipg.ltx-video.v1"] = Field(alias="schema")
    variant: Literal["distilled", "dev"]
    max_frames: int = Field(ge=9, le=337)
    max_pixels: int = Field(ge=256 * 256, le=960 * 544)
    max_keyframes: int = Field(default=0, ge=0, le=16)
    source_modes: list[Literal["audio", "video"]] = Field(default_factory=list, max_length=2)
    appearance_profile: Literal["ingredients-dev-v1"] | None = None

    @model_validator(mode="after")
    def validate_capacity(self):
        if self.max_frames % 8 != 1:
            raise ValueError("max_frames must be 8n+1")
        if self.appearance_profile is not None:
            if (self.variant != "dev" or self.max_frames != 121 or self.max_pixels != 768 * 448
                    or self.max_keyframes != 0 or set(self.source_modes) != {"audio", "video"}):
                raise ValueError("Ingredients requires the separate 121-frame Dev profile")
        elif self.variant == "dev" and self.max_frames > 81:
            raise ValueError("dev capacity above 81 frames is not qualified")
        if len(self.source_modes) != len(set(self.source_modes)):
            raise ValueError("source modes must be unique")
        if self.source_modes and self.variant != "distilled" and self.appearance_profile is None:
            raise ValueError("source conditioning is qualified only on the distilled scaffold")
        return self


class LtxKeyframe(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    frame: int = Field(ge=0, le=336)
    strength: float = Field(default=1.0, ge=0, le=1)


class LtxVideoInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    prompt: str = Field(default="", max_length=8000)
    negative_prompt: str = Field(default="", max_length=8000)
    seed: int | None = Field(default=None, ge=0, le=2**53 - 1)
    image: str | None = None  # Worker-bound upload filename, never fetched here.
    width: int = Field(default=960, ge=256, le=960)
    height: int = Field(default=544, ge=256, le=960)
    frames: int | None = Field(default=None, ge=9, le=337)
    seconds: float | None = Field(default=None, gt=0, le=337 / 24)
    keyframes: list[LtxKeyframe] = Field(default_factory=list, max_length=16)
    fps: Literal[24] = 24
    steps: int | None = Field(default=None, ge=4, le=40)
    cfg: float | None = Field(default=None, ge=1, le=5)
    sampler: Literal["euler_ancestral_cfg_pp"] = "euler_ancestral_cfg_pp"
    audio: Literal[True] = True
    source_mode: SourceMode = "generated"
    guide_strength: float | None = Field(default=None, gt=0, le=1)
    reference_image: bool = False  # Presence only; private bytes use a separate source role.
    reference_strength: float | None = Field(default=None, gt=0, le=1)
    reference_attention_strength: float | None = Field(default=None, gt=0, le=1)
    motion_attention_strength: float | None = Field(default=None, gt=0, le=1)

    @field_validator("fps", "audio", mode="before")
    @classmethod
    def require_primitive_type(cls, value, info):
        if type(value) is not (int if info.field_name == "fps" else bool):
            raise ValueError("fps must be an integer and audio must be a boolean")
        return value


def normalize_timing(contract: LtxVideoContract, *, frames: int | None = None,
                     seconds: float | None = None, fps: int = 24) -> dict:
    """Quote duration only; this does not validate sources or authorize execution."""
    inputs = LtxVideoInputs.model_validate({"frames": frames, "seconds": seconds, "fps": fps})
    if frames is None:
        frames = (121 if contract.appearance_profile else 81) if seconds is None else (
            1 + 8 * math.floor((inputs.seconds * inputs.fps - 1) / 8 + 0.5))
    elif seconds is not None and not math.isclose(seconds, frames / fps, abs_tol=1e-9):
        raise ValueError("seconds and frames must describe the same duration")
    if frames < 9 or frames > contract.max_frames or frames % 8 != 1:
        raise ValueError(f"LTX-2.5 frames must be 8n+1 between 9 and {contract.max_frames}")
    if contract.appearance_profile and frames != 121:
        raise ValueError("Ingredients requires 121 frames")
    return {"frames": frames, "fps": fps, "seconds": frames / fps}


def normalize(contract: LtxVideoContract, values: dict) -> dict:
    """Seconds select the nearest 8n+1 clip; explicit frames are never rounded."""
    appearance = contract.appearance_profile is not None
    values = dict(values)
    if appearance:
        values.setdefault("width", 768)
        values.setdefault("height", 448)
        values.setdefault("frames", 121 if values.get("seconds") is None else None)
    inputs = LtxVideoInputs.model_validate(values)
    if appearance:
        if (not inputs.reference_image or inputs.source_mode == "generated" or inputs.image
                or inputs.keyframes or (inputs.width, inputs.height) != (768, 448)):
            raise ValueError("Ingredients requires a separate appearance image and exact-duration source audio")
    elif (inputs.reference_image or inputs.reference_strength is not None
          or inputs.reference_attention_strength is not None or inputs.motion_attention_strength is not None):
        raise ValueError("recipe does not support appearance references")
    if inputs.source_mode != "generated" and inputs.source_mode not in contract.source_modes:
        raise ValueError("recipe does not support this source conditioning mode")
    if inputs.guide_strength is not None and inputs.source_mode != "video":
        raise ValueError("guide strength requires a video reference")
    if inputs.motion_attention_strength is not None and inputs.source_mode != "video":
        raise ValueError("motion attention requires a video reference")
    if inputs.width % 32 or inputs.height % 32:
        raise ValueError("LTX-2.5 dimensions must be multiples of 32")
    if inputs.width * inputs.height > contract.max_pixels:
        raise ValueError(f"LTX-2.5 size exceeds {contract.max_pixels} pixels")
    frames = normalize_timing(contract, frames=inputs.frames, seconds=inputs.seconds, fps=inputs.fps)["frames"]
    steps = inputs.steps if inputs.steps is not None else (8 if contract.variant == "distilled" else 40)
    cfg = inputs.cfg if inputs.cfg is not None else (1.0 if contract.variant == "distilled" else 2.0)
    if contract.variant == "distilled" and (steps != 8 or cfg != 1):
        raise ValueError("LTX-2.5 distilled requires 8 steps and CFG 1")
    if appearance and (frames != 121 or steps != 40 or cfg != 2):
        raise ValueError("Ingredients requires 121 frames, 40 steps and CFG 2")
    pins = inputs.keyframes
    positions = [pin.frame for pin in pins]
    if len(pins) > contract.max_keyframes or (pins and (
        positions[0] != 0 or positions != sorted(set(positions)) or positions[-1] >= frames
    )):
        raise ValueError("keyframes must start at zero and be strictly ordered within the clip")
    result = {**inputs.model_dump(exclude_none=True), "frames": frames,
              "seconds": frames / inputs.fps, "steps": steps, "cfg": cfg}
    if not pins:
        result.pop("keyframes")
    if not inputs.reference_image:
        result.pop("reference_image")
    if appearance:
        result["appearance_profile"] = contract.appearance_profile
        result["reference_strength"] = inputs.reference_strength if inputs.reference_strength is not None else 1.0
    if inputs.source_mode == "generated":
        result.pop("source_mode")
    elif inputs.source_mode == "video":
        result["guide_strength"] = inputs.guide_strength if inputs.guide_strength is not None else 0.5 if appearance else 0.85
        if appearance:
            result["motion_attention_strength"] = inputs.motion_attention_strength if inputs.motion_attention_strength is not None else 0.25
    return result


def expand_sources(spec: dict, mode: SourceMode, guide_strength: float = 0.85,
                   motion_attention_strength: float | None = None) -> list[dict]:
    """Bind input roles to a fixed, Core-owned audio/Union-Control scaffold."""
    if mode == "generated":
        return []
    expected = {"model": "UNETLoader", "video_vae": "VAELoader", "audio_vae": "VAELoader",
                "latent_audio": "LTXVEmptyLatentAudio", "av_latent": "LTXVConcatAVLatent",
                "guider": "CFGGuider", "crop": "LTXVCropGuides", "video": "CreateVideo",
                "decode_audio": "LTXVAudioVAEDecode"}
    added = {"source_audio", "encode_audio", "audio_mask", "source_video", "source_frames",
             "source_edges", "control_model", "video_guide"}
    if (mode not in {"audio", "video"} or not 0 < guide_strength <= 1
            or (motion_attention_strength is not None and
                (mode != "video" or not 0 < motion_attention_strength <= 1))
            or added.intersection(spec)
            or any(not isinstance(spec.get(name), dict)
                   or spec[name].get("class_type") != kind
                   or not isinstance(spec[name].get("inputs"), dict)
                   for name, kind in expected.items())
            or spec["av_latent"]["inputs"].get("audio_latent") != ["latent_audio", 0]
            or spec["video"]["inputs"].get("audio") != ["decode_audio", 0]):
        raise ValueError("recipe does not contain the governed source scaffold")
    spec["source_audio"] = {"class_type": "LoadAudio", "inputs": {"audio": "grid_reference.wav"}}
    spec["encode_audio"] = {"class_type": "LTXVAudioVAEEncode", "inputs": {
        "audio": ["source_audio", 0], "audio_vae": ["audio_vae", 0]}}
    spec["audio_mask"] = {"class_type": "SolidMask", "inputs": {"value": 0.0, "width": 1, "height": 1}}
    spec["latent_audio"] = {"class_type": "SetLatentNoiseMask", "inputs": {
        "samples": ["encode_audio", 0], "mask": ["audio_mask", 0]}}
    spec["video"]["inputs"]["audio"] = ["source_audio", 0]
    del spec["decode_audio"]
    bindings = [{"role": "reference_audio", "path": "source_audio.inputs.audio"}]
    if mode == "video":
        # A motion reference is not an opening image. Keep the existing video
        # latent/pins, if any, and add control independently of first-frame I2V.
        latent = spec["av_latent"]["inputs"]["video_latent"]
        positive, negative = (spec["guider"]["inputs"][key] for key in ("positive", "negative"))
        spec["control_model"] = {"class_type": "LTXICLoRALoaderModelOnly", "inputs": {
            "model": spec["guider"]["inputs"]["model"], "lora_name": "ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors",
            "strength_model": 1.0}}
        spec["source_video"] = {"class_type": "LoadVideo", "inputs": {"file": "grid_reference.mp4"}}
        spec["source_frames"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["source_video", 0]}}
        spec["source_edges"] = {"class_type": "Canny", "inputs": {
            "image": ["source_frames", 0], "low_threshold": 0.35, "high_threshold": 0.75}}
        spec["video_guide"] = {"class_type": "LTXAddVideoICLoRAGuide", "inputs": {
            "positive": positive, "negative": negative, "vae": ["video_vae", 0], "latent": latent,
            "image": ["source_edges", 0], "frame_idx": 0, "strength": guide_strength,
            "latent_downscale_factor": ["control_model", 1], "crop": "disabled",
            "use_tiled_encode": False, "tile_size": 256, "tile_overlap": 64}}
        if motion_attention_strength is not None:
            spec["video_guide"]["class_type"] = "LTXAddVideoICLoRAGuideAdvanced"
            spec["video_guide"]["inputs"]["attention_strength"] = motion_attention_strength
        spec["av_latent"]["inputs"]["video_latent"] = ["video_guide", 2]
        spec["guider"]["inputs"].update(model=["control_model", 0], positive=["video_guide", 0], negative=["video_guide", 1])
        spec["crop"]["inputs"].update(positive=["video_guide", 0], negative=["video_guide", 1])
        bindings.append({"role": "reference_video", "path": "source_video.inputs.file"})
    return bindings


def expand_appearance(spec: dict, strength: float = 1.0,
                      attention_strength: float | None = None) -> list[dict]:
    """Apply the fixed Ingredients guide, never a frame-zero image latent."""
    expected = {"model": "UNETLoader", "latent_video": "EmptyLTXVLatentVideo",
                "av_latent": "LTXVConcatAVLatent", "guider": "CFGGuider",
                "crop": "LTXVCropGuides", "sigmas": "LTXVScheduler",
                "conditioning": "LTXVConditioning", "video_vae": "VAELoader"}
    added = {"reference_model", "reference_image", "reference_scale", "reference_sheet", "reference_guide"}
    if (not 0 < strength <= 1 or added.intersection(spec)
            or (attention_strength is not None and
                (type(attention_strength) not in (int, float) or not 0 < attention_strength <= 1))
            or any(not isinstance(spec.get(name), dict) or spec[name].get("class_type") != kind
                   or not isinstance(spec[name].get("inputs"), dict) for name, kind in expected.items())
            or spec["model"]["inputs"].get("unet_name") != "ltx-2.5-22b-dev-transformer-bf16.safetensors"
            or spec["av_latent"]["inputs"].get("video_latent") != ["latent_video", 0]
            or spec["guider"]["inputs"].get("model") != ["model", 0]
            or spec["latent_video"]["inputs"] != {"width": 768, "height": 448, "length": 121, "batch_size": 1}):
        raise ValueError("recipe does not contain the governed Ingredients scaffold")
    spec["reference_model"] = {"class_type": "LTXICLoRALoaderModelOnly", "inputs": {
        "model": ["model", 0], "lora_name": "ltx-2.5-22b-ic-lora-ingredients-0.9.safetensors",
        "strength_model": 1.0}}
    spec["reference_image"] = {"class_type": "LoadImage", "inputs": {"image": "grid_reference.png"}}
    spec["reference_scale"] = {"class_type": "ImageScale", "inputs": {
        "image": ["reference_image", 0], "upscale_method": "lanczos", "width": 768, "height": 448, "crop": "center"}}
    spec["reference_sheet"] = {"class_type": "RepeatImageBatch", "inputs": {"image": ["reference_scale", 0], "amount": 121}}
    spec["reference_guide"] = {"class_type": "LTXAddVideoICLoRAGuide", "inputs": {
        "positive": ["conditioning", 0], "negative": ["conditioning", 1], "vae": ["video_vae", 0],
        "latent": ["latent_video", 0], "image": ["reference_sheet", 0], "frame_idx": 0,
        "strength": strength, "latent_downscale_factor": ["reference_model", 1], "crop": "disabled",
        "use_tiled_encode": False, "tile_size": 256, "tile_overlap": 64}}
    if attention_strength is not None:
        spec["reference_guide"]["class_type"] = "LTXAddVideoICLoRAGuideAdvanced"
        spec["reference_guide"]["inputs"]["attention_strength"] = attention_strength
    spec["av_latent"]["inputs"]["video_latent"] = ["reference_guide", 2]
    spec["guider"]["inputs"].update(model=["reference_model", 0], positive=["reference_guide", 0], negative=["reference_guide", 1])
    spec["crop"]["inputs"].update(positive=["reference_guide", 0], negative=["reference_guide", 1])
    return [{"role": "reference_image", "path": "reference_image.inputs.image"}]


def expand_keyframes(spec: dict, pins: list[dict]) -> list[dict]:
    """Expand only the governed LTX first-image scaffold, never uploaded nodes."""
    expected = {"key_0": "LoadImage", "scale_0": "ImageScale", "preprocess_0": "LTXVPreprocess",
                "first_0": "LTXVImgToVideoInplace", "av_latent": "LTXVConcatAVLatent",
                "guider": "CFGGuider", "crop": "LTXVCropGuides",
                "conditioning": "LTXVConditioning", "video_vae": "VAELoader"}
    if not pins or any(not isinstance(spec.get(name), dict)
                       or spec[name].get("class_type") != kind
                       or not isinstance(spec[name].get("inputs"), dict)
                       for name, kind in expected.items()):
        raise ValueError("recipe does not contain the governed keyframe scaffold")
    spec["first_0"]["inputs"]["strength"] = pins[0]["strength"]
    positive, negative, latent = ["conditioning", 0], ["conditioning", 1], ["first_0", 0]
    bindings = [{"image_index": 0, "path": "key_0.inputs.image"}]
    for index, pin in enumerate(pins[1:], 1):
        for kind in ("key", "scale", "preprocess"):
            name = f"{kind}_{index}"
            if name in spec or f"guide_{index}" in spec:
                raise ValueError("recipe collides with generated guide nodes")
            spec[name] = copy.deepcopy(spec[f"{kind}_0"])
        spec[f"scale_{index}"]["inputs"]["image"] = [f"key_{index}", 0]
        spec[f"preprocess_{index}"]["inputs"]["image"] = [f"scale_{index}", 0]
        name = f"guide_{index}"
        spec[name] = {"class_type": "LTXVAddGuide", "inputs": {
            "positive": positive, "negative": negative, "vae": ["video_vae", 0],
            "latent": latent, "image": [f"preprocess_{index}", 0],
            "frame_idx": pin["frame"], "strength": pin["strength"],
        }}
        positive, negative, latent = [name, 0], [name, 1], [name, 2]
        bindings.append({"image_index": index, "path": f"key_{index}.inputs.image"})
    spec["av_latent"]["inputs"]["video_latent"] = latent
    for name in ("guider", "crop"):
        spec[name]["inputs"].update(positive=positive, negative=negative)
    if spec.get("sigmas", {}).get("class_type") == "LTXVScheduler":
        spec["sigmas"]["inputs"]["latent"] = latent
    return bindings


def parse_size(size: str) -> tuple[int, int]:
    try:
        width, height = (int(value) for value in size.lower().split("x"))
    except (ValueError, AttributeError):
        raise ValueError("size must be formatted as WIDTHxHEIGHT") from None
    return width, height


def source_size(contract: LtxVideoContract, width: int, height: int) -> tuple[int, int]:
    """Fit a source aspect under the same pixel ceiling before upload/dispatch."""
    scale = min(960 / width, 960 / height, math.sqrt(contract.max_pixels / (width * height)))
    out_width = max(256, int(width * scale / 32) * 32)
    out_height = max(256, int(height * scale / 32) * 32)
    while out_width * out_height > contract.max_pixels:
        if out_width >= out_height:
            out_width -= 32
        else:
            out_height -= 32
    return out_width, out_height

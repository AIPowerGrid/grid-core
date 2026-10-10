# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded LTX-2.5 execution units shared by recipes, HTTP and billing."""

import copy
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LtxVideoContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract_schema: Literal["aipg.ltx-video.v1"] = Field(alias="schema")
    variant: Literal["distilled", "dev"]
    max_frames: int = Field(ge=9, le=337)
    max_pixels: int = Field(ge=256 * 256, le=960 * 544)
    max_keyframes: int = Field(default=0, ge=0, le=16)

    @model_validator(mode="after")
    def validate_capacity(self):
        if self.max_frames % 8 != 1:
            raise ValueError("max_frames must be 8n+1")
        if self.variant == "dev" and self.max_frames > 81:
            raise ValueError("dev capacity above 81 frames is not qualified")
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

    @field_validator("fps", "audio", mode="before")
    @classmethod
    def require_primitive_type(cls, value, info):
        if type(value) is not (int if info.field_name == "fps" else bool):
            raise ValueError("fps must be an integer and audio must be a boolean")
        return value


def normalize(contract: LtxVideoContract, values: dict) -> dict:
    """Seconds select the nearest 8n+1 clip; explicit frames are never rounded."""
    inputs = LtxVideoInputs.model_validate(values)
    if inputs.width % 32 or inputs.height % 32:
        raise ValueError("LTX-2.5 dimensions must be multiples of 32")
    if inputs.width * inputs.height > contract.max_pixels:
        raise ValueError(f"LTX-2.5 size exceeds {contract.max_pixels} pixels")
    frames = inputs.frames
    if frames is None:
        frames = 81 if inputs.seconds is None else 1 + 8 * math.floor((inputs.seconds * inputs.fps - 1) / 8 + 0.5)
    elif inputs.seconds is not None and not math.isclose(inputs.seconds, frames / inputs.fps, abs_tol=1e-9):
        raise ValueError("seconds and frames must describe the same duration")
    if frames < 9 or frames > contract.max_frames or frames % 8 != 1:
        raise ValueError(f"LTX-2.5 frames must be 8n+1 between 9 and {contract.max_frames}")
    steps = inputs.steps if inputs.steps is not None else (8 if contract.variant == "distilled" else 40)
    cfg = inputs.cfg if inputs.cfg is not None else (1.0 if contract.variant == "distilled" else 2.0)
    if contract.variant == "distilled" and (steps != 8 or cfg != 1):
        raise ValueError("LTX-2.5 distilled requires 8 steps and CFG 1")
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
    return result


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

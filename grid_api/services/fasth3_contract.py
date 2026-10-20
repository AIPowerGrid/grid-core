# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline FastH3 qualification compiler, deliberately outside live recipe admission.

The live registry does not parse this schema. No model registration, pricing-book
entry, credit authorization, source decoding, storage or network request occurs.
"""

import copy
import json
import math
import re
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MODEL = "FastH3 Omni NVFP4 QAD"
CANDIDATE = Path(__file__).resolve().parents[2] / "recipes/qualification/fasth3/omni-nvfp4-qad-v1.json"


class FastH3Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract_schema: Literal["aipg.fasth3-video.v1"] = Field(alias="schema")
    variant: Literal["omni-nvfp4-qad"]
    min_frames: int = Field(ge=124, le=362)
    max_frames: int = Field(ge=124, le=362)
    canvases: list[Literal["1344x768", "768x1344", "768x768"]] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_matrix(self):
        if (
            self.min_frames > self.max_frames
            or self.min_frames % 17 != 5
            or self.max_frames % 17 != 5
            or len(set(self.canvases)) != len(self.canvases)
        ):
            raise ValueError("invalid FastH3 candidate matrix")
        return self


class FastH3Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    prompt: str = Field(default="", max_length=8000)
    seed: int = Field(default=29, ge=0, le=2**53 - 1)
    frames: int = Field(default=124, ge=124, le=362)
    seconds: float | None = Field(default=None, gt=0, le=362 / 24)
    width: int = Field(default=1344)
    height: int = Field(default=768)
    fps: Literal[24] = 24
    steps: Literal[8] = 8
    cfg: Literal[1.0] = 1.0
    sampler: Literal["res_multistep"] = "res_multistep"
    scheduler: Literal["simple"] = "simple"
    audio: Literal[True] = True
    appearance_image: bool = False
    opening_image: bool = False
    audio_mode: Literal["generated", "reference", "preserve"] = "generated"

    @field_validator("fps", "steps", "audio", mode="before")
    @classmethod
    def primitive_literals(cls, value, info):
        if type(value) is not (bool if info.field_name == "audio" else int):
            raise ValueError("invalid primitive type")
        return value

    @field_validator("cfg", mode="before")
    @classmethod
    def numeric_cfg(cls, value):
        if type(value) not in (int, float):
            raise ValueError("CFG must be numeric, not boolean")
        return value

    @model_validator(mode="after")
    def validate_roles(self):
        if not self.prompt.strip() or len(self.prompt.encode()) > 8000:
            raise ValueError("invalid prompt")
        if self.frames % 17 != 5:
            raise ValueError("frames must be exact 17k+5; no silent rounding")
        if self.seconds is not None and not math.isclose(
            self.seconds,
            self.frames / 24,
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise ValueError("seconds disagree with exact frames")
        if self.audio_mode != "generated" and not (self.appearance_image or self.opening_image):
            raise ValueError("audio candidates require an explicit image")
        available = set()
        if self.appearance_image:
            available.add(("Picture", "1"))
        if self.audio_mode == "reference":
            available.add(("Audio", "1"))
        if any(label not in available for label in re.findall(r"<(Picture|Audio|Video)\s*(\d+)>", self.prompt)):
            raise ValueError("prompt refers to an absent reference slot")
        return self


def normalize(contract: FastH3Contract, values: dict) -> dict:
    """Return exact candidate units, never a price, live capability or permission."""
    try:
        contract = FastH3Contract.model_validate(contract.model_dump(by_alias=True))
        inputs = FastH3Inputs.model_validate(values)
        if not contract.min_frames <= inputs.frames <= contract.max_frames or f"{inputs.width}x{inputs.height}" not in contract.canvases:
            raise ValueError("outside candidate matrix")
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid FastH3 qualification inputs") from None
    duration = Fraction(inputs.frames, 24)
    return {
        **inputs.model_dump(exclude_none=True),
        "seconds": float(duration),
        "duration_numerator": duration.numerator,
        "duration_denominator": duration.denominator,
        "output_audio_sample_rate": 48_000 if inputs.audio_mode == "preserve" else 32_000,
        "output_audio_channels": 2,
    }


def quote_units(frames: int, micro_usd_per_second: int) -> int:
    """Offline integer ceiling for a supplied server rate; no tariff is approved here."""
    if (
        type(frames) is not int
        or not 124 <= frames <= 362
        or frames % 17 != 5
        or type(micro_usd_per_second) is not int
        or micro_usd_per_second <= 0
    ):
        raise ValueError("invalid FastH3 quote units")
    return (frames * micro_usd_per_second + 23) // 24


def compile_candidate(values: dict) -> dict:
    """Materialize one Core-owned candidate and explicit unbound source slots.

    Source bytes must be independently validated and committed by a future H3
    source adapter before execution. Placeholder filenames are not input assets.
    This is intentionally not called by recipes.resolve or public HTTP routes.
    """
    workflow = json.loads(CANDIDATE.read_text())
    metadata = workflow["_grid"]
    if (
        metadata["modelName"] != MODEL
        or metadata.get("qualificationOnly") is not True
        or metadata.get("engine") != "comfyui"
        or metadata.get("jobType") != "video"
    ):
        raise ValueError("invalid FastH3 qualification metadata")
    contract = FastH3Contract.model_validate(metadata["videoContract"])
    execution = normalize(contract, values)
    graph = copy.deepcopy({key: node for key, node in workflow.items() if key != "_grid"})
    graph["conditioning"]["inputs"].update(
        {
            "prompt": execution["prompt"],
            "width": execution["width"],
            "height": execution["height"],
            "length": execution["frames"],
        },
    )
    graph["noise"]["inputs"]["noise_seed"] = execution["seed"]
    bindings = []
    if execution["appearance_image"]:
        graph["appearance"] = {"class_type": "LoadImage", "inputs": {"image": "grid_appearance.png"}}
        graph["conditioning"]["inputs"]["ref_images.ref_image_0"] = ["appearance", 0]
        bindings.append({"role": "appearance_image", "path": "appearance.inputs.image"})
    if execution["audio_mode"] == "reference":
        graph["reference_sound"] = {"class_type": "LoadAudio", "inputs": {"audio": "grid_reference.wav"}}
        graph["conditioning"]["inputs"]["ref_audios.ref_audio_0"] = ["reference_sound", 0]
        bindings.append({"role": "conditioning_audio", "path": "reference_sound.inputs.audio"})
    if execution["opening_image"] or execution["audio_mode"] == "preserve":
        guide = {"positive": ["conditioning", 0], "latent": ["conditioning", 1], "frame_idx": 0}
        if execution["opening_image"]:
            graph["opening"] = {"class_type": "LoadImage", "inputs": {"image": "grid_opening.png"}}
            guide.update(image=["opening", 0], vae=["video_vae", 0])
            bindings.append({"role": "opening_image", "path": "opening.inputs.image"})
        if execution["audio_mode"] == "preserve":
            graph["soundtrack"] = {"class_type": "LoadAudio", "inputs": {"audio": "grid_soundtrack.wav"}}
            guide.update(audio=["soundtrack", 0], audio_vae=["audio_vae", 0])
            graph["video"]["inputs"]["audio"] = ["soundtrack", 0]
            del graph["decode_audio"]
            bindings.append({"role": "soundtrack", "path": "soundtrack.inputs.audio"})
        graph["guide"] = {"class_type": "MiniMaxH3AddGuide", "inputs": guide}
        graph["guider"]["inputs"]["conditioning"] = ["guide", 0]
    return {"model": MODEL, "execution": execution, "spec": graph, "source_bindings": bindings, "qualification_only": True}

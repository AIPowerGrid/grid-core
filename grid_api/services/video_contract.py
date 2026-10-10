# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded LTX-2.5 execution units shared by recipes, HTTP and billing."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LtxVideoContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract_schema: Literal["aipg.ltx-video.v1"] = Field(alias="schema")
    variant: Literal["distilled", "dev"]
    max_frames: int = Field(ge=9, le=297)
    max_pixels: int = Field(ge=256 * 256, le=960 * 544)

    @model_validator(mode="after")
    def validate_capacity(self):
        if self.max_frames % 8 != 1:
            raise ValueError("max_frames must be 8n+1")
        if self.variant == "dev" and self.max_frames > 81:
            raise ValueError("dev capacity above 81 frames is not qualified")
        return self


class LtxVideoInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    prompt: str = Field(default="", max_length=8000)
    negative_prompt: str = Field(default="", max_length=8000)
    seed: int | None = Field(default=None, ge=0, le=2**53 - 1)
    image: str | None = None  # Worker-bound upload filename, never fetched here.
    width: int = Field(default=960, ge=256, le=960)
    height: int = Field(default=544, ge=256, le=960)
    frames: int | None = Field(default=None, ge=9, le=297)
    seconds: float | None = Field(default=None, gt=0, le=297 / 24)
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
    return {**inputs.model_dump(exclude_none=True), "frames": frames, "seconds": frames / inputs.fps, "steps": steps, "cfg": cfg}


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

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""POST /v1/audio/generations - governed local ACE-Step music generation."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..auth import extract_api_key
from ..config import get_settings
from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..services import audio, media, quota
from .worker_ws import get_available_models

logger = logging.getLogger("grid_api.audio")
router = APIRouter()


class AudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=2000)
    lyrics: str = Field(default="", max_length=20000)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    seconds: float = Field(
        default=30.0,
        ge=audio.MIN_AUDIO_SECONDS,
        le=audio.MAX_AUDIO_SECONDS,
    )
    inference_steps: int = Field(
        default=8,
        ge=audio.MIN_INFERENCE_STEPS,
        le=audio.MAX_INFERENCE_STEPS,
    )
    seed: int | None = Field(default=None, ge=0, le=media.MAX_SEED)
    worker: str | None = None
    progress_token: str | None = Field(default=None, max_length=128)


@router.post("/v1/audio/generations")
@limiter.limit("4/minute")
async def create_audio(
    request: Request,
    body: AudioRequest,
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
    x_grid_user_assertion: str | None = Header(None),
    x_grid_user_token: str | None = Header(None),
):
    """Generate one WAV using a Core-governed local ACE-Step recipe."""
    try:
        if not get_settings().audio_enabled:
            raise HTTPException(status_code=503, detail="Audio generation is not enabled.")
        key = extract_api_key(apikey, authorization)
        user = await accounts_svc.authenticate(
            key,
            x_grid_user_assertion,
            user_token=x_grid_user_token,
            required_scope="inference.submit",
        )
        if body.worker:
            await accounts_svc.assert_owns_worker(user, body.worker)

        model = body.model or audio.DEFAULT_AUDIO_MODEL
        available = await get_available_models(job_type="audio")
        if not available:
            raise HTTPException(status_code=503, detail="No audio workers are online.")
        if model not in available:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{model}' is not available. Online audio models: {available}",
            )
        await quota.check_and_consume(dict(user))
        seed = media.normalize_seed(body.seed)
        payload = {
            "prompt": body.prompt,
            "lyrics": body.lyrics,
            "seconds": body.seconds,
            "inference_steps": body.inference_steps,
            "n": 1,
            "ext": "wav",
            "seed": seed,
            "seeds": [seed],
            "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
        }
        outputs, meta = await media.submit_and_wait(
            model,
            "audio",
            payload,
            audio.AUDIO_TIMEOUT,
            account_id=user.get("account_id"),
            concurrency_limit=media.MEDIA_CONCURRENCY,
            preferred_worker=body.worker or "",
            progress_token=body.progress_token or "",
            billing_user=user,
        )
        output = outputs[0]
        return {
            "created": int(time.time()),
            "data": [{"url": output["url"], "seed": output.get("seed", seed)}],
            "grid": {**meta, "recipe_root": audio.ACE_STEP_RECIPE_ROOT},
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Audio generation failed")
        raise HTTPException(status_code=500, detail="Internal error while processing the request.")

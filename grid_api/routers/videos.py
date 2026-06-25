# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""/v1/videos/generations — text-to-video generation.

Mirrors the image endpoint (there is no OpenAI standard here, so we keep the
same shape for consistency). Jobs go onto the media Redis Stream with
job_type=video and are served by media workers that support temporal models
(e.g. LTX). den scales per frame-step on the worker side.
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..auth import extract_api_key
from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..services import loras as loras_svc
from ..services import media, quota, recipes
from ..services import styles as styles_svc
from .worker_ws import get_available_models

logger = logging.getLogger("grid_api.videos")

router = APIRouter()


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str
    model: Optional[str] = None
    n: int = Field(default=1, ge=1, le=2)
    size: str = "768x512"
    seconds: float = Field(default=4.0, ge=1, le=10)
    fps: int = Field(default=24, ge=8, le=30)
    image: Optional[str] = None  # img2video start frame: inline base64 / data: URI
    loras: Optional[list] = None  # gated via loras service (rejected unless model supports them)
    response_format: Optional[str] = "url"  # "url" | "b64_json"
    worker: Optional[str] = None  # soft-affinity: prefer this worker (must be owned by the account)
    style: Optional[str] = None  # curated style id (GET /v1/styles?job_type=video); expanded server-side
    progress_token: Optional[str] = None  # client id to poll live % at GET /v1/progress/{token}


@router.post("/v1/videos/generations")
@limiter.limit("4/minute")
async def create_video(
    request: Request,
    body: VideoRequest,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Text-to-video generation (grid-native, OpenAI-style envelope)."""
    try:
        key = extract_api_key(apikey, authorization)
        user = await accounts_svc.authenticate(key)

        if body.worker:
            await accounts_svc.assert_owns_worker(user, body.worker)

        extra = body.model_dump(exclude={"prompt", "model", "n", "size", "seconds", "fps", "image",
                                         "loras", "response_format", "worker", "style", "progress_token"})
        prompt = body.prompt
        style_model = None
        style_loras: list = []
        if body.style:
            try:
                eff = styles_svc.apply_style(body.style, prompt=body.prompt, user_params=extra,
                                             user_negative=str(extra.get("negative_prompt") or ""))
            except KeyError:
                raise HTTPException(status_code=404, detail=f"Unknown style '{body.style}'.")
            prompt = eff["prompt"]
            if eff.get("negative_prompt"):
                extra["negative_prompt"] = eff["negative_prompt"]
            extra.update(eff["params"])
            style_model = eff.get("model") or None
            style_loras = eff.get("loras") or []

        model = body.model or style_model or media.DEFAULT_VIDEO_MODEL

        available = await get_available_models(job_type="video")
        if not available:
            raise HTTPException(status_code=503, detail="No video workers are online.")
        if model not in available:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{model}' is not available. Online video models: {available}",
            )

        # strength/denoise: reject for models with no latent-blend recipe (e.g. LTX
        # i2v) rather than silently dropping it.
        if (extra.get("strength") is not None or extra.get("denoise") is not None) \
                and not recipes.supports_denoise(model):
            raise HTTPException(
                status_code=422,
                detail=f"model '{model}' does not support strength/denoise",
            )

        await quota.check_and_consume(dict(user))

        set_fields = body.model_fields_set
        width, height = media.parse_size(body.size, default=(768, 512), strict=("size" in set_fields))
        frames, effective_seconds = media.normalize_video_timing(body.seconds, body.fps)
        steps, cfg_scale, sampler = media.diffusion_params(model, extra)
        seed = media.normalize_seed(extra.get("seed"))
        seeds = media.seeds_for_outputs(seed, body.n)

        # Recipe knobs: pass effective dimensions/timing every time so the recipe,
        # legacy payload, ledger reward, and billing all agree on what ran.
        # Out-of-range values are rejected (422) by the resolver against the
        # model's allowed band.
        recipe_inputs: dict = {}

        # img2video: decode the inline start frame, AUTO-MATCH output size to it
        # (unless the caller pinned `size`), upload it, and let the worker load it.
        source_image_url = None
        if body.image:
            dims, source_image_url = await media.prepare_source_image(
                model, body.image, size_was_set=("size" in set_fields))
            recipe_inputs.update(dims)
            if dims:
                width, height = int(dims["width"]), int(dims["height"])

        recipe_inputs["width"], recipe_inputs["height"] = width, height
        recipe_inputs["seconds"] = effective_seconds
        recipe_inputs["fps"] = body.fps
        recipe_inputs["frames"] = frames
        recipe_inputs.update(media.advanced_knob_inputs(extra))  # steps/cfg/sampler/scheduler

        # LoRAs: gate consistently with /v1/images (was silently ignored on video — a
        # model with no img/lora recipe now returns 422 instead of dropping them).
        loras = loras_svc.prepare_loras(model, (body.loras or []) + style_loras)

        payload = {
            "prompt": prompt,
            "n": body.n,
            "width": width,
            "height": height,
            "frames": frames,
            "fps": body.fps,
            "length": frames,
            "video_length": frames,
            "steps": steps,
            "sampler_name": sampler,
            "cfg_scale": cfg_scale,
            "ext": "mp4",
            "seed": seed,
            "seeds": seeds,
        }
        for k in ("negative_prompt",):
            if extra.get(k) is not None:
                payload[k] = extra[k]
        if recipe_inputs:
            payload["recipe_inputs"] = recipe_inputs
        if source_image_url:
            payload["source_image_url"] = source_image_url
        if loras:
            payload["loras"] = loras

        outputs, meta = await media.submit_and_wait(model, "video", payload, media.VIDEO_TIMEOUT,
                                              account_id=user.get("account_id"), concurrency_limit=media.MEDIA_CONCURRENCY,
                                              preferred_worker=body.worker or "", progress_token=body.progress_token or "")

        want_b64 = body.response_format == "b64_json"
        data = []
        for i, o in enumerate(outputs):
            item = {}
            if want_b64:
                item["b64_json"] = await media.url_to_b64(o["url"])
            else:
                item["url"] = o["url"]
            item["seed"] = o.get("seed") if o.get("seed") is not None else seeds[min(i, len(seeds) - 1)]
            data.append(item)

        return {"created": int(time.time()), "data": data, "grid": meta}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Video generation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error while processing the request.")

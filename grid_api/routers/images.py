# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI-compatible /v1/images/generations — native v2 media dispatch.

Jobs go onto the media Redis Stream and are served by WS-connected media
workers (same machinery as text/video). The worker uploads results directly to
R2 via presigned URLs; the completion arrives on the job's pub/sub channel.

Smart defaults for steps/cfg/sampler are derived from the model, but advanced
callers can override them (and pass seed / negative_prompt) — the schema allows
extra fields so power users get raw control while staying OpenAI-shaped.
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

logger = logging.getLogger("grid_api.images")

router = APIRouter()


class ImageRequest(BaseModel):
    # Allow advanced passthrough params (seed, negative_prompt, steps, cfg_scale,
    # sampler) without dropping them — power users get raw control.
    model_config = ConfigDict(extra="allow")

    prompt: str
    model: Optional[str] = None
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    quality: Optional[str] = "standard"
    image: Optional[str] = None  # img2img source: inline base64 / data: URI
    loras: Optional[list] = None  # [{name, model, clip, is_version, inject_trigger}], CivitAI
    output_format: Optional[str] = None  # png | jpeg | webp (default webp); OpenAI-style
    response_format: Optional[str] = "url"  # "url" | "b64_json"
    worker: Optional[str] = None  # soft-affinity: prefer this worker (must be owned by the account)
    style: Optional[str] = None  # curated style id (GET /v1/styles); expanded server-side
    progress_token: Optional[str] = None  # client id to poll live % at GET /v1/progress/{token}


@router.post("/v1/images/generations")
@limiter.limit("10/minute")
async def create_image(
    request: Request,
    body: ImageRequest,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """OpenAI-compatible image generation."""
    try:
        key = extract_api_key(apikey, authorization)
        user = await accounts_svc.authenticate(key)

        # Worker affinity is ownership-gated: reject before doing any work if the
        # caller named a worker they don't own.
        if body.worker:
            await accounts_svc.assert_owns_worker(user, body.worker)

        # Advanced passthrough knobs (steps/cfg_scale/sampler/scheduler/seed/negative_prompt).
        extra = body.model_dump(exclude={"prompt", "model", "n", "size", "quality", "image",
                                         "loras", "output_format", "response_format", "worker", "style", "progress_token"})
        prompt = body.prompt
        style_model = None
        style_loras: list = []
        if body.style:
            # A style composes over the recipe: expand the prompt template, apply
            # curated params (locked ones override the user — e.g. distilled `steps`),
            # and attach its LoRAs. The recipe resolver still hard-gates everything.
            try:
                eff = styles_svc.apply_style(body.style, prompt=body.prompt, user_params=extra,
                                             user_negative=str(extra.get("negative_prompt") or ""))
            except KeyError:
                raise HTTPException(status_code=404, detail=f"Unknown style '{body.style}'.")
            prompt = eff["prompt"]
            if eff.get("negative_prompt"):
                extra["negative_prompt"] = eff["negative_prompt"]
            extra.update(eff["params"])          # locked params already enforced in apply_style
            style_model = eff.get("model") or None
            style_loras = eff.get("loras") or []

        model = body.model or style_model or media.DEFAULT_IMAGE_MODEL

        # Fast availability check — fail in ms instead of waiting out the job
        # timeout when no worker serves the model.
        available = await get_available_models(job_type="image")
        if not available:
            raise HTTPException(status_code=503, detail="No image workers are online.")
        if model not in available:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{model}' is not available. Online image models: {available}",
            )

        # strength/denoise is a latent-blend img2img knob. Reject up-front for models
        # whose recipe has no `denoise` slot (e.g. FLUX.2 reference-edit) so it's never
        # silently dropped — clients learn the model doesn't support it.
        if (extra.get("strength") is not None or extra.get("denoise") is not None) \
                and not recipes.supports_denoise(model):
            raise HTTPException(
                status_code=422,
                detail=f"model '{model}' does not support strength/denoise "
                       "(no latent-blend img2img recipe; it edits via reference conditioning)",
            )

        # Validate cheap stuff BEFORE consuming quota (reject without charging).
        out_ext = media.normalize_image_format(body.output_format)
        loras = loras_svc.prepare_loras(model, (body.loras or []) + style_loras)

        await quota.check_and_consume(dict(user))

        set_fields = body.model_fields_set
        width, height = media.parse_size(body.size, strict=("size" in set_fields))
        steps, cfg_scale, sampler = media.diffusion_params(model, extra)
        seed = media.normalize_seed(extra.get("seed"))
        seeds = media.seeds_for_outputs(seed, body.n)

        # Recipe knobs (for recipe-governed image models): pass the effective
        # dimensions every time so recipe dispatch, den, billing, and response
        # metadata all describe the same job. img2img source frames auto-match
        # unless the caller pinned `size`.
        recipe_inputs: dict = {}
        source_image_url = None
        if body.image:
            dims, source_image_url = await media.prepare_source_image(
                model, body.image, size_was_set=("size" in set_fields))
            recipe_inputs.update(dims)
            if dims:
                width, height = int(dims["width"]), int(dims["height"])
        recipe_inputs["width"], recipe_inputs["height"] = width, height
        recipe_inputs.update(media.advanced_knob_inputs(extra))  # shared w/ videos (no drift)

        payload = {
            "prompt": prompt,
            "n": body.n,
            "width": width,
            "height": height,
            "steps": steps,
            "sampler_name": sampler,
            "cfg_scale": cfg_scale,
            "ext": out_ext,
            "seed": seed,
            "seeds": seeds,
        }
        # Pass advanced knobs through to the worker when provided.
        for k in ("negative_prompt",):
            if extra.get(k) is not None:
                payload[k] = extra[k]
        if recipe_inputs:
            payload["recipe_inputs"] = recipe_inputs
        if source_image_url:
            payload["source_image_url"] = source_image_url
        if loras:
            payload["loras"] = loras

        outputs, meta = await media.submit_and_wait(model, "image", payload, media.IMAGE_TIMEOUT,
                                              account_id=user.get("account_id"), concurrency_limit=media.MEDIA_CONCURRENCY,
                                              preferred_worker=body.worker or "", progress_token=body.progress_token or "")

        want_b64 = body.response_format == "b64_json"
        data = []
        for i, o in enumerate(outputs):
            item = {"revised_prompt": prompt}
            if want_b64:
                item["b64_json"] = await media.url_to_b64(o["url"])
            else:
                item["url"] = o["url"]
            item["seed"] = o.get("seed") if o.get("seed") is not None else seeds[min(i, len(seeds) - 1)]
            data.append(item)

        # `grid` carries who ran the job + how long, so a UI can show per-image
        # provenance (worker, gen time, model) without a second lookup.
        return {"created": int(time.time()), "data": data, "grid": meta}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image generation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error while processing the request.")

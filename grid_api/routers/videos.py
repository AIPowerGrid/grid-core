# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""/v1/videos/generations — text-to-video generation.

Mirrors the image endpoint (there is no OpenAI standard here, so we keep the
same shape for consistency). Jobs go onto the media Redis Stream with
job_type=video and are served by media workers that support temporal models
(e.g. LTX). den scales per frame-step on the worker side.
"""

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..auth import extract_api_key
from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..services import loras as loras_svc
from ..services import generation_admission, media, quota, recipes, storage, video_contract, video_sources
from ..services.video_plans import VideoInputs, stage_inputs
from ..services import styles as styles_svc
from .worker_ws import get_available_models
from ._video_body import BoundedVideoRoute

logger = logging.getLogger("grid_api.videos")

router = APIRouter(route_class=BoundedVideoRoute)


class VideoKeyframe(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    image: str = Field(min_length=1, max_length=17_000_000)
    frame: int = Field(ge=0, le=336)
    strength: float = Field(default=1.0, ge=0, le=1)


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str
    model: Optional[str] = None
    n: int = Field(default=1, ge=1, le=2)
    size: str = "768x512"
    seconds: float | None = Field(default=None, gt=0, le=337 / 24, allow_inf_nan=False)
    frames: int | None = Field(default=None, ge=9, le=337, strict=True)
    fps: int = Field(default=24, ge=8, le=30, strict=True)
    image: Optional[str] = None  # img2video start frame: inline base64 / data: URI
    reference_audio: str | None = Field(default=None, min_length=1, max_length=16_777_280)
    reference_video: str | None = Field(default=None, min_length=1, max_length=44_739_308)
    keyframes: list[VideoKeyframe] = Field(default_factory=list, max_length=16)
    loras: Optional[list] = None  # gated via loras service (rejected unless model supports them)
    response_format: Optional[str] = "url"  # "url" | "b64_json"
    worker: Optional[str] = None  # soft-affinity: prefer this worker (must be owned by the account)
    style: Optional[str] = None  # curated style id (GET /v1/styles?job_type=video); expanded server-side
    progress_token: Optional[str] = None  # client id to poll live % at GET /v1/progress/{token}


async def validate_video(body: VideoRequest, user: dict, *, async_submission: bool = False) -> VideoInputs:
    """Validate bounded inputs and admission without quota, uploads or dispatch."""
    try:
        if body.worker:
            await accounts_svc.assert_owns_worker(user, body.worker)

        extra = body.model_dump(exclude={"prompt", "model", "n", "size", "seconds", "fps", "image",
                                         "frames", "keyframes", "reference_audio", "reference_video", "loras", "response_format", "worker", "style", "progress_token"})
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
        if body.image and body.keyframes:
            raise HTTPException(status_code=422, detail="Use image or keyframes, not both")
        source_image = body.image
        if body.keyframes:
            source_image = body.keyframes[0].image
        contract = recipes.video_contract_for_model(model, has_source=bool(source_image))
        source_mode = "video" if body.reference_video else "audio" if body.reference_audio else "generated"
        if contract is None and (body.frames is not None or body.keyframes or source_mode != "generated"):
            raise HTTPException(status_code=422, detail="This model has no typed frame/keyframe contract")
        if source_mode != "generated" and source_mode not in contract.source_modes:
            raise HTTPException(422, detail="This recipe does not accept the requested audio/video reference mode")
        if body.reference_video and (not body.reference_audio or "size" not in body.model_fields_set):
            raise HTTPException(422, detail="Video reference requires reference_audio and an explicit output size")

        feature = "recipe-image-bindings-v1" if len(body.keyframes) > 1 else None
        source_features = ("recipe-media-sources-v1",) if source_mode != "generated" else ()
        if async_submission:
            features = ("async-video-resume-v1",) + source_features + ((feature,) if feature else ())
            available = await get_available_models(job_type="video", media_features=features)
            if contract is None:
                raise HTTPException(422, detail="Async video requires a governed typed video recipe")
        elif source_features:
            features = source_features + ((feature,) if feature else ())
            available = await get_available_models(job_type="video", media_features=features)
        else:
            available = await get_available_models(job_type="video", **({"media_feature": feature} if feature else {}))
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

        media.validate_batch_request("video", body.n, has_source=bool(source_image))

        set_fields = body.model_fields_set
        if contract:
            if body.loras or style_loras:
                raise HTTPException(status_code=422, detail="LTX-2.5 LoRAs are not qualified")
            if body.response_format not in ("url", "b64_json"):
                raise HTTPException(status_code=422, detail="response_format must be url or b64_json")
            unsupported = set(extra) - {"negative_prompt", "seed", "steps", "cfg_scale", "sampler", "audio", "guide_strength"}
            if unsupported:
                raise HTTPException(status_code=422, detail="Unsupported LTX-2.5 parameter; consult the model parameter contract")
            values = {"prompt": prompt, "fps": body.fps, **media.advanced_knob_inputs(extra)}
            if source_mode != "generated":
                values["source_mode"] = source_mode
            if extra.get("guide_strength") is not None:
                values["guide_strength"] = extra["guide_strength"]
            if body.keyframes:
                values["keyframes"] = [{"frame": pin.frame, "strength": pin.strength} for pin in body.keyframes]
            for key in ("negative_prompt", "seed", "audio"):
                if key in extra:
                    values[key] = extra[key]
            try:
                if "size" in set_fields:
                    values["width"], values["height"] = video_contract.parse_size(body.size)
                if body.seconds is not None:
                    values["seconds"] = body.seconds
                if body.frames is not None:
                    values["frames"] = body.frames
                values = video_contract.normalize(contract, values)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid LTX-2.5 inputs; consult the model parameter contract") from None
            width, height, frames = values["width"], values["height"], values["frames"]
            effective_seconds = values["seconds"]
            steps, cfg_scale, sampler = values["steps"], values["cfg"], values["sampler"]
        else:
            seconds = 4.0 if body.seconds is None else body.seconds
            if not 1 <= seconds <= 10:
                raise HTTPException(status_code=422, detail="seconds must be between 1 and 10 for this model")
            width, height = media.parse_size(body.size, default=(768, 512), strict=("size" in set_fields))
            frames, effective_seconds = media.normalize_video_timing(seconds, body.fps)
            steps, cfg_scale, sampler = media.diffusion_params(model, extra)
        seed = media.normalize_seed(extra.get("seed"))
        seeds = media.seeds_for_outputs(seed, body.n)

        # Validate every inline pin before quota or any storage side effect.
        decoded_pins = media.decode_video_keyframes([pin.image for pin in body.keyframes]) if body.keyframes else None
        if decoded_pins and "size" not in set_fields:
            width, height = video_contract.source_size(contract, *decoded_pins[0][2:])
        if source_image and not decoded_pins:
            if not recipes.supports_image(model):
                raise HTTPException(422, detail=f"model '{model}' does not accept an input image (no img2img/img2video recipe)")
            decoded_pins = [media.decode_source_image(source_image)]
            if "size" not in set_fields:
                sw, sh = decoded_pins[0][2:]
                width, height = video_contract.source_size(contract, sw, sh) if contract else media.smart_size(sw, sh)
        # Source-bearing paths are default dark, independently of charging.
        # Check before decoding MP4s, uploads or consuming caller quota.
        generation_admission.require_media("video", {
            "source_image_url": bool(source_image),
            "source_media": ([{"role": "reference_audio"}] + ([{"role": "reference_video"}] if body.reference_video else []))
                            if source_mode != "generated" else [],
            "recipe_inputs": {"keyframes": values.get("keyframes", []) if contract else [],
                              "timeline": extra.get("timeline")},
        })
        if sum(len(value) for value in ([pin.image for pin in body.keyframes] +
                                       [body.image, body.reference_audio, body.reference_video]) if value) > video_sources.MAX_ENCODED_BYTES:
            raise HTTPException(413, detail="Combined video inputs exceed the encoded limit")
        source_media = await video_sources.validate_sources(body.reference_audio, body.reference_video,
                                                           frames=frames, width=width, height=height)
        if contract and (source_image or source_media):
            storage.private_source_bucket()

        # Recipe knobs: pass effective dimensions/timing every time so the recipe,
        # legacy payload, ledger reward, and billing all agree on what ran.
        # Out-of-range values are rejected (422) by the resolver against the
        # model's allowed band.
        recipe_inputs: dict = {}

        recipe_inputs["width"], recipe_inputs["height"] = width, height
        recipe_inputs["seconds"] = effective_seconds
        recipe_inputs["fps"] = body.fps
        recipe_inputs["frames"] = frames
        recipe_inputs.update(media.advanced_knob_inputs(extra))  # steps/cfg/sampler/scheduler
        if contract:
            recipe_inputs.update(steps=steps, cfg=cfg_scale, sampler=sampler, audio=True)
            if source_mode != "generated":
                recipe_inputs["source_mode"] = source_mode
                if "guide_strength" in values:
                    recipe_inputs["guide_strength"] = values["guide_strength"]
            if body.keyframes:
                recipe_inputs["keyframes"] = values["keyframes"]
        # Director/timeline recipes (e.g. LTX Director 2.0): forward the recipe's
        # string knobs. The resolver applies them ONLY if the recipe declares the
        # var (unknown inputs are ignored), so plain video recipes are unaffected.
        # `timeline` may carry inline base64 media (imageB64/audioB64) per the
        # LTXDirector schema — generous cap; the others stay tight.
        for k, cap in (("timeline", 25_000_000), ("local_prompts", 100_000),
                       ("segment_lengths", 100_000), ("guide_strength", 100_000)):
            if contract:
                continue
            v = extra.get(k)
            if v is not None:
                recipe_inputs[k] = str(v)[:cap]

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
        if loras:
            payload["loras"] = loras

        if async_submission or source_features:
            payload["required_media_features"] = list(features)
        return VideoInputs(model, json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=True, allow_nan=False),
                           tuple(seeds), tuple(decoded_pins or []), tuple(source_media),
                           bool(body.keyframes), bool(contract))
    except HTTPException:
        raise
    except Exception:
        logger.error("Video preparation failed")
        raise HTTPException(status_code=503, detail="Video preparation temporarily unavailable") from None


async def prepare_video(body: VideoRequest, user: dict, *, async_submission: bool = False) -> tuple[str, dict, list[int]]:
    """Legacy waiting ingress: validate first, then consume quota and upload."""
    inputs = await validate_video(body, user, async_submission=async_submission)
    try:
        await quota.check_and_consume(dict(user))
        return inputs.model, await stage_inputs(inputs), list(inputs.seeds)
    except HTTPException:
        raise
    except Exception:
        logger.error("Video preparation failed")
        raise HTTPException(status_code=503, detail="Video preparation temporarily unavailable") from None


@router.post("/v1/videos/generations")
@limiter.limit("4/minute")
async def create_video(
    request: Request,
    body: VideoRequest,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    x_grid_user_assertion: Optional[str] = Header(None),
    x_grid_user_token: Optional[str] = Header(None),
):
    """Text-to-video generation (grid-native, OpenAI-style envelope)."""
    try:
        user = await accounts_svc.authenticate(
            extract_api_key(apikey, authorization), x_grid_user_assertion,
            user_token=x_grid_user_token, required_scope="inference.submit",
        )
        model, payload, seeds = await prepare_video(body, user)

        outputs, meta = await media.submit_and_wait(model, "video", payload, media.VIDEO_TIMEOUT,
                                              account_id=user.get("account_id"), concurrency_limit=media.MEDIA_CONCURRENCY,
                                              preferred_worker=body.worker or "", progress_token=body.progress_token or "",
                                              billing_user=user)

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

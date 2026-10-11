# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared media (image / video / audio / 3D) generation helpers.

One code path behind three front doors: `/v1/images/generations`,
`/v1/videos/generations`, and the chat-completions abstraction. Each submits a
media job to the Redis media stream (served by ComfyUI/media workers that upload
to R2 and report back on the job channel) and waits for the result. den +
ledger are recorded by the worker WS handler, so callers never meter directly.
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
import secrets
from typing import Optional
from uuid import uuid4

import httpx
from fastapi import HTTPException
from PIL import Image

from . import job_queue, token_stream, recipes, storage, credits
from . import generation_admission, video_contract

logger = logging.getLogger("grid_api.media")

DEFAULT_IMAGE_MODEL = "FLUX.2 Klein 4B FP8"
DEFAULT_VIDEO_MODEL = "LTX-2.3"
DEFAULT_3D_MODEL = "TRELLIS2"

# Output dimension clamps — a client must not be able to push an arbitrary size
# that ties up a GPU (or OOMs the worker).
MIN_DIM, MAX_DIM = 256, 1536
MAX_FRAMES = 240  # ~10s @ 24fps ceiling
MAX_SEED = 2**53 - 1
MAX_STEPS = int(os.getenv("MEDIA_MAX_STEPS", "80"))
MAX_CFG_SCALE = float(os.getenv("MEDIA_MAX_CFG_SCALE", "30"))
_SAMPLER_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,64}$")

IMAGE_TIMEOUT = 300
VIDEO_TIMEOUT = 600
THREED_TIMEOUT = 900   # TRELLIS image->3D is slow (model load + sparse ops)

# Billing fallback: when a video request omits `seconds`, the recipe's baked
# default duration isn't known to the grid — bill this rather than free.
DEFAULT_VIDEO_SECONDS = 5.0

# Per-account cap on concurrent in-flight MEDIA jobs. Media is heavy + long (300–600s),
# so one key holding many would starve the GPU pool. Kept tighter than the general
# per-account `concurrency` allowance. Override via env.
MEDIA_CONCURRENCY = int(os.getenv("MEDIA_CONCURRENCY", "4"))

# Source-image (img2img / img2video) input limits.
MAX_SOURCE_BYTES = 12 * 1024 * 1024
_SOURCE_EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


def decode_source_image(value: str) -> tuple[bytes, str, int, int]:
    """Decode an INLINE base64 / data-URI source image → (bytes, ext, width, height).

    No URL fetch (SSRF-safe): callers embed the image. Raises HTTPException(400) on
    anything that isn't a small, decodable image."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="source image must be base64 or a data: URI")
    s = value.strip()
    if s.startswith("data:"):
        if "," not in s:
            raise HTTPException(status_code=400, detail="malformed data: URI")
        s = s.split(",", 1)[1]
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="source image is not valid base64")
    if not raw or len(raw) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=400, detail=f"source image must be 1B–{MAX_SOURCE_BYTES} bytes")
    try:
        Image.open(io.BytesIO(raw)).verify()           # integrity
        im = Image.open(io.BytesIO(raw))               # re-open (verify exhausts it)
        w, h = im.size
        fmt = (im.format or "").upper()
    except Exception:
        raise HTTPException(status_code=400, detail="source image is not a decodable image")
    return raw, _SOURCE_EXT.get(fmt, "png"), int(w), int(h)


IMAGE_FORMATS = {"png": "png", "webp": "webp", "jpg": "jpg", "jpeg": "jpg"}


def normalize_image_format(fmt: Optional[str], default: str = "webp") -> str:
    """Validate a requested output image format → storage ext (jpeg→jpg). Reject
    anything unsupported so the caller learns instead of silently getting webp."""
    if fmt is None:
        return default
    ext = IMAGE_FORMATS.get(str(fmt).lower())
    if ext is None:
        raise HTTPException(status_code=422,
                            detail=f"output_format must be one of: png, jpeg, webp")
    return ext


def clamp_dim(v: int) -> int:
    """Clamp a dimension and snap to a multiple of 64 (diffusion-friendly)."""
    v = max(MIN_DIM, min(MAX_DIM, int(v)))
    return (v // 64) * 64 or MIN_DIM


def smart_size(src_w: int, src_h: int, *, dim_min: int = 512, dim_max: int = 1280,
               multiple: int = 32, max_pixels: int = 1280 * 736) -> tuple[int, int]:
    """Pick output dims that MATCH a source image's aspect — the OpenAI/happy-path
    default for img2img / img2video, so output mirrors the input with no letterbox
    and no manual `size`.

    The matched box is fit under `max_pixels`, snapped to the model's grid
    (`multiple`), and clamped to its band (`dim_min`..`dim_max`). Band + multiple
    are meant to come from the model's constraints (same source as the gating band,
    `recipes._range_for`); defaults here suit LTX-2.3.
    """
    if src_w <= 0 or src_h <= 0:
        return parse_size("", default=(dim_max, dim_max))
    ar = src_w / src_h
    h = (max_pixels / ar) ** 0.5
    w = h * ar

    def _snap(v: float) -> int:
        return max(dim_min, min(dim_max, int(round(v / multiple)) * multiple))

    return _snap(w), _snap(h)


def advanced_knob_inputs(extra: dict) -> dict:
    """Map request advanced knobs → recipe-var inputs, only for those explicitly set.
    Shared by /v1/images and /v1/videos so the two surfaces can't drift (audit cons §2).
    Request `cfg_scale` maps to the recipe's `cfg` var. The resolver gates/allow-lists
    each value (or ignores it if the recipe doesn't declare the var)."""
    out: dict = {}
    # `strength`/`denoise` both target the recipe's `denoise` var (latent-blend
    # img2img). The router capability-gates them (reject if the model has no denoise
    # recipe) so they're never silently dropped; the resolver range-gates [lo,hi].
    for req_key, var in (("steps", "steps"), ("cfg_scale", "cfg"),
                         ("sampler", "sampler"), ("scheduler", "scheduler"),
                         ("strength", "denoise"), ("denoise", "denoise")):
        if extra.get(req_key) is not None:
            out[var] = extra[req_key]
    return out


def parse_size(size: str, default=(1024, 1024), *, strict: bool = False) -> tuple[int, int]:
    try:
        w, h = (int(x) for x in str(size).lower().split("x"))
    except (ValueError, AttributeError):
        if strict:
            raise HTTPException(status_code=422, detail="size must be formatted as WIDTHxHEIGHT")
        return default
    normalized = clamp_dim(w), clamp_dim(h)
    if strict and normalized != (w, h):
        raise HTTPException(
            status_code=422,
            detail=f"size dimensions must be multiples of 64 between {MIN_DIM} and {MAX_DIM}",
        )
    return normalized


def normalize_seed(value) -> int:
    """Seed contract: omitted/null randomizes; any explicit non-negative int is honored."""
    if value is None or value == "":
        return secrets.randbelow(MAX_SEED + 1)
    try:
        seed = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="seed must be an integer")
    if seed < 0 or seed > MAX_SEED:
        raise HTTPException(status_code=422, detail=f"seed must be between 0 and {MAX_SEED}")
    return seed


def seeds_for_outputs(seed: int, n: int) -> list[int]:
    count = max(int(n or 1), 1)
    return [(int(seed) + i) % (MAX_SEED + 1) for i in range(count)]


def validate_batch_request(job_type: str, n: int, *, has_source: bool) -> None:
    """Reject batch shapes that no approved worker recipe can fulfill yet."""
    if n <= 1:
        return
    if job_type == "video":
        raise HTTPException(
            status_code=422,
            detail="video batch generation is unavailable until the recipe declares a verified batch strategy",
        )
    if has_source:
        raise HTTPException(
            status_code=422,
            detail="source-image batch generation is unavailable until the recipe declares a verified batch strategy",
        )


def normalize_video_timing(seconds: float, fps: int) -> tuple[int, float]:
    try:
        fps_i = int(fps)
        frames = int(round(float(seconds) * fps_i))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="seconds and fps must be numeric")
    if frames < 1:
        raise HTTPException(status_code=422, detail="video must contain at least 1 frame")
    if frames > MAX_FRAMES:
        raise HTTPException(
            status_code=422,
            detail=f"seconds * fps produces {frames} frames; max is {MAX_FRAMES}",
        )
    return frames, frames / fps_i


def _validate_int_knob(name: str, value, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{name} must be an integer")
    if v < lo or v > hi:
        raise HTTPException(status_code=422, detail=f"{name} must be between {lo} and {hi}")
    return v


def _validate_float_knob(name: str, value, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{name} must be a number")
    if v < lo or v > hi:
        raise HTTPException(status_code=422, detail=f"{name} must be between {lo:g} and {hi:g}")
    return v


def _validate_sampler(value) -> str:
    sampler = str(value)
    if not _SAMPLER_RE.fullmatch(sampler):
        raise HTTPException(status_code=422, detail="sampler contains unsupported characters")
    return sampler


def diffusion_params(model: str, overrides: dict) -> tuple[int, float, str]:
    """Pick sensible steps/cfg/sampler from the model, honoring explicit overrides.

    Advanced callers can pass `steps` / `cfg_scale` / `sampler` to override the
    smart defaults; the OpenAI-shaped path just omits them and gets the defaults.
    """
    name = model.lower()
    is_flux = "flux" in name
    is_fast = "klein" in name or "schnell" in name
    default_steps = 4 if is_fast else (20 if is_flux else 30)
    default_cfg = 1.0 if is_fast else (3.5 if is_flux else 7.5)
    default_sampler = "euler" if is_flux else "k_euler"
    steps = overrides.get("steps") if overrides.get("steps") is not None else default_steps
    cfg = overrides.get("cfg_scale") if overrides.get("cfg_scale") is not None else default_cfg
    sampler = overrides.get("sampler") if overrides.get("sampler") is not None else default_sampler
    return (
        _validate_int_knob("steps", steps, 1, MAX_STEPS),
        _validate_float_knob("cfg_scale", cfg, 0, MAX_CFG_SCALE),
        _validate_sampler(sampler),
    )


async def prepare_source_image(model: str, image_value: str, *, size_was_set: bool) -> tuple[dict, str]:
    """Shared img2img / img2video ingest for every OpenAI surface (/v1/images,
    /v1/videos, chat). Honest capability gate, then:
      decode inline source → AUTO-MATCH output dims to it (unless size pinned) →
      upload to R2 → return (dim_inputs, source_image_url).

    `dim_inputs` is {} when the caller pinned an explicit size (they inject their
    own width/height). Rejects (422) if the model has no recipe declaring an `image`
    var, so a source frame is never silently ignored."""
    if not recipes.supports_image(model):
        raise HTTPException(
            status_code=422,
            detail=f"model '{model}' does not accept an input image (no img2img/img2video recipe)",
        )
    raw, ext, sw, sh = decode_source_image(image_value)
    dims: dict = {}
    if not size_was_set:
        contract = recipes.video_contract_for_model(model, has_source=True)
        w, h = video_contract.source_size(contract, sw, sh) if contract else smart_size(sw, sh)
        dims = {"width": w, "height": h}
    url = await asyncio.to_thread(storage.upload_source, raw, ext)
    return dims, url


def decode_video_keyframes(values: list[str]) -> list[tuple[bytes, str, int, int]]:
    if not 1 <= len(values) <= 16 or sum(len(value) for value in values) > 32 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="keyframes exceed the 32 MB encoded input limit")
    decoded = [decode_source_image(value) for value in values]
    if sum(len(item[0]) for item in decoded) > 24 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="keyframes exceed the 24 MB decoded input limit")
    return decoded


async def upload_video_keyframes(decoded: list[tuple[bytes, str, int, int]]) -> list[str]:
    # Reused reference images share an object, but retain distinct timed pins.
    uploaded: dict[bytes, str] = {}
    urls = []
    for raw, ext, _width, _height in decoded:
        if raw not in uploaded:
            uploaded[raw] = await asyncio.to_thread(storage.upload_source, raw, ext)
        urls.append(uploaded[raw])
    return urls


_INFLIGHT_PREFIX = "grid:inflight:"
_INFLIGHT_TTL = 1800  # safety: self-heal a leaked counter (job longer than this is dead anyway)


async def _inflight_acquire(account_id, limit: int) -> bool:
    """Reserve one in-flight slot for the account. Returns False (and reserves nothing)
    if the account is already at its concurrency limit. Backed by a Redis counter so the
    cap holds across all uvicorn workers."""
    from ..redis_client import get_redis
    r = get_redis()
    key = f"{_INFLIGHT_PREFIX}{account_id}"
    cur = await r.incr(key)
    await r.expire(key, _INFLIGHT_TTL)
    if cur > limit:
        await r.decr(key)
        return False
    return True


async def _inflight_release(account_id) -> None:
    from ..redis_client import get_redis
    r = get_redis()
    key = f"{_INFLIGHT_PREFIX}{account_id}"
    # floor at 0 — never let a double-release drive the counter negative
    if await r.decr(key) < 0:
        await r.set(key, 0)


async def prepare_dispatch(model: str, job_type: str, payload: dict) -> tuple[dict, dict | None]:
    """Validate and freeze governed execution before either ingress moves money."""
    sources = payload.get("source_image_urls")
    if sources is not None and (job_type != "video" or not isinstance(sources, list)
                               or not 1 <= len(sources) <= 16
                               or not all(isinstance(url, str) and 0 < len(url) <= 4096 for url in sources)
                               or sources[0] != payload.get("source_image_url")):
        raise HTTPException(status_code=422, detail="Invalid video keyframe source list")
    generation_admission.require_media(job_type, payload)
    n = int(payload.get("n", 1) or 1)
    validate_batch_request(
        job_type,
        n,
        has_source=bool(payload.get("source_image_url")),
    )
    resolved_recipe = None
    contract = recipes.video_contract_for_model(model, has_source=bool(payload.get("source_image_url"))) if job_type == "video" else None
    if contract is None and (payload.get("source_media") or (payload.get("recipe_inputs") or {}).get("source_mode")):
        raise HTTPException(422, detail="Source-conditioned video requires a typed recipe contract")
    if sources is not None and (contract is None or not (payload.get("recipe_inputs") or {}).get("keyframes")):
        raise HTTPException(status_code=422, detail="Video keyframe sources require a typed keyframe contract")
    if contract:
        try:
            resolved_recipe = recipes.resolve_for_model(model, {
                "prompt": payload.get("prompt", ""),
                "negative_prompt": payload.get("negative_prompt", ""),
                "seed": payload.get("seed"),
                **(payload.get("recipe_inputs") or {}),
            }, has_source=bool(payload.get("source_image_url")))
        except recipes.RecipeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if not resolved_recipe or not resolved_recipe.get("execution"):
            raise HTTPException(status_code=503, detail="Qualified video recipe is unavailable")
        bindings = resolved_recipe.get("image_bindings")
        if bindings:
            urls = sources if sources is not None else [payload.get("source_image_url")]
            if len(urls) != len(bindings) or not all(isinstance(url, str) and url for url in urls):
                raise HTTPException(status_code=422, detail="Video keyframe sources do not match the execution contract")
            if len(bindings) > 1:
                from ..routers.worker_ws import get_available_models
                available = await get_available_models(job_type="video", media_feature="recipe-image-bindings-v1")
                if model not in available:
                    raise HTTPException(status_code=503, detail="No compatible video keyframe worker is available")
                payload = {**payload, "required_media_feature": "recipe-image-bindings-v1"}
        source_bindings = resolved_recipe.get("source_bindings") or []
        source_media = payload.get("source_media") or []
        if source_bindings or source_media:
            from .video_sources import validate_commitments
            validate_commitments(source_media, source_bindings, resolved_recipe["execution"])
            features = set(payload.get("required_media_features") or []) | {"recipe-media-sources-v1"}
            if len(bindings or []) > 1:
                features.add("recipe-image-bindings-v1")
            from ..routers.worker_ws import get_available_models
            available = await get_available_models(job_type="video", media_features=tuple(sorted(features)))
            if model not in available:
                raise HTTPException(503, detail="No compatible source-conditioned video worker is available")
            payload = {**payload, "required_media_features": sorted(features)}
        execution = resolved_recipe["execution"]
        # Snapshot one validated graph before money moves. Every ingress,
        # including chat, gets its billing/reward units from that same graph.
        payload = {**payload, **{key: execution[key] for key in ("width", "height", "frames", "fps", "seconds", "steps")},
                   "cfg_scale": execution["cfg"], "sampler_name": execution["sampler"],
                   "length": execution["frames"], "video_length": execution["frames"],
                   "seed": resolved_recipe["seed"], "seeds": [resolved_recipe["seed"]]}
    return payload, resolved_recipe


def bind_dispatch_recipe(model: str, payload: dict, spec: dict | None) -> tuple[dict, list[str]]:
    """Attach the already resolved graph and bindings, without re-resolving."""
    if spec is not None:
        payload = {**payload, "recipe_engine": spec["engine"], "recipe_spec": spec["spec"],
                   "recipe_root": spec["recipe_root"], "recipe_id": spec.get("recipe_id"),
                   "seed": spec["seed"], "deterministic": spec["deterministic"]}
        if payload.get("loras") and spec.get("lora_inject"):
            payload["recipe_lora_inject"] = spec["lora_inject"]
        if payload.get("source_image_url") and spec.get("image_paths"):
            payload["recipe_image_inputs"] = spec["image_paths"]
        if spec.get("image_bindings"):
            payload["recipe_image_bindings"] = spec["image_bindings"]
        if spec.get("source_bindings"):
            payload["recipe_source_bindings"] = spec["source_bindings"]
    route_models = spec.get("required_models") if spec and spec.get("required_models") else [model]
    return payload, route_models


async def submit_and_wait(model: str, job_type: str, payload: dict, timeout: int,
                          account_id=None, concurrency_limit: int | None = None,
                          preferred_worker: str = "", progress_token: str = "",
                          billing_user: dict | None = None,
                          max_cost_micro_usd: int | None = None) -> tuple[list[dict], dict]:
    """Reserve, queue and wait for committed media; HTTP timeouts do not cancel it.

    The caller proves ownership of soft worker affinity. Returned outputs and
    metadata describe the existing durable worker terminal, not another settler.
    """
    if max_cost_micro_usd is not None and (type(max_cost_micro_usd) is not int
                                         or not 0 <= max_cost_micro_usd <= 2**63 - 1):
        raise HTTPException(status_code=422, detail="Invalid maximum media cost")
    if progress_token and (not isinstance(progress_token, str) or len(progress_token) > 128):
        raise HTTPException(status_code=422, detail="progress_token must be at most 128 characters")
    payload, resolved_recipe = await prepare_dispatch(model, job_type, payload)
    n = int(payload.get("n", 1) or 1)
    job_id = str(uuid4())

    # Billing gate: media cost is deterministic from the request (n images /
    # video seconds), so RESERVE the exact cost before dispatch in live mode and
    # fail CLOSED with 402 if funds can't be held. Pre-dispatch failures release
    # here; after a submission attempt, the durable terminal/sweeper owns the
    # hold, not the HTTP wait timeout. No-op in dry-run; unpriced/no-account deny.
    # Reserve the EXACT cost + write the durable reservation row in one
    # transaction (record_reservation), so the worker-WS handler is the sole
    # settler (settle_exact on success / release_job on failure) and a crash can't
    # strand the hold — the sweeper releases stale 'held' rows. No-op in dry-run.
    seconds = payload.get("seconds") or (payload.get("recipe_inputs") or {}).get("seconds")
    if job_type == "video" and not seconds:
        # Bill the exact graph default. Recipe display names can share a
        # checkpoint while baking different durations (e.g. 5s Director vs
        # 10s Audio), so a single global fallback would undercharge one.
        seconds = recipes.baked_default_for_model(
            model,
            "seconds",
            payload.get("recipe_inputs") or {},
            has_source=bool(payload.get("source_image_url")),
        ) or DEFAULT_VIDEO_SECONDS
    cost_limit = {"max_cost_micro_usd": max_cost_micro_usd} if max_cost_micro_usd is not None else {}
    auth = await credits.authorize_media(account_id, model, job_type, n, seconds, job_id,
                                         record_reservation=True, user=billing_user,
                                         client_ref=progress_token or None, **cost_limit)
    if not auth["ok"]:
        raise HTTPException(status_code=402, detail=auth.get("reason", "payment required"))
    if account_id is not None and concurrency_limit:
        if not await _inflight_acquire(account_id, concurrency_limit):
            # Rejected BEFORE dispatch — worker_ws never sees it, so release here.
            await credits.release_job(job_id)
            raise HTTPException(status_code=429,
                                detail=f"Too many concurrent jobs (limit {concurrency_limit}). Retry shortly.")
    dispatch_state = {"attempted": False}
    try:
        return await _submit_and_wait_inner(model, job_type, payload, timeout, job_id,
                                            preferred_worker=preferred_worker,
                                            progress_token=progress_token,
                                            dispatch_state=dispatch_state,
                                            resolved_recipe=resolved_recipe)
    except Exception:
        # Before a queue submission attempt, this request is the only lifecycle
        # authority and must release its hold. After dispatch, worker_ws is the
        # sole terminal authority: an HTTP timeout/disconnect can race a valid
        # late worker success, so releasing here could refund the customer and
        # make the completed worker ineligible for payout. The worker terminal,
        # dead-letter path, or stale-hold sweeper will settle/release it.
        if not dispatch_state["attempted"]:
            await credits.release_job(job_id)
        raise
    finally:
        if account_id is not None and concurrency_limit:
            await _inflight_release(account_id)


async def _submit_and_wait_inner(model: str, job_type: str, payload: dict, timeout: int,
                                 job_id: str, preferred_worker: str = "", progress_token: str = "",
                                 dispatch_state: dict | None = None,
                                 resolved_recipe: dict | None = None) -> tuple[list[dict], dict]:

    # Recipe-governed path: if `model` selects an approved RecipeVault recipe,
    # resolve it to a concrete ComfyUI graph and ride it in the payload — the
    # worker then executes the graph directly (dumb executor). If no recipe maps
    # to this model, fall through to the legacy model-name dispatch (worker-side
    # model_mapper) so nothing breaks during migration. See RECIPE_DISPATCH.md.
    try:
        # Build recipe inputs from INTENT only, not the whole payload: prompt/seed/
        # negative/image are always intended; numeric knobs (size, seconds, …) ride
    # in `recipe_inputs` and are present ONLY when the caller set them explicitly.
        # An omitted knob never reaches the resolver, so it keeps the recipe's baked
        # default (the dumb happy path). i2v start frame may arrive as `source_image`.
        # NB: the image slot is NOT injected here. When a source image is supplied
        # it rides as `source_image_url` and the worker loads it into ComfyUI and
        # points the LoadImage node(s) at it; absent that, the recipe's baked default
        # frame stands. So the grid only injects text/seed + the numeric knobs.
        inputs = {
            "prompt": payload.get("prompt"),
            "negative_prompt": payload.get("negative_prompt"),
            "seed": payload.get("seed"),
            **(payload.get("recipe_inputs") or {}),
        }
        spec = resolved_recipe if resolved_recipe is not None else recipes.resolve_for_model(
            model, inputs, has_source=bool(payload.get("source_image_url")))
    except recipes.RecipeError as e:
        # Out-of-range / invalid knob → reject so the caller learns, not silently altered.
        raise HTTPException(status_code=422, detail=str(e))
    # Route on the recipe's requiredModels (the checkpoint names WORKERS advertise),
    # not the recipe's display name: a recipe can define a NEW model ("LTX-2.3 Audio")
    # served by an EXISTING checkpoint ("LTX-2.3"). The worker matches on what it has and
    # runs the resolved recipe_spec regardless of display name. Falls back to the model
    # name for the legacy path (no recipe) or a recipe with no requiredModels.
    payload, route_models = bind_dispatch_recipe(model, payload, spec)
    # Mark before the Redis call. A connection can drop after Redis accepted
    # XADD but before its reply reached Core; conservatively treating that as
    # dispatched leaves the hold to the durable terminal/sweeper instead of
    # risking completed-but-refunded work.
    if dispatch_state is not None:
        dispatch_state["attempted"] = True
    await job_queue.submit_job(job_id, payload, route_models, job_type=job_type,
                               preferred_worker=preferred_worker, progress_token=progress_token)
    logger.info(f"{job_type} job {job_id} queued model={model} route={route_models}"
                + (f" preferred_worker={preferred_worker}" if preferred_worker else ""))

    async for event in token_stream.subscribe_tokens(job_id, timeout=timeout):
        if event.get("text") != token_stream.DONE_SENTINEL:
            continue  # progress event — skip
        if event.get("error"):
            raise HTTPException(status_code=event.get("code") or 502, detail=event["error"])
        try:
            result = json.loads(event.get("full_text") or "{}")
        except (TypeError, ValueError):
            raise HTTPException(status_code=500, detail="Malformed worker result")
        media = [o for o in result.get("media", []) if o.get("url")]
        if not media:
            raise HTTPException(status_code=502, detail="No media returned")
        logger.info(f"{job_type} job {job_id} done: {len(media)} output(s)")
        meta = {
            "job_id": job_id,
            "worker": result.get("worker", ""),
            "gen_time": result.get("gen_time"),
            "model": result.get("model", model),
            "recipe_root": result.get("recipe_root"),
        }
        if spec and spec.get("execution"):
            meta["execution"] = spec["execution"]
        return media, meta

    raise HTTPException(status_code=504, detail=f"{job_type} generation timed out")


async def url_to_b64(url: str) -> str:
    """Fetch a generated asset and base64-encode it (for response_format=b64_json)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        r.raise_for_status()
        return base64.b64encode(r.content).decode()

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared media (image / video) generation helpers.

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

logger = logging.getLogger("grid_api.media")

DEFAULT_IMAGE_MODEL = "FLUX.2 [klein]"
DEFAULT_VIDEO_MODEL = "LTX-2.3"

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
        w, h = smart_size(sw, sh)
        dims = {"width": w, "height": h}
    url = await asyncio.to_thread(storage.upload_source, raw, ext)
    return dims, url


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


async def submit_and_wait(model: str, job_type: str, payload: dict, timeout: int,
                          account_id=None, concurrency_limit: int | None = None,
                          preferred_worker: str = "", progress_token: str = "") -> tuple[list[dict], dict]:
    """Queue a media job and block until the worker reports the result.

    Returns (outputs, meta): the list of output objects ({url, key, sha256,
    seed}) and a meta dict {worker, gen_time, model} describing which worker ran
    it and how long it took. Progress events on the channel are ignored; the
    DONE event carries the media JSON.

    If account_id + concurrency_limit are given, enforces a per-account in-flight cap
    (long media jobs would otherwise let one key monopolize the worker pool) — 429 over.

    `preferred_worker` (a worker NAME the caller has verified the account owns)
    expresses soft affinity — the grid prefers that worker but won't stall if it's
    offline or busy."""
    job_id = str(uuid4())

    # Billing gate: media cost is deterministic from the request (n images /
    # video seconds), so RESERVE the exact cost before dispatch in live mode and
    # fail CLOSED with 402 if funds can't be held. Refund on every non-running
    # path (429 / fault / timeout) so a job that didn't produce output is never
    # charged. No-op in dry-run. Same enforce policy as text (unpriced/no-acct).
    # Reserve the EXACT cost + write the durable reservation row in one
    # transaction (record_reservation), so the worker-WS handler is the sole
    # settler (settle_exact on success / release_job on failure) and a crash can't
    # strand the hold — the sweeper releases stale 'held' rows. No-op in dry-run.
    reserved = 0
    if account_id is not None and credits.CHARGING_ENABLED:
        n = int(payload.get("n", 1) or 1)
        seconds = (payload.get("recipe_inputs") or {}).get("seconds")
        if job_type == "video" and not seconds:
            # the recipe's baked default duration isn't known to the grid; bill a
            # conservative default rather than letting it slip through free.
            seconds = DEFAULT_VIDEO_SECONDS
        auth = await credits.authorize_media(account_id, model, job_type, n, seconds, job_id,
                                             record_reservation=True)
        if not auth["ok"]:
            raise HTTPException(status_code=402, detail=auth.get("reason", "payment required"))
        reserved = auth["reserved"]

    if account_id is not None and concurrency_limit:
        if not await _inflight_acquire(account_id, concurrency_limit):
            # Rejected BEFORE dispatch — worker_ws never sees it, so release here.
            await credits.release_job(job_id)
            raise HTTPException(status_code=429,
                                detail=f"Too many concurrent jobs (limit {concurrency_limit}). Retry shortly.")
    try:
        return await _submit_and_wait_inner(model, job_type, payload, timeout, job_id,
                                            preferred_worker=preferred_worker,
                                            progress_token=progress_token)
    except Exception:
        # Generation failed/timed out/never-dispatched → release the hold. Idempotent
        # with worker_ws's terminal settle via the held→settled conditional UPDATE
        # (whoever reaches terminal first wins; the other is a no-op).
        await credits.release_job(job_id)
        raise
    finally:
        if account_id is not None and concurrency_limit:
            await _inflight_release(account_id)


async def _submit_and_wait_inner(model: str, job_type: str, payload: dict, timeout: int,
                                 job_id: str, preferred_worker: str = "", progress_token: str = "") -> tuple[list[dict], dict]:

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
        spec = recipes.resolve_for_model(
            model, inputs, has_source=bool(payload.get("source_image_url")))
    except recipes.RecipeError as e:
        # Out-of-range / invalid knob → reject so the caller learns, not silently altered.
        raise HTTPException(status_code=422, detail=str(e))
    if spec is not None:
        payload = {**payload, "recipe_engine": spec["engine"], "recipe_spec": spec["spec"],
                   "recipe_root": spec["recipe_root"], "recipe_id": spec.get("recipe_id"),
                   "seed": spec["seed"], "deterministic": spec["deterministic"]}
        # LoRAs: ride the request's validated `loras` + the recipe's injection spec to the
        # worker, which downloads/verifies the weights and splices LoraLoader nodes.
        if payload.get("loras") and spec.get("lora_inject"):
            payload["recipe_lora_inject"] = spec["lora_inject"]
        logger.info(f"{job_type} job {job_id} resolved recipe {spec['recipe_root']} "
                    f"({spec['name']}, engine={spec['engine']})"
                    + (f" +{len(payload['loras'])} lora(s)" if payload.get('loras') else ""))

    await job_queue.submit_job(job_id, payload, [model], job_type=job_type,
                               preferred_worker=preferred_worker, progress_token=progress_token)
    logger.info(f"{job_type} job {job_id} queued model={model}"
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
            "worker": result.get("worker", ""),
            "gen_time": result.get("gen_time"),
            "model": result.get("model", model),
        }
        return media, meta

    raise HTTPException(status_code=504, detail=f"{job_type} generation timed out")


async def url_to_b64(url: str) -> str:
    """Fetch a generated asset and base64-encode it (for response_format=b64_json)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        r.raise_for_status()
        return base64.b64encode(r.content).decode()

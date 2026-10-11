# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Default-off, credit-backed async video submission and private recovery."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ConfigDict, Field

from ..auth import extract_api_key
from ..config import get_settings
from ..ratelimit import limiter
from ..services import accounts, media, media_jobs
from .videos import VideoRequest, prepare_video

MAX_BODY_BYTES = 32 * 1024 * 1024 + 64 * 1024
PRIVATE = {"Cache-Control": "no-store"}


class BoundedVideoRoute(APIRoute):
    """Bound before FastAPI JSON validation and redact its input-bearing errors."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def guarded(request: Request):
            try:
                if request.method == "POST":
                    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                        raise HTTPException(415, detail="Content-Type must be application/json")
                    length = request.headers.get("content-length")
                    if length is not None:
                        if not length.isascii() or not length.isdigit() or len(length) > 20:
                            raise HTTPException(400, detail="Invalid Content-Length")
                        if int(length) > MAX_BODY_BYTES:
                            raise HTTPException(413, detail="Video request exceeds the body limit")
                    content = bytearray()
                    async for chunk in request.stream():
                        if len(content) + len(chunk) > MAX_BODY_BYTES:
                            raise HTTPException(413, detail="Video request exceeds the body limit")
                        content.extend(chunk)
                    # Starlette's body cache lets the original typed route read
                    # the bounded body, preserving generated OpenAPI schemas.
                    request._body = bytes(content)
                response = await original(request)
                response.headers.update(PRIVATE)
                return response
            except RequestValidationError:
                return JSONResponse({"detail": "Invalid async video request"}, status_code=422, headers=PRIVATE)
            except RecursionError:
                return JSONResponse({"detail": "Video request exceeds JSON nesting limit"}, status_code=422, headers=PRIVATE)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers={**(exc.headers or {}), **PRIVATE})

        return guarded


router = APIRouter(route_class=BoundedVideoRoute)


class AsyncVideoRequest(VideoRequest):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    prompt: str = Field(min_length=1, max_length=8000)
    model: str = Field(default="LTX-2.5 Distilled BF16", min_length=1, max_length=255)
    n: int = Field(default=1, ge=1, le=1, strict=True)
    size: str = Field(default="768x512", max_length=32)
    image: str | None = Field(default=None, min_length=1, max_length=17_000_000)
    negative_prompt: str = Field(default="", max_length=8000)
    seed: int | None = Field(default=None, ge=0, le=2**53 - 1, strict=True)
    steps: int | None = Field(default=None, ge=4, le=40, strict=True)
    cfg_scale: float | None = Field(default=None, ge=1, le=5)
    sampler: Literal["euler_ancestral_cfg_pp"] = "euler_ancestral_cfg_pp"
    audio: bool = True
    response_format: Literal["url"] = "url"
    worker: str | None = Field(default=None, min_length=1, max_length=255)
    style: str | None = Field(default=None, min_length=1, max_length=128)
    progress_token: None = None
    max_cost_micro_usd: int = Field(gt=0, le=2**63 - 1, strict=True)


def request_hash(body: AsyncVideoRequest) -> str:
    """Commit intent before generated seeds/uploads; retain only input digests."""
    if body.image and body.keyframes:
        raise HTTPException(422, detail="Use image or keyframes, not both")
    intent = body.model_dump(mode="json")
    if "size" not in body.model_fields_set:
        intent.pop("size")  # Omitted size fits a source/default; explicit size pins it.
    sources = [pin.image for pin in body.keyframes] if body.keyframes else [body.image] if body.image else []
    decoded = media.decode_video_keyframes(sources) if sources else []
    digests = [hashlib.sha256(item[0]).hexdigest() for item in decoded]
    if body.image:
        intent["image"] = {"sha256": digests[0]}
    for pin, digest in zip(intent["keyframes"], digests):
        pin["image"] = {"sha256": digest}
    encoded = json.dumps({"schema": "aipg.video-intent.v1", "request": intent},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


async def _authenticate(apikey, authorization, assertion, token):
    return await accounts.authenticate(extract_api_key(apikey, authorization), assertion,
                                       user_token=token, required_scope="inference.submit")


def _error(exc: Exception) -> JSONResponse:
    headers = {**(exc.headers or {}), **PRIVATE} if isinstance(exc, HTTPException) else PRIVATE
    if isinstance(exc, HTTPException):
        code, detail = exc.status_code, exc.detail
    elif isinstance(exc, media_jobs.MediaJobConflict):
        code, detail = 409, "Idempotency key conflicts with another video request"
    elif isinstance(exc, media_jobs.MediaJobPaymentRequired):
        code, detail = 402, "A positive live credit hold within the cost ceiling is required"
    elif isinstance(exc, media_jobs.MediaJobCapacityExceeded):
        code, detail = 429, "Too many held async video jobs; retry after one finishes"
    elif isinstance(exc, media_jobs.MediaJobError):
        code, detail = 422, "Invalid async video request identity or execution contract"
    else:
        code, detail = 503, "Async video temporarily unavailable; retry with the same idempotency key"
    return JSONResponse({"detail": detail}, status_code=code, headers=headers)


@router.post("/v1/videos/jobs", status_code=202)
@limiter.limit("4/minute")
async def submit_video_job(
    request: Request, body: AsyncVideoRequest,
    idempotency_key: str | None = Header(None, max_length=64),
    apikey: str | None = Header(None), authorization: str | None = Header(None),
    x_grid_user_assertion: str | None = Header(None), x_grid_user_token: str | None = Header(None),
):
    try:
        settings = get_settings()
        user = await _authenticate(apikey, authorization, x_grid_user_assertion, x_grid_user_token)
        media_jobs.validate_idempotency_key(idempotency_key)
        intent = request_hash(body)
        row = await media_jobs.lookup_request(user["account_id"], idempotency_key, intent)
        if row is None:
            if not settings.media_async_enabled:
                raise HTTPException(503, detail="Async video admission is disabled")
            native = body.model_dump(exclude_unset=True, exclude_none=True, exclude={"max_cost_micro_usd"})
            native["model"] = body.model
            model, payload, _seeds = await prepare_video(VideoRequest.model_validate(native), user, async_submission=True)
            payload, recipe = await media.prepare_dispatch(model, "video", payload)
            payload, route_models = media.bind_dispatch_recipe(model, payload, recipe)
            prepared = media_jobs.prepare(account_id=user["account_id"], idempotency_key=idempotency_key,
                request_hash=intent, model=model, payload=payload, route_models=route_models,
                preferred_worker=body.worker or "", max_cost_micro=body.max_cost_micro_usd)
            row = await media_jobs.reserve(prepared, user=user, concurrency_limit=settings.media_async_concurrency)
        status = await media_jobs.recover(user["account_id"], row["job_id"])
        if status is None:
            raise media_jobs.MediaJobError("committed video is not recoverable")
        # SQL commit, not an HTTP response or Redis acknowledgment, accepts the
        # request. The bounded outbox handles first dispatch and lost replies.
        terminal = status["state"] in {"completed", "closed_without_result"}
        headers = {**PRIVATE, "Location": f"/v1/videos/jobs/{row['job_id']}"}
        if not terminal:
            headers["Retry-After"] = "5"
        return JSONResponse({"schema": "aipg.video-job.v1", **status},
                            status_code=200 if terminal else 202, headers=headers)
    except Exception as exc:
        return _error(exc)


@router.get("/v1/videos/jobs/{job_id}")
@limiter.limit("60/minute")
async def get_video_job(
    request: Request, job_id: UUID,
    apikey: str | None = Header(None), authorization: str | None = Header(None),
    x_grid_user_assertion: str | None = Header(None), x_grid_user_token: str | None = Header(None),
):
    try:
        user = await _authenticate(apikey, authorization, x_grid_user_assertion, x_grid_user_token)
        status = await media_jobs.recover(user["account_id"], job_id)
        if status is None:
            raise HTTPException(404, detail="No video job found")
        return JSONResponse({"schema": "aipg.video-job.v1", **status}, headers=PRIVATE)
    except Exception as exc:
        return _error(exc)


@router.get("/v1/videos/jobs")
@limiter.limit("60/minute")
async def get_video_request(
    request: Request, idempotency_key: str | None = Header(None, max_length=64),
    apikey: str | None = Header(None), authorization: str | None = Header(None),
    x_grid_user_assertion: str | None = Header(None), x_grid_user_token: str | None = Header(None),
):
    """Recover a lost submission reply without another POST or input upload."""
    try:
        user = await _authenticate(apikey, authorization, x_grid_user_assertion, x_grid_user_token)
        media_jobs.validate_idempotency_key(idempotency_key)
        status = await media_jobs.recover(user["account_id"], idempotency_key=idempotency_key)
        if status is None:
            raise HTTPException(404, detail="No video job found")
        return JSONResponse({"schema": "aipg.video-job.v1", **status}, headers=PRIVATE)
    except Exception as exc:
        return _error(exc)

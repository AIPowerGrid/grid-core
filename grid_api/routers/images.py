# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI-compatible /v1/images/generations endpoint — native v2 dispatch.

Jobs go straight onto the media Redis Stream and are served by WS-connected
media workers (same dispatch machinery as text). No Flask proxy: the worker
uploads results directly to R2 via presigned URLs and the completion arrives
on the job's pub/sub channel.
"""

import json
import logging
import time
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional

from ..auth import extract_api_key, hash_api_key
from ..database import new_session, users_table
from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..services import job_queue, quota, token_stream

logger = logging.getLogger("grid_api.images")

router = APIRouter()

DEFAULT_IMAGE_MODEL = "FLUX.2 [klein]"

# Max seconds to wait for a media job before giving up.
MEDIA_TIMEOUT = 300


class ImageRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    quality: Optional[str] = "standard"
    response_format: Optional[str] = "url"


@router.post("/v1/images/generations")
@limiter.limit("10/minute")
async def create_image(
    request: Request,
    body: ImageRequest,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """OpenAI-compatible image generation.

    Submits to the Grid v2 image API, polls for completion, returns
    the result in OpenAI format. Uses poll-based workers (not streaming).
    """
    try:
        key = extract_api_key(apikey, authorization)
        # v2 account keys first, legacy Haidra keys as fallback.
        user = await accounts_svc.authenticate(key)
        await quota.check_and_consume(dict(user))
        return await _handle_image_gen(body, key)
    except HTTPException:
        raise
    except Exception as e:
        # Log full detail server-side; return generic message to the client.
        logger.error(f"Image generation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error while processing the request.")


async def _handle_image_gen(request: ImageRequest, apikey: str):
    # Parse size
    try:
        width, height = map(int, request.size.split("x"))
    except ValueError:
        width, height = 1024, 1024

    model = request.model or DEFAULT_IMAGE_MODEL

    # Adjust params based on model
    # FLUX.2 klein: 4 steps, cfg 1.0 (distilled). FLUX.1 schnell: same.
    # Other flux models: 20 steps. Non-flux: 30 steps, higher cfg.
    is_flux = "flux" in model.lower()
    is_fast_flux = "klein" in model.lower() or "schnell" in model.lower()
    steps = 4 if is_fast_flux else (20 if is_flux else 30)
    cfg_scale = 1.0 if is_fast_flux else (3.5 if is_flux else 7.5)
    sampler = "euler" if is_flux else "k_euler"

    job_id = str(uuid4())
    payload = {
        "prompt": request.prompt,
        "n": request.n,
        "width": width,
        "height": height,
        "steps": steps,
        "sampler_name": sampler,
        "cfg_scale": cfg_scale,
        "ext": "webp",
    }

    await job_queue.submit_job(job_id, payload, [model], job_type="image")
    logger.info(f"Image job {job_id} queued for model={model} size={width}x{height} n={request.n}")

    # Wait for the worker's completion on the job's pub/sub channel.
    # Progress events arrive as plain tokens (JSON text) and are skipped here;
    # the done event carries the media result as JSON in full_text.
    async for event in token_stream.subscribe_tokens(job_id, timeout=MEDIA_TIMEOUT):
        if event.get("error"):
            raise HTTPException(status_code=502, detail=event["error"])
        if "full_text" in event:
            try:
                result = json.loads(event["full_text"])
            except (TypeError, ValueError):
                raise HTTPException(status_code=500, detail="Malformed worker result")
            data = [
                {"url": o["url"], "revised_prompt": request.prompt}
                for o in result.get("media", [])
                if o.get("url")
            ]
            if not data:
                raise HTTPException(status_code=500, detail="No images returned")
            logger.info(f"Image job {job_id} completed: {len(data)} images")
            return {"created": int(time.time()), "data": data}

    raise HTTPException(status_code=504, detail="Image generation timed out")

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI-compatible /v1/images/generations endpoint.

Image generation uses the existing poll-based Flask API under the hood.
This endpoint translates OpenAI format → Grid v2 API → polls → returns result.
Default model: Flux Schnell (fast) or whatever is available.
"""

import asyncio
import logging
import time

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
from pydantic import BaseModel, Field
from typing import Optional

from ..auth import extract_api_key, hash_api_key
from ..config import get_settings

logger = logging.getLogger("grid_api.images")

router = APIRouter()

DEFAULT_IMAGE_MODEL = "FLUX.2 [klein]"
GRID_API_BASE = None  # Lazy init


def _get_grid_api_base() -> str:
    global GRID_API_BASE
    if GRID_API_BASE is None:
        settings = get_settings()
        # Talk to the Flask API on localhost
        GRID_API_BASE = "http://127.0.0.1:7001"
    return GRID_API_BASE


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
        return await _handle_image_gen(body, key)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image generation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _handle_image_gen(request: ImageRequest, apikey: str):
    base = _get_grid_api_base()
    headers = {"apikey": apikey, "Content-Type": "application/json"}

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

    payload = {
        "prompt": request.prompt,
        "nsfw": False,
        "censor_nsfw": True,
        "trusted_workers": False,
        "models": [model],
        "r2": True,
        "params": {
            "n": request.n,
            "width": width,
            "height": height,
            "steps": steps,
            "sampler_name": sampler,
            "cfg_scale": cfg_scale,
        },
    }

    async with httpx.AsyncClient(timeout=120) as client:
        # Submit job to Flask v2 API
        resp = await client.post(f"{base}/api/v2/generate/async", headers=headers, json=payload)
        if resp.status_code not in (200, 202):
            detail = resp.text[:200]
            logger.error(f"Grid API submit error {resp.status_code}: {detail}")
            raise HTTPException(status_code=resp.status_code, detail=detail)

        job_data = resp.json()
        job_id = job_data.get("id")
        if not job_id:
            raise HTTPException(status_code=500, detail="No job ID returned from Grid API")

        logger.info(f"Image job {job_id} submitted for model={model} size={width}x{height}")

        # Poll for completion
        for attempt in range(60):  # Max 2 minutes
            await asyncio.sleep(2)
            check = await client.get(f"{base}/api/v2/generate/check/{job_id}", headers=headers)
            if check.status_code != 200:
                continue

            check_data = check.json()
            if check_data.get("done"):
                # Fetch final result
                status = await client.get(f"{base}/api/v2/generate/status/{job_id}", headers=headers)
                if status.status_code != 200:
                    raise HTTPException(status_code=500, detail="Failed to fetch completed image")

                status_data = status.json()
                generations = status_data.get("generations", [])

                # Build OpenAI-format response
                data = []
                for gen in generations:
                    img_url = gen.get("img")
                    if not img_url:
                        img_id = gen.get("id")
                        if img_id:
                            img_url = f"https://images.aipg.art/{img_id}.webp"
                    if img_url:
                        data.append({"url": img_url, "revised_prompt": request.prompt})

                if not data:
                    raise HTTPException(status_code=500, detail="No images returned")

                logger.info(f"Image job {job_id} completed: {len(data)} images")
                return {"created": int(time.time()), "data": data}

            if check_data.get("faulted"):
                raise HTTPException(status_code=500, detail="Image generation faulted")

        raise HTTPException(status_code=504, detail="Image generation timed out")

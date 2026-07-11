# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""/v1/3d/generations — image-to-3D mesh generation (TRELLIS).

Takes a single source image and returns a 3D mesh (GLB). Jobs go onto the media
Redis Stream with job_type=3d and are served by a media worker whose ComfyUI has
the TRELLIS nodes. There is no prompt — TRELLIS is image-conditioned only.
Output is slow (model load + sparse 3D sampling), so callers should use
`progress_token` + GET /v1/progress/{token} rather than a long blocking request.
"""

import logging
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ..auth import extract_api_key
from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..services import media, quota
from .worker_ws import get_available_models

logger = logging.getLogger("grid_api.threed")

router = APIRouter()


class ThreeDRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    image: str                              # REQUIRED — source image (base64 / data: URI)
    model: Optional[str] = None
    seed: Optional[int] = None
    steps: Optional[int] = None             # sampling steps (shape) — gated by the recipe
    guidance: Optional[float] = None        # classifier-free guidance — gated by the recipe
    target_faces: Optional[int] = None      # mesh decimation target — gated by the recipe
    response_format: Optional[str] = "url"  # "url" | "b64_json"
    worker: Optional[str] = None            # soft-affinity: prefer this worker (must own it)
    progress_token: Optional[str] = None    # poll live % at GET /v1/progress/{token}


@router.post("/v1/3d/generations")
@limiter.limit("4/minute")
async def create_3d(
    request: Request,
    body: ThreeDRequest,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    x_grid_user_assertion: Optional[str] = Header(None),
):
    """Image-to-3D mesh generation (grid-native envelope)."""
    try:
        key = extract_api_key(apikey, authorization)
        user = await accounts_svc.authenticate(
            key, x_grid_user_assertion, required_scope="inference.submit",
        )

        if body.worker:
            await accounts_svc.assert_owns_worker(user, body.worker)

        model = body.model or media.DEFAULT_3D_MODEL

        available = await get_available_models(job_type="3d")
        if not available:
            raise HTTPException(status_code=503, detail="No 3D workers are online.")
        if model not in available:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{model}' is not available. Online 3D models: {available}",
            )

        await quota.check_and_consume(dict(user))

        # image is required — decode/upload the source, worker binds it to LoadImage.
        _dims, source_image_url = await media.prepare_source_image(
            model, body.image, size_was_set=False)

        seed = media.normalize_seed(body.seed)

        # Recipe knobs — present only when the caller set them (else the recipe's
        # baked default stands). Out-of-band values are rejected (422) by the resolver.
        recipe_inputs: dict = {}
        for name, val in (("steps", body.steps), ("guidance", body.guidance),
                          ("target_faces", body.target_faces)):
            if val is not None:
                recipe_inputs[name] = val

        payload = {
            "n": 1,
            "ext": "glb",
            "seed": seed,
            "seeds": [seed],
            "source_image_url": source_image_url,
        }
        if recipe_inputs:
            payload["recipe_inputs"] = recipe_inputs

        outputs, meta = await media.submit_and_wait(
            model, "3d", payload, media.THREED_TIMEOUT,
            account_id=user.get("account_id"), concurrency_limit=media.MEDIA_CONCURRENCY,
            preferred_worker=body.worker or "", progress_token=body.progress_token or "")

        want_b64 = body.response_format == "b64_json"
        data = []
        for o in outputs:
            item = {"seed": o.get("seed", seed)}
            if want_b64:
                item["b64_json"] = await media.url_to_b64(o["url"])
            else:
                item["url"] = o["url"]
            data.append(item)

        return {"created": int(time.time()), "data": data, "grid": meta}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("3D generation failed")
        raise HTTPException(status_code=500, detail=str(e))

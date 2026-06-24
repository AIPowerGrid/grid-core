# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI Responses API (`/v1/responses`) — raw passthrough.

Routed only to workers whose backend natively exposes `/v1/responses` (e.g.
recent vLLM). No translation: the client's request is forwarded as-is and the
upstream events are relayed verbatim. If no worker serves the format, 503.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import extract_api_key
from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..services import credits, quota
from ._passthrough import (
    SSE_HEADERS,
    collect_passthrough,
    deep_sanitize,
    stream_passthrough,
    submit_passthrough_job,
)
from .worker_ws import get_available_models

logger = logging.getLogger("grid_api.responses")

router = APIRouter()

API_FORMAT = "openai-responses"


@router.post("/v1/responses")
@limiter.limit("30/minute")
async def create_response(
    request: Request,
    body: dict = Body(...),
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """OpenAI-compatible Responses endpoint (raw passthrough to a capable worker)."""
    try:
        key = extract_api_key(apikey, authorization)
        user = await accounts_svc.authenticate(key)

        model = body.get("model")
        if not model:
            raise HTTPException(status_code=400, detail="'model' is required.")

        available = await get_available_models(job_type="text", api_format=API_FORMAT)
        if not available:
            raise HTTPException(
                status_code=503,
                detail="No workers serving the OpenAI Responses API are online.",
            )
        if model not in available:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{model}' is not available via the Responses API. Online: {available}",
            )

        # Billing gate: this is a RAW passthrough — the grid relays upstream
        # events verbatim and cannot reliably count tokens grid-side for this
        # format, so it has no trusted meter. Until per-request metering is wired
        # (parse Responses `input`/output server-side, or reserve+reconcile), we
        # fail CLOSED when charging is on rather than serve paid work for free.
        if credits.CHARGING_ENABLED:
            raise HTTPException(
                status_code=402,
                detail="Per-request billing is not yet available for the Responses API; use /v1/chat/completions.",
            )

        await quota.check_and_consume(dict(user))

        raw = deep_sanitize(dict(body))
        max_len = int(raw.get("max_output_tokens") or raw.get("max_tokens") or 4096)
        job_id = await submit_passthrough_job(model, API_FORMAT, raw, max_len)

        if raw.get("stream"):
            return StreamingResponse(
                stream_passthrough(job_id), media_type="text/event-stream", headers=SSE_HEADERS
            )
        return JSONResponse(await collect_passthrough(job_id))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"responses error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error while processing the request.")

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Anthropic Messages API (`/v1/messages`) — raw passthrough.

The grid does NOT translate Anthropic <-> OpenAI. This endpoint is backed by
the pool of workers whose inference engine NATIVELY exposes `/v1/messages`. If
no such worker is connected the endpoint returns 503 — there is simply no
capacity behind it (vLLM, for instance, does not serve this format, so until an
Anthropic-native backend joins, /v1/messages is honestly unavailable).

When a capable worker exists, the client's request is forwarded as-is and the
upstream Anthropic SSE events are relayed verbatim; the grid only tees `usage`
for den metering.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..ratelimit import limiter
from ..services import accounts as accounts_svc
from ..services import quota
from ._passthrough import (
    SSE_HEADERS,
    authorize_passthrough,
    collect_passthrough,
    deep_sanitize,
    new_passthrough_job_id,
    stream_passthrough,
    submit_passthrough_job,
)
from .worker_ws import get_available_models

logger = logging.getLogger("grid_api.anthropic")

router = APIRouter()

API_FORMAT = "anthropic"


def _err(status: int, etype: str, message: str) -> HTTPException:
    """Anthropic-shaped error envelope."""
    return HTTPException(status_code=status, detail={"type": etype, "message": message})


@router.post("/v1/messages")
@limiter.limit("30/minute")
async def create_message(
    request: Request,
    body: dict = Body(...),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
):
    """Anthropic-compatible messages endpoint (raw passthrough to a capable worker)."""
    try:
        # Anthropic SDKs send x-api-key; accept a bearer token too.
        key = x_api_key
        if not key and authorization:
            key = authorization.split(" ", 1)[1] if " " in authorization else authorization
        user = await accounts_svc.resolve_api_key(key or "")
        if not user:
            raise _err(401, "authentication_error", "Invalid API key")

        model = body.get("model")
        if not model:
            raise _err(400, "invalid_request_error", "'model' is required.")

        available = await get_available_models(job_type="text", api_format=API_FORMAT)
        if not available:
            raise _err(503, "overloaded_error", "No workers serving the Anthropic Messages API are online.")
        if model not in available:
            raise _err(
                404, "not_found_error",
                f"Model '{model}' is not available via the Anthropic Messages API. Online: {available}",
            )

        await quota.check_and_consume(dict(user))

        raw = deep_sanitize(dict(body))
        max_len = int(raw.get("max_tokens") or 4096)

        # Billing: reserve BEFORE dispatch on a grid-side prompt count (never the
        # worker's). 402 (Anthropic-shaped) on insufficient funds. Settlement on
        # grid-counted output happens in the stream/collect terminal handler.
        job_id = new_passthrough_job_id()
        auth = await authorize_passthrough(user, model, API_FORMAT, raw, max_len, job_id)
        if not auth["ok"]:
            raise _err(402, "billing_error", auth.get("reason", "Insufficient credits."))
        prompt_toks = auth["prompt_toks"]

        await submit_passthrough_job(job_id, model, API_FORMAT, raw, max_len)

        bill = dict(api_format=API_FORMAT, user=user, model=model, prompt_toks=prompt_toks)
        if raw.get("stream"):
            return StreamingResponse(
                stream_passthrough(job_id, **bill), media_type="text/event-stream", headers=SSE_HEADERS
            )
        return JSONResponse(await collect_passthrough(job_id, **bill))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"messages error: {e}", exc_info=True)
        raise _err(500, "api_error", "Internal error while processing the request.")

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI Embeddings API (`/v1/embeddings`) — raw passthrough.

Routed only to workers whose backend natively answers `/v1/embeddings` for the
model they serve. No translation: the validated request is forwarded and the
backend's vectors come back as they are. If no worker serves the format, 503.

Billing is input-only. The grid counts the input text itself, reserves before
dispatch and settles on the same count; an embedding has no output tokens.
"""

import logging
import os

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth import extract_api_key
from ..ratelimit import limiter
from ..safe_logging import error_type
from ..services import accounts as accounts_svc
from ..services import generation_admission, quota
from ._passthrough import (
    EMBEDDINGS_FORMAT,
    MAX_EMBEDDING_DIMENSIONS,
    authorize_passthrough,
    collect_passthrough,
    deep_sanitize,
    guard_passthrough_body,
    new_passthrough_job_id,
    submit_passthrough_job,
)
from .worker_ws import get_available_models

logger = logging.getLogger("grid_api.embeddings")

router = APIRouter()

API_FORMAT = EMBEDDINGS_FORMAT

# One request carries at most this many texts. The vectors travel back through
# the worker socket and Redis as one JSON body, so the batch is bounded.
MAX_INPUTS = int(os.getenv("GRID_EMBEDDINGS_MAX_INPUTS", "64"))
MAX_DIMENSIONS = MAX_EMBEDDING_DIMENSIONS
_ENCODINGS = ("float", "base64")


def normalize_embeddings_request(body: dict) -> dict:
    """Validate the client body and return the exact request sent to the worker.

    Only the fields the grid understands are forwarded. Token-array input is
    refused: the grid bills on its own count of the text, and it cannot count
    or screen token ids of an unknown tokenizer."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(status_code=400, detail="'model' is required.")

    value = body.get("input")
    texts = [value] if isinstance(value, str) else value
    if not isinstance(texts, list) or not texts:
        raise HTTPException(status_code=400, detail="'input' must be a string or a non-empty list of strings.")
    if len(texts) > MAX_INPUTS:
        raise HTTPException(
            status_code=400,
            detail=f"'input' holds {len(texts)} items; at most {MAX_INPUTS} per request.",
        )
    if not all(isinstance(text, str) for text in texts):
        raise HTTPException(
            status_code=400,
            detail="'input' must be text. Token arrays are not supported.",
        )
    if not all(text for text in texts):
        raise HTTPException(status_code=400, detail="'input' must not contain empty strings.")

    request: dict = {"model": model, "input": value}

    encoding = body.get("encoding_format")
    if encoding is not None:
        if encoding not in _ENCODINGS:
            raise HTTPException(status_code=400, detail="'encoding_format' must be 'float' or 'base64'.")
        request["encoding_format"] = encoding

    dimensions = body.get("dimensions")
    if dimensions is not None:
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) \
                or not 1 <= dimensions <= MAX_DIMENSIONS:
            raise HTTPException(
                status_code=400, detail=f"'dimensions' must be an integer between 1 and {MAX_DIMENSIONS}.",
            )
        request["dimensions"] = dimensions
    return request


def input_count(request: dict) -> int:
    value = request.get("input")
    return len(value) if isinstance(value, list) else 1


def shape_embeddings_response(full: dict, model: str, prompt_toks: int) -> dict:
    """Return the worker's vectors under the grid's model name and usage.

    `usage` is the grid's own count, the one the request is billed on, never
    the number the backend reported."""
    return {
        "object": "list",
        "data": full.get("data") or [],
        "model": model,
        "usage": {"prompt_tokens": int(prompt_toks), "total_tokens": int(prompt_toks)},
    }


@router.post("/v1/embeddings")
@limiter.limit("30/minute")
async def create_embeddings(
    request: Request,
    body: dict = Body(...),
    apikey: str | None = Header(None),
    authorization: str | None = Header(None),
    x_grid_user_assertion: str | None = Header(None),
    x_grid_user_token: str | None = Header(None),
):
    """OpenAI-compatible embeddings endpoint (raw passthrough to a capable worker)."""
    try:
        guard_passthrough_body(body)  # bound size/depth before the recursive walk
        key = extract_api_key(apikey, authorization)
        user = await accounts_svc.authenticate(
            key, x_grid_user_assertion, user_token=x_grid_user_token,
            required_scope="inference.submit",
        )

        # Closed by default: refuse before validation, worker lookup or quota.
        generation_admission.require_path(API_FORMAT)

        raw = deep_sanitize(normalize_embeddings_request(body))
        model = raw["model"]

        available = await get_available_models(job_type="text", api_format=API_FORMAT)
        if not available:
            raise HTTPException(
                status_code=503,
                detail="No workers serving the OpenAI Embeddings API are online.",
            )
        if model not in available:
            raise HTTPException(
                status_code=404,
                detail=f"Model '{model}' is not available via the Embeddings API. Online: {available}",
            )

        await quota.check_and_consume(dict(user))

        # Billing: reserve BEFORE dispatch on the grid's own input count. There
        # is no output budget, so the reservation equals the final charge.
        job_id = new_passthrough_job_id()
        auth = await authorize_passthrough(user, model, API_FORMAT, raw, 0, job_id)
        if not auth["ok"]:
            raise HTTPException(status_code=402, detail=auth.get("reason", "payment required"))
        prompt_toks = auth["prompt_toks"]

        await submit_passthrough_job(job_id, model, API_FORMAT, raw, 0)

        full = await collect_passthrough(
            job_id, api_format=API_FORMAT, user=user, model=model, prompt_toks=prompt_toks,
        )
        return JSONResponse(shape_embeddings_response(full, model, prompt_toks))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("embeddings error [%s]", error_type(e))
        raise HTTPException(status_code=500, detail="Internal error while processing the request.")

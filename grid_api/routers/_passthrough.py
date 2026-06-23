# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared plumbing for raw API passthrough endpoints.

The grid does NOT translate between API formats. Each non-OpenAI-chat endpoint
(Anthropic `/v1/messages`, OpenAI `/v1/responses`) routes to the pool of
workers whose backend NATIVELY serves that format. If that pool is empty the
endpoint returns 503 — the grid never fakes a format it can't serve.

A job carries the client's raw request plus an `api_format` tag; the worker
forwards it to the matching backend endpoint and relays the upstream events
verbatim, which we stream straight back. We only tee `usage` for den metering
(observe mode) — the payload itself is never mutated.
"""

import json
import logging
from uuid import uuid4

from fastapi import HTTPException

from ..services import job_queue, token_stream
from ..services.sanitizer import sanitize

logger = logging.getLogger("grid_api.passthrough")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def deep_sanitize(obj):
    """Recursively scrub credentials from every string in a request body.

    Format-agnostic: works for Anthropic messages, Responses `input`, tool
    arguments, etc. Structure and keys are preserved; only string *values* are
    passed through the secret sanitizer (which is a no-op on normal text)."""
    if isinstance(obj, str):
        return sanitize(obj).text
    if isinstance(obj, list):
        return [deep_sanitize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: deep_sanitize(v) for k, v in obj.items()}
    return obj


async def submit_passthrough_job(model: str, api_format: str, raw_request: dict, max_length: int) -> str:
    """Queue a raw-passthrough job for a worker that serves `api_format`."""
    job_id = str(uuid4())
    payload = {
        "request": raw_request,
        "api_format": api_format,
        "max_length": max_length,
        # Passthrough endpoints are v2-only; no legacy horde bookkeeping rows.
        "_legacy_rows": False,
    }
    await job_queue.submit_job(job_id, payload, [model])
    return job_id


async def stream_passthrough(job_id: str):
    """Relay raw upstream SSE events verbatim to the client."""
    async for data in token_stream.subscribe_tokens(job_id):
        if data.get("text") == token_stream.DONE_SENTINEL:
            err = data.get("error")
            if err:
                body = json.dumps({"type": "error", "error": {"type": "api_error", "message": err}})
                yield f"event: error\ndata: {body}\n\n"
            return
        if data.get("raw"):
            ev = data.get("event")
            d = data.get("data", "")
            # Anthropic + Responses both use named SSE events; relay the name
            # when present so the client's SDK dispatches correctly.
            if ev:
                yield f"event: {ev}\ndata: {d}\n\n"
            else:
                yield f"data: {d}\n\n"


async def collect_passthrough(job_id: str) -> dict:
    """Return the complete upstream JSON body for a non-streaming request."""
    async for data in token_stream.subscribe_tokens(job_id):
        if data.get("text") == token_stream.DONE_SENTINEL:
            err = data.get("error")
            if err:
                raise HTTPException(status_code=data.get("code") or 502, detail=err)
            return data.get("full_json") or {}
    raise HTTPException(status_code=504, detail="No response from worker")

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI-compatible /v1/chat/completions and /v1/models endpoints.

Tokens flow: Worker → WebSocket → Redis Pub/Sub → SSE → Client
The only difference from the Anthropic endpoint is the JSON envelope.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..ratelimit import limiter
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import extract_api_key, hash_api_key

from .. import format as fmt
from ..database import new_session, processing_gens_table, users_table, waiting_prompts_table
from ..models.openai import ChatCompletionRequest, ModelInfo, ModelListResponse
from ..services import accounts as accounts_svc
from ..services import job_queue, quota, token_stream
from ..services.sanitizer import sanitize_messages
from .worker_ws import get_available_models

logger = logging.getLogger("grid_api.openai")

router = APIRouter()


def _messages_to_prompt(messages: list) -> str:
    """Convert OpenAI chat messages to a single prompt string."""
    parts = []
    for msg in messages:
        role = msg.role
        content = msg.content
        if role == "system":
            parts.append(f"{content}\n")
        elif role == "user":
            parts.append(f"User: {content}\n")
        elif role == "assistant":
            parts.append(f"Assistant: {content}\n")
    parts.append("Assistant:")
    return "".join(parts)


@router.post("/v1/chat/completions")
@limiter.limit("30/minute")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    apikey: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """OpenAI-compatible chat completions with real streaming."""
    try:
        key = extract_api_key(apikey, authorization)
        return await _handle_chat_completions(body, key)
    except HTTPException:
        raise
    except Exception as e:
        # Log the full detail server-side; return a generic message so we
        # never leak internal paths / SQL / stack details to the public.
        logger.error(f"chat_completions error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error while processing the request.")


async def _handle_chat_completions(request: ChatCompletionRequest, apikey: str):
    # Auth — v2 account keys first, legacy Haidra keys as fallback.
    user = await accounts_svc.authenticate(apikey)

    # Check for available text workers
    available = await get_available_models(job_type="text")
    if not available:
        raise HTTPException(
            status_code=503,
            detail="No streaming workers online. Use /api/v2/generate/text/async for the legacy queue.",
        )

    # Resolve model. Never silently substitute — a client asking for
    # llama-70b must not receive output from whatever random model happens
    # to be online, labeled as the one they asked for. Return a clear
    # model-not-available error listing what IS online.
    model = request.model
    if model not in available:
        raise HTTPException(
            status_code=404,
            detail=f"Model '{request.model}' is not available. Online models: {available}",
        )

    # Free-tier daily quota. Checked here (after auth + worker availability)
    # so a user only spends quota on a request that's actually going to queue.
    await quota.check_and_consume(dict(user))

    # Sanitize messages — strip credentials before they reach workers
    clean_messages, was_redacted, redacted_types = sanitize_messages(
        [m.model_dump() for m in request.messages]
    )

    # Convert messages to prompt
    from ..models.openai import ChatMessage
    prompt = _messages_to_prompt([ChatMessage(**m) for m in clean_messages])

    # Create job
    job_id = str(uuid4())
    payload = {
        "prompt": prompt,
        "max_length": request.max_tokens or 4096,
        "temperature": request.temperature,
        "top_p": request.top_p,
    }

    # Legacy bookkeeping rows only exist for legacy (Haidra) keys — v2
    # account ids don't fit the integer FK, and nothing v2 reads these.
    legacy_rows = user["source"] == "legacy"
    if legacy_rows:
      async with await new_session() as session:
        await session.execute(
            sa.insert(waiting_prompts_table).values(
                id=job_id,
                wp_type="text",
                user_id=user["id"],
                prompt=prompt,
                params=payload,
                gen_payload=payload,
                n=1,
                jobs=1,
                things=0,
                total_usage=0,
                job_ttl=150,
                disable_batching=False,
                worker_blacklist=False,
                active=True,
                faulted=False,
                max_length=request.max_tokens or 4096,
                max_context_length=2048,
                expiry=datetime.utcnow() + timedelta(minutes=5),
                created=datetime.utcnow(),
                kudos=0,
                consumed_kudos=0,
                extra_priority=user.get("kudos", 0),
                nsfw=False,
                slow_workers=True,
                trusted_workers=False,
                ipaddr="0.0.0.0",
                safe_ip=True,
                client_agent="grid-api/1.0",
            )
        )
        # processing_gen is created by the worker WebSocket handler
        # when it picks up the job (needs real worker_id for FK constraint)
        await session.commit()

    # Submit to Redis Stream for workers. _legacy_rows tells the WS handler
    # whether the horde bookkeeping rows exist for this job.
    payload["_legacy_rows"] = legacy_rows
    await job_queue.submit_job(job_id, payload, [model])

    completion_id = fmt._gen_id()

    if request.stream:
        return StreamingResponse(
            _stream_openai(job_id, model, completion_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        return await _collect_response(job_id, model)


async def _stream_openai(job_id: str, model: str, completion_id: str):
    """SSE generator for OpenAI streaming format."""
    # First chunk: role
    chunk = fmt.openai_chunk("", model, completion_id, is_first=True)
    yield f"data: {json.dumps(chunk)}\n\n"

    # Stream tokens from Redis Pub/Sub
    token_count = 0
    async for data in token_stream.subscribe_tokens(job_id):
        text = data.get("text", "")
        if text == token_stream.DONE_SENTINEL:
            # Final chunk with finish_reason
            chunk = fmt.openai_chunk("", model, completion_id, is_last=True)
            yield f"data: {json.dumps(chunk)}\n\n"
            break
        token_count += 1
        # Reasoning tokens go in delta.reasoning_content; answer tokens in delta.content.
        if data.get("reasoning"):
            chunk = fmt.openai_chunk("", model, completion_id, reasoning=text)
        else:
            chunk = fmt.openai_chunk(text, model, completion_id)
        yield f"data: {json.dumps(chunk)}\n\n"

    yield "data: [DONE]\n\n"


async def _collect_response(job_id: str, model: str) -> dict:
    """Collect all tokens and return a single non-streaming response."""
    full_text = ""
    full_reasoning = ""
    token_count = 0
    async for data in token_stream.subscribe_tokens(job_id):
        text = data.get("text", "")
        if text == token_stream.DONE_SENTINEL:
            full_text = data.get("full_text", full_text)
            full_reasoning = data.get("full_reasoning", full_reasoning)
            break
        if data.get("reasoning"):
            full_reasoning += text
        else:
            full_text += text
        token_count += 1

    return fmt.openai_response(full_text, model, completion_tokens=token_count, reasoning=full_reasoning)


@router.get("/v1/models")
async def list_models():
    """List TEXT models available from connected workers.

    This is the OpenAI chat-completions model list (what UI model pickers read),
    so it returns text models only — image/video models are served via the media
    job API, not chat-completions, and must not appear in a chat picker.
    """
    models = await get_available_models(job_type="text")
    return ModelListResponse(
        data=[ModelInfo(id=m, owned_by="aipowergrid") for m in models],
    )


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    """Get info for a specific text model."""
    models = await get_available_models(job_type="text")
    if model_id not in models:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    return ModelInfo(id=model_id, owned_by="aipowergrid")

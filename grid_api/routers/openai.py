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
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import extract_api_key, hash_api_key

from .. import format as fmt
from ..database import new_session, processing_gens_table, users_table, waiting_prompts_table
from ..models.openai import ChatCompletionRequest, ModelInfo, ModelListResponse
from ..services import job_queue, token_stream
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
        logger.error(f"chat_completions error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _handle_chat_completions(request: ChatCompletionRequest, apikey: str):
    # Auth
    hashed = hash_api_key(apikey)
    async with await new_session() as session:
        result = await session.execute(
            sa.select(users_table).where(users_table.c.api_key == hashed)
        )
        user = result.mappings().first()
        if not user:
            raise HTTPException(status_code=401, detail="Invalid API key")

    # Check for available workers
    available = await get_available_models()
    if not available:
        raise HTTPException(
            status_code=503,
            detail="No streaming workers online. Use /api/v2/generate/text/async for the legacy queue.",
        )

    # Resolve model — use requested or first available
    model = request.model
    if model not in available:
        if available:
            model = available[0]
        else:
            raise HTTPException(status_code=400, detail=f"Model '{request.model}' not available. Available: {available}")

    # Convert messages to prompt
    prompt = _messages_to_prompt(request.messages)

    # Create job
    job_id = str(uuid4())
    payload = {
        "prompt": prompt,
        "max_length": request.max_tokens or 512,
        "temperature": request.temperature,
        "top_p": request.top_p,
    }

    # Write waiting prompt + processing gen to DB
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
                max_length=request.max_tokens or 512,
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

    # Submit to Redis Stream for workers
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
        chunk = fmt.openai_chunk(text, model, completion_id)
        yield f"data: {json.dumps(chunk)}\n\n"

    yield "data: [DONE]\n\n"


async def _collect_response(job_id: str, model: str) -> dict:
    """Collect all tokens and return a single non-streaming response."""
    full_text = ""
    token_count = 0
    async for data in token_stream.subscribe_tokens(job_id):
        text = data.get("text", "")
        if text == token_stream.DONE_SENTINEL:
            full_text = data.get("full_text", full_text)
            break
        full_text += text
        token_count += 1

    return fmt.openai_response(full_text, model, completion_tokens=token_count)


@router.get("/v1/models")
async def list_models():
    """List models available from connected streaming workers."""
    models = await get_available_models()
    return ModelListResponse(
        data=[ModelInfo(id=m, owned_by="aipowergrid") for m in models],
    )


@router.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    """Get info for a specific model."""
    models = await get_available_models()
    if model_id not in models:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    return ModelInfo(id=model_id, owned_by="aipowergrid")

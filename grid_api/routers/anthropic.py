# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Anthropic-compatible /v1/messages endpoint.

Same streaming infrastructure as OpenAI — just a different SSE envelope.
Workers don't know or care which format the client requested.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Literal, Optional
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..ratelimit import limiter

from ..auth import hash_api_key

from .. import format as fmt
from ..database import new_session, processing_gens_table, users_table, waiting_prompts_table
from ..services import job_queue, quota, token_stream
from ..services.sanitizer import sanitize
from .worker_ws import get_available_models

logger = logging.getLogger("grid_api.anthropic")

router = APIRouter()


# ── Anthropic request models ──


class AnthropicMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class MessagesRequest(BaseModel):
    model: str
    messages: list[AnthropicMessage]
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    temperature: float = Field(default=1.0, ge=0, le=1)
    top_p: Optional[float] = None
    stream: bool = False
    system: Optional[str] = None


def _messages_to_prompt(request: MessagesRequest) -> str:
    """Convert Anthropic messages to a prompt string."""
    parts = []
    if request.system:
        parts.append(f"{request.system}\n")
    for msg in request.messages:
        if msg.role == "user":
            parts.append(f"User: {msg.content}\n")
        elif msg.role == "assistant":
            parts.append(f"Assistant: {msg.content}\n")
    parts.append("Assistant:")
    return "".join(parts)


@router.post("/v1/messages")
@limiter.limit("30/minute")
async def create_message(
    request: Request,
    body: MessagesRequest,
    x_api_key: str = Header(..., alias="x-api-key", description="API key"),
):
    """Anthropic-compatible messages endpoint with streaming."""

    # Auth
    hashed = hash_api_key(x_api_key)
    async with await new_session() as session:
        result = await session.execute(
            sa.select(users_table).where(users_table.c.api_key == hashed)
        )
        user = result.mappings().first()
    if not user:
        raise HTTPException(status_code=401, detail={"type": "authentication_error", "message": "Invalid API key"})

    available = await get_available_models()
    if not available:
        raise HTTPException(status_code=503, detail={"type": "overloaded_error", "message": "No streaming workers online"})

    # Never silently substitute a different model than the one requested.
    if body.model not in available:
        raise HTTPException(
            status_code=404,
            detail={
                "type": "not_found_error",
                "message": f"Model '{body.model}' is not available. Online models: {available}",
            },
        )

    # Free-tier daily quota (paid/contributor users pass through).
    await quota.check_and_consume(dict(user))

    model = body.model
    raw_prompt = _messages_to_prompt(body)

    # Sanitize — strip credentials before they reach workers
    sanitized = sanitize(raw_prompt)
    prompt = sanitized.text

    job_id = str(uuid4())
    payload = {
        "prompt": prompt,
        "max_length": body.max_tokens,
        "temperature": body.temperature,
        "top_p": body.top_p or 0.9,
    }

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
                max_length=body.max_tokens,
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

    await job_queue.submit_job(job_id, payload, [model])

    message_id = fmt._gen_id("msg")

    if body.stream:
        return StreamingResponse(
            _stream_anthropic(job_id, model, message_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        return await _collect_response(job_id, model)


async def _stream_anthropic(job_id: str, model: str, message_id: str):
    """SSE generator for Anthropic streaming format."""
    # message_start
    yield f"event: message_start\ndata: {json.dumps(fmt.anthropic_message_start(model, message_id))}\n\n"

    # content_block_start
    yield f"event: content_block_start\ndata: {json.dumps(fmt.anthropic_content_block_start())}\n\n"

    # Stream tokens
    yield "event: ping\ndata: {}\n\n"

    token_count = 0
    async for data in token_stream.subscribe_tokens(job_id):
        text = data.get("text", "")
        if text == token_stream.DONE_SENTINEL:
            break
        token_count += 1
        delta = fmt.anthropic_content_block_delta(text)
        yield f"event: content_block_delta\ndata: {json.dumps(delta)}\n\n"

    # content_block_stop
    yield f"event: content_block_stop\ndata: {json.dumps(fmt.anthropic_content_block_stop())}\n\n"

    # message_delta
    yield f"event: message_delta\ndata: {json.dumps(fmt.anthropic_message_delta(token_count))}\n\n"

    # message_stop
    yield f"event: message_stop\ndata: {json.dumps(fmt.anthropic_message_stop())}\n\n"


async def _collect_response(job_id: str, model: str) -> dict:
    """Collect all tokens and return non-streaming Anthropic response."""
    full_text = ""
    token_count = 0
    async for data in token_stream.subscribe_tokens(job_id):
        text = data.get("text", "")
        if text == token_stream.DONE_SENTINEL:
            full_text = data.get("full_text", full_text)
            break
        full_text += text
        token_count += 1

    return fmt.anthropic_response(full_text, model, output_tokens=token_count)

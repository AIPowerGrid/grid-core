# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI-compatible /v1/chat/completions and /v1/models endpoints.

Tokens flow: Worker → WebSocket → Redis Pub/Sub → SSE → Client
The only difference from the Anthropic endpoint is the JSON envelope.
"""

import asyncio
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

# DoS guard: cap total request size BEFORE sanitize-regex + tiktoken + Redis push, so a
# multi-MB prompt can't amplify CPU/memory (sec audit M3). Generous but bounded.
MAX_REQUEST_CHARS = int(os.getenv("MAX_REQUEST_CHARS", "200000"))   # ~50k tokens
MAX_REQUEST_MESSAGES = int(os.getenv("MAX_REQUEST_MESSAGES", "500"))

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..ratelimit import limiter

from ..auth import extract_api_key

from .. import format as fmt
from ..database import new_session, processing_gens_table, waiting_prompts_table
from ..models.openai import ChatCompletionRequest, ModelInfo, ModelListResponse
from ..services import accounts as accounts_svc
from ..services import credits, job_queue, media, quota, recipes, token_stream
from ..services.sanitizer import sanitize_messages
from .worker_ws import get_available_models

logger = logging.getLogger("grid_api.openai")


async def _meter_charge(user, model, prompt_tokens, completion_tokens, job_id):
    """Meter one completion against the consumer's credit balance.

    OFF by default (GRID_CHARGING_ENABLED=0): charge_request only LOGS what it
    *would* bill and never debits or blocks — so we can observe pricing against
    real traffic before flipping charging on. Billing must NEVER break a
    response, so all errors are swallowed."""
    try:
        await credits.charge_request(
            user, model, int(prompt_tokens or 0), int(completion_tokens or 0), job_id
        )
    except Exception:
        logger.debug("charge_request failed (non-fatal)", exc_info=True)

router = APIRouter()


def _content_to_text(content) -> str:
    """Flatten a message's content to plain text.

    Content may be a string, None (tool-only assistant turn), or a list of
    multimodal parts; we keep only the text so the legacy `prompt` string and
    the ledger prompt-hash stay well-defined. The faithful structured request
    (with images, tools, roles intact) is carried separately in payload.request.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return str(content)


def _messages_to_prompt(messages: list[dict]) -> str:
    """Convert OpenAI chat messages to a single prompt string.

    Used ONLY for legacy bookkeeping, the ledger prompt-hash, and as a fallback
    payload for pre-passthrough workers — never as the primary request anymore.
    """
    parts = []
    for msg in messages:
        role = msg.get("role")
        text = _content_to_text(msg.get("content"))
        if role in ("system", "developer"):
            parts.append(f"{text}\n")
        elif role == "user":
            parts.append(f"User: {text}\n")
        elif role == "assistant":
            parts.append(f"Assistant: {text}\n")
        elif role == "tool":
            parts.append(f"Tool: {text}\n")
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


async def _detect_media_model(model: str) -> Optional[str]:
    """Return 'image'/'video' if `model` is a media model, else None.

    The recipe's jobType is AUTHORITATIVE for kind: a media worker may advertise the
    same model under both image+video job-types, so the worker list can't tell us
    which a model actually is (LTX-2.3 is video-only but shows in both). Fall back to
    the worker-advertised job-type only for non-recipe (legacy) models."""
    r = recipes.get_recipe(model)
    if r is not None and r.job_type in ("image", "video"):
        return r.job_type
    if model in await get_available_models(job_type="image"):
        return "image"
    if model in await get_available_models(job_type="video"):
        return "video"
    return None


def _last_user_prompt(messages: list) -> str:
    """Extract the most recent user message as a plain-text prompt."""
    for m in reversed(messages):
        if m.role == "user":
            text = _content_to_text(m.content)
            if text.strip():
                return text
    # Fall back to whatever text we can find.
    return " ".join(_content_to_text(m.content) for m in messages).strip()


def _last_user_image(messages: list) -> Optional[str]:
    """Most recent INLINE (data: URI) image in a user turn — the img2img/img2video
    source frame. http(s) URLs are ignored on purpose (SSRF: inline base64 only)."""
    for m in reversed(messages):
        if m.role != "user" or not isinstance(m.content, list):
            continue
        for part in m.content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if isinstance(url, str) and url.startswith("data:"):
                    return url
    return None


async def _chat_media(request: ChatCompletionRequest, kind: str, account_id=None):
    """Image/video generation abstracted behind /v1/chat/completions.

    When the requested model is a media model, the latest user turn is the
    prompt; we run a media job and return the result inside the assistant
    message as markdown (renders in any chat UI) plus a structured `images`
    array (OpenRouter-style) for programmatic clients. The dedicated
    /v1/images|videos endpoints remain for advanced control (size, seed, n…).
    """
    model = request.model
    prompt = _last_user_prompt(request.messages)

    # img2img / img2video from a pasted image in the turn. Chat has no `size`, so a
    # source frame always auto-matches the output dims to it. An image-only turn
    # (no text) is valid for img2video.
    source_image = _last_user_image(request.messages)
    if not prompt and not source_image:
        raise HTTPException(status_code=400, detail="No prompt or image found in messages.")

    recipe_inputs: dict = {}
    source_image_url = None
    if source_image:
        recipe_inputs, source_image_url = await media.prepare_source_image(
            model, source_image, size_was_set=False)

    steps, cfg_scale, sampler = media.diffusion_params(model, {})
    if kind == "video":
        width, height = 768, 512
        payload = {
            "prompt": prompt, "n": 1, "width": width, "height": height,
            "frames": 4 * 24, "fps": 24, "steps": steps, "sampler_name": sampler,
            "cfg_scale": cfg_scale, "ext": "mp4",
        }
        timeout = media.VIDEO_TIMEOUT
    else:
        width, height = 1024, 1024
        payload = {
            "prompt": prompt, "n": 1, "width": width, "height": height,
            "steps": steps, "sampler_name": sampler, "cfg_scale": cfg_scale, "ext": "webp",
        }
        timeout = media.IMAGE_TIMEOUT
    if recipe_inputs:
        payload["recipe_inputs"] = recipe_inputs
    if source_image_url:
        payload["source_image_url"] = source_image_url
    outputs, _meta = await media.submit_and_wait(model, kind, payload, timeout,
                                          account_id=account_id, concurrency_limit=media.MEDIA_CONCURRENCY)

    urls = [o["url"] for o in outputs if o.get("url")]
    if kind == "video":
        markdown = "\n".join(f"[video]({u})" for u in urls)
        images = []
        videos = [{"type": "video_url", "video_url": {"url": u}} for u in urls]
    else:
        markdown = "\n".join(f"![{prompt}]({u})" for u in urls)
        images = [{"type": "image_url", "image_url": {"url": u}} for u in urls]
        videos = []

    completion_id = fmt._gen_id()

    if request.stream:
        async def _gen():
            yield f"data: {json.dumps(fmt.openai_chunk('', model, completion_id, is_first=True))}\n\n"
            yield f"data: {json.dumps(fmt.openai_chunk(markdown, model, completion_id))}\n\n"
            yield f"data: {json.dumps(fmt.openai_chunk_raw({}, model, completion_id, finish_reason='stop'))}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    message = {"role": "assistant", "content": markdown}
    if images:
        message["images"] = images
    if videos:
        message["videos"] = videos
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _assert_request_size(messages: list) -> None:
    """Reject oversized requests before any CPU-heavy processing (sanitize/tokenize)."""
    if len(messages) > MAX_REQUEST_MESSAGES:
        raise HTTPException(status_code=413, detail=f"too many messages (max {MAX_REQUEST_MESSAGES})")
    total = sum(len(_content_to_text(m.content)) for m in messages)
    if total > MAX_REQUEST_CHARS:
        raise HTTPException(status_code=413,
                            detail=f"request too large ({total} chars; max {MAX_REQUEST_CHARS})")


async def _handle_chat_completions(request: ChatCompletionRequest, apikey: str):
    # Auth — v2 account keys first, legacy Haidra keys as fallback.
    user = await accounts_svc.authenticate(apikey)
    _assert_request_size(request.messages)

    # Media abstraction: if the requested model is an image/video model, run a
    # media job and return the asset in the assistant message. Keeps a single
    # /v1/chat/completions surface for "give me a picture of…" while the
    # dedicated /v1/images|videos endpoints stay for advanced control.
    media_kind = await _detect_media_model(request.model)
    if media_kind:
        await quota.check_and_consume(dict(user))
        return await _chat_media(request, media_kind, account_id=user["id"])

    # Check for available text workers serving the OpenAI chat-completions API.
    available = await get_available_models(job_type="text", api_format="openai-chat")
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

    # Sanitize messages — strip credentials before they reach workers.
    # We can read+scrub here because this is the OBSERVE-mode path (the grid is
    # a transparent-but-watching proxy). The future TEE/confidential path will
    # be a blind relay where this step moves client-side.
    clean_messages, was_redacted, redacted_types = sanitize_messages(
        [m.model_dump(exclude_none=True) for m in request.messages]
    )

    # Flattened prompt — legacy bookkeeping, ledger prompt-hash, and the
    # fallback payload for pre-passthrough workers.
    prompt = _messages_to_prompt(clean_messages)

    # Seed: grid-side randomization. If the caller didn't pin a seed, mint a
    # fresh one here so output varies per request regardless of the backend
    # engine's default RNG behavior (don't rely on each heterogeneous worker's
    # engine to randomize) — and echo it back for reproducibility. A
    # client-supplied seed is always honored. Mirrors the media path.
    if request.seed is None:
        request.seed = secrets.randbelow(2**53)

    # Faithful request: forward the developer's request as-is (tools,
    # tool_choice, multimodal content, seed, response_format, any extra params)
    # with only the sanitized messages swapped in. The worker overrides `model`
    # to its backend's name and forces streaming; everything else passes through
    # untouched so a model behaves on the grid exactly as it does locally.
    request_body = request.model_dump(exclude_none=True)
    request_body["messages"] = clean_messages

    # Create job
    job_id = str(uuid4())
    payload = {
        "request": request_body,
        "api_format": "openai-chat",
        # Legacy/fallback fields (also read by the den/context caps).
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
            _stream_openai(job_id, model, completion_id, user, request.seed),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        return await _collect_response(job_id, model, user, request.seed)


async def _stream_openai(job_id: str, model: str, completion_id: str, user: dict | None = None, seed: int | None = None):
    """SSE generator for OpenAI streaming format.

    Faithful passthrough: the grid emits one leading role chunk, then relays
    each backend delta verbatim (content / reasoning_content / tool_calls), and
    finally a finish_reason chunk + an optional usage chunk. The grid stamps
    only id/model/created — it never rewrites the delta payload.
    """
    # First chunk: role (the grid's single authoritative role delta).
    chunk = fmt.openai_chunk("", model, completion_id, is_first=True)
    yield f"data: {json.dumps(chunk)}\n\n"

    async for data in token_stream.subscribe_tokens(job_id):
        if data.get("text") == token_stream.DONE_SENTINEL:
            # A mid-stream error (bad request, worker/backend failure): surface
            # it as an OpenAI error event so the client sees the real reason
            # instead of a silently-truncated reply.
            err = data.get("error")
            if err:
                yield f"data: {json.dumps({'error': {'message': err, 'type': 'invalid_request_error' if data.get('code') == 400 else 'upstream_error'}})}\n\n"
                yield "data: [DONE]\n\n"
                return
            # Terminal finish_reason chunk, then optional usage chunk.
            finish = data.get("finish_reason") or "stop"
            yield f"data: {json.dumps(fmt.openai_chunk_raw({}, model, completion_id, finish_reason=finish))}\n\n"
            usage = data.get("usage")
            grid_meta = data.get("grid")
            if seed is not None:
                grid_meta = {**(grid_meta or {}), "seed": seed}
            if usage or grid_meta:
                usage_chunk = fmt.openai_usage_chunk(model, completion_id, usage or {})
                # Additive provenance on the final chunk (worker, gen_time, ttft,
                # tokens_per_s) — standard clients ignore the extra `grid` key.
                if grid_meta:
                    usage_chunk["grid"] = grid_meta
                yield f"data: {json.dumps(usage_chunk)}\n\n"
            u = usage or {}
            await _meter_charge(
                user, model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), job_id
            )
            break

        delta = data.get("delta")
        if delta is not None:
            # Faithful path — relay the raw backend delta untouched.
            chunk = fmt.openai_chunk_raw(delta, model, completion_id, finish_reason=data.get("finish_reason"))
        elif data.get("reasoning"):
            # Legacy worker path — reasoning channel.
            chunk = fmt.openai_chunk("", model, completion_id, reasoning=data.get("text", ""))
        else:
            # Legacy worker path — plain content.
            chunk = fmt.openai_chunk(data.get("text", ""), model, completion_id)
        yield f"data: {json.dumps(chunk)}\n\n"

    yield "data: [DONE]\n\n"


async def _collect_response(job_id: str, model: str, user: dict | None = None, seed: int | None = None) -> dict:
    """Collect the stream and return a single non-streaming response.

    The worker always streams; the grid assembles. The DONE event carries the
    already-assembled content / reasoning / tool_calls / usage (assembled once,
    server-side, in worker_ws). We also accumulate from deltas as a fallback so
    a legacy worker that never sends a rich DONE still produces a valid reply.
    """
    content = ""
    reasoning = ""
    tool_calls = None
    usage = None
    finish_reason = "stop"
    grid_meta = None

    async for data in token_stream.subscribe_tokens(job_id):
        if data.get("text") == token_stream.DONE_SENTINEL:
            err = data.get("error")
            if err:
                # Surface the real failure with a meaningful status (400 for a
                # caller fault, 502 for an upstream worker/backend failure).
                raise HTTPException(status_code=data.get("code") or 502, detail=err)
            content = data.get("full_text") or content
            reasoning = data.get("full_reasoning") or reasoning
            tool_calls = data.get("tool_calls") or tool_calls
            usage = data.get("usage") or usage
            finish_reason = data.get("finish_reason") or finish_reason
            grid_meta = data.get("grid") or grid_meta
            break

        delta = data.get("delta")
        if delta is not None:
            if delta.get("content"):
                content += delta["content"]
            if delta.get("reasoning_content"):
                reasoning += delta["reasoning_content"]
        elif data.get("reasoning"):
            reasoning += data.get("text", "")
        else:
            content += data.get("text", "")

    prompt_tokens = (usage or {}).get("prompt_tokens", 0)
    completion_tokens = (usage or {}).get("completion_tokens", 0)
    await _meter_charge(user, model, prompt_tokens, completion_tokens, job_id)
    resp = fmt.openai_response(
        content,
        model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning=reasoning,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
    )
    # Additive provenance sibling (worker, gen_time, ttft, tokens_per_s). Standard
    # OpenAI clients ignore unknown top-level fields; UIs that want it read `grid`.
    if seed is not None:
        grid_meta = {**(grid_meta or {}), "seed": seed}
    if grid_meta:
        resp["grid"] = grid_meta
    return resp


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

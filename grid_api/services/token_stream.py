# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Token streaming via Redis Pub/Sub + buffered replay.

Workers publish tokens as they generate. Clients subscribe to receive
them in real time. A Redis list buffers tokens so clients that connect
slightly late can replay missed tokens before switching to live pub/sub.

Channel: `grid:stream:{job_id}`
Buffer:  `grid:tokens:{job_id}` (list, TTL 5 min)
"""

import asyncio
import json
import logging
from typing import AsyncGenerator

import redis.exceptions

from ..redis_client import get_redis

logger = logging.getLogger("grid_api.token_stream")

CHANNEL_PREFIX = "grid:stream:"
BUFFER_PREFIX = "grid:tokens:"
DONE_SENTINEL = "[DONE]"
BUFFER_TTL = 300  # 5 minutes


def event_content_text(data: dict) -> str:
    """Extract plain *answer* text from a stream event, regardless of protocol.

    Faithful passthrough events carry text in `data['delta']['content']`;
    legacy events carry it in `data['text']` (unless flagged reasoning, which
    is not answer text). Format adapters that render plain text only — like the
    current Anthropic text block — use this so they work with either worker."""
    delta = data.get("delta")
    if delta is not None:
        return delta.get("content") or ""
    if data.get("reasoning"):
        return ""
    return data.get("text", "")


async def publish_token(
    job_id: str,
    text: str = "",
    reasoning: bool = False,
    delta: dict | None = None,
    finish_reason: str | None = None,
):
    """Publish one stream event to pub/sub AND append to the replay buffer.

    Two shapes flow through here:

    * Faithful passthrough (text generation): `delta` carries the inference
      backend's raw `choices[0].delta` (content / reasoning_content /
      tool_calls). The SSE layer re-wraps it untouched. This is the path that
      makes the grid a transparent OpenAI proxy.
    * Legacy / media: positional `text` (+ `reasoning` flag) — kept so old
      workers and the media progress channel keep working unchanged.

    Every event keeps a `text` key (default "") so the DONE-sentinel check in
    `subscribe_tokens` never KeyErrors on a delta-only event."""
    r = get_redis()
    channel = f"{CHANNEL_PREFIX}{job_id}"
    buf_key = f"{BUFFER_PREFIX}{job_id}"
    event = {"text": text, "reasoning": reasoning}
    if delta is not None:
        event["delta"] = delta
    if finish_reason is not None:
        event["finish_reason"] = finish_reason
    data = json.dumps(event)

    pipe = r.pipeline()
    pipe.publish(channel, data)
    pipe.rpush(buf_key, data)
    pipe.expire(buf_key, BUFFER_TTL)
    await pipe.execute()


async def publish_raw_event(job_id: str, event: str | None, data: str):
    """Relay ONE raw upstream SSE event verbatim (Anthropic / OpenAI-Responses
    passthrough).

    For natively-served formats the grid is a faithful tunnel: the worker reads
    the backend's SSE `event:`/`data:` lines and we forward them byte-for-byte to
    the client, only teeing usage for metering. `event` is the SSE event name
    (may be None for data-only streams); `data` is the raw JSON string."""
    r = get_redis()
    channel = f"{CHANNEL_PREFIX}{job_id}"
    buf_key = f"{BUFFER_PREFIX}{job_id}"
    payload = json.dumps({"text": "", "raw": True, "event": event, "data": data})

    pipe = r.pipeline()
    pipe.publish(channel, payload)
    pipe.rpush(buf_key, payload)
    pipe.expire(buf_key, BUFFER_TTL)
    await pipe.execute()


async def publish_done(
    job_id: str,
    full_text: str = "",
    full_reasoning: str = "",
    tool_calls: list | None = None,
    usage: dict | None = None,
    finish_reason: str = "stop",
    full_json: dict | None = None,
    grid: dict | None = None,
):
    """Signal generation complete via pub/sub + buffer.

    Carries the fully-assembled result (content + reasoning + tool_calls) and
    authoritative `usage` so the non-streaming collector can build the final
    message and the streaming layer can emit a trailing usage chunk.

    `grid` is optional provenance metadata (worker, gen_time, ttft, tokens_per_s)
    the API surfaces alongside the response — additive, never alters output."""
    r = get_redis()
    channel = f"{CHANNEL_PREFIX}{job_id}"
    buf_key = f"{BUFFER_PREFIX}{job_id}"
    data = json.dumps({
        "text": DONE_SENTINEL,
        "full_text": full_text,
        "full_reasoning": full_reasoning,
        "tool_calls": tool_calls,
        "usage": usage,
        "finish_reason": finish_reason,
        # For non-streaming raw passthrough (Anthropic/Responses): the complete
        # upstream JSON body, returned to the client unchanged.
        "full_json": full_json,
        "grid": grid,
    })

    pipe = r.pipeline()
    pipe.publish(channel, data)
    pipe.rpush(buf_key, data)
    pipe.expire(buf_key, BUFFER_TTL)
    await pipe.execute()


async def publish_error(job_id: str, message: str, code: int = 502):
    """Signal an error on the stream (worker disconnected, timeout, bad request).

    `code` is the HTTP status the client should ultimately see — 502 for
    worker/backend failures (the default), 400 for a deterministic caller fault
    surfaced from the backend."""
    r = get_redis()
    channel = f"{CHANNEL_PREFIX}{job_id}"
    buf_key = f"{BUFFER_PREFIX}{job_id}"
    data = json.dumps({"text": DONE_SENTINEL, "error": message, "code": code, "full_text": ""})

    pipe = r.pipeline()
    pipe.publish(channel, data)
    pipe.rpush(buf_key, data)
    pipe.expire(buf_key, BUFFER_TTL)
    await pipe.execute()


async def subscribe_tokens(job_id: str, timeout: int = 300) -> AsyncGenerator[dict, None]:
    """Subscribe to a job's token stream with replay support.

    1. First replays any buffered tokens (catches tokens published before we subscribed)
    2. Then switches to live pub/sub for new tokens
    3. Deduplicates so tokens aren't yielded twice

    Yields dicts with 'text' key until DONE_SENTINEL or timeout.
    """
    r = get_redis()
    buf_key = f"{BUFFER_PREFIX}{job_id}"
    channel = f"{CHANNEL_PREFIX}{job_id}"

    # Subscribe first so we don't miss anything published after buffer read
    pubsub = r.pubsub()
    await pubsub.subscribe(channel)

    seen_count = 0
    done = False

    try:
        # Phase 1: Replay buffered tokens
        buffered = await r.lrange(buf_key, 0, -1)
        for raw in buffered:
            data = json.loads(raw)
            seen_count += 1
            if data["text"] == DONE_SENTINEL:
                yield data
                done = True
                break
            yield data

        if done:
            return

        # Phase 2: Live pub/sub (skip tokens we already replayed)
        live_count = 0
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break

            # A Redis socket read-timeout here is benign — it just means no
            # message arrived in the window (the blocking read raced its own
            # timeout). Treat it as "no message" and keep polling until the
            # real deadline, rather than letting it bubble up and end the
            # stream with an empty reply.
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                    timeout=min(remaining, 5.0),
                )
            except (asyncio.TimeoutError, redis.exceptions.TimeoutError):
                message = None

            if message is None:
                # Check buffer for tokens that arrived via replay race
                new_buffered = await r.lrange(buf_key, seen_count + live_count, -1)
                for raw in new_buffered:
                    data = json.loads(raw)
                    live_count += 1
                    if data["text"] == DONE_SENTINEL:
                        yield data
                        return
                    yield data
                continue

            if message["type"] == "message":
                live_count += 1
                # Skip tokens we already yielded from the buffer
                if live_count <= seen_count:
                    continue
                data = json.loads(message["data"])
                if data["text"] == DONE_SENTINEL:
                    yield data
                    break
                yield data

    except asyncio.TimeoutError:
        logger.warning(f"Token stream timeout for job {job_id}")
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()

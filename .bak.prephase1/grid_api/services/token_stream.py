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


async def publish_token(job_id: str, text: str, reasoning: bool = False):
    """Publish a token to pub/sub AND append to the replay buffer.

    `reasoning=True` marks a chain-of-thought token so the SSE layer emits it in
    `delta.reasoning_content` rather than `delta.content` (faithful passthrough)."""
    r = get_redis()
    channel = f"{CHANNEL_PREFIX}{job_id}"
    buf_key = f"{BUFFER_PREFIX}{job_id}"
    data = json.dumps({"text": text, "reasoning": reasoning})

    pipe = r.pipeline()
    pipe.publish(channel, data)
    pipe.rpush(buf_key, data)
    pipe.expire(buf_key, BUFFER_TTL)
    await pipe.execute()


async def publish_done(job_id: str, full_text: str, full_reasoning: str = ""):
    """Signal generation complete via pub/sub + buffer."""
    r = get_redis()
    channel = f"{CHANNEL_PREFIX}{job_id}"
    buf_key = f"{BUFFER_PREFIX}{job_id}"
    data = json.dumps({"text": DONE_SENTINEL, "full_text": full_text, "full_reasoning": full_reasoning})

    pipe = r.pipeline()
    pipe.publish(channel, data)
    pipe.rpush(buf_key, data)
    pipe.expire(buf_key, BUFFER_TTL)
    await pipe.execute()


async def publish_error(job_id: str, message: str):
    """Signal an error on the stream (worker disconnected, timeout, etc)."""
    r = get_redis()
    channel = f"{CHANNEL_PREFIX}{job_id}"
    buf_key = f"{BUFFER_PREFIX}{job_id}"
    data = json.dumps({"text": DONE_SENTINEL, "error": message, "full_text": ""})

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

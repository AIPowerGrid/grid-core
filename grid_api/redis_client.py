# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Async Redis client for Grid API streaming infrastructure.

Uses Redis DB 7 (configurable) to avoid conflicts with Flask's DB 0-5.
Provides two services:
  - Redis Streams for job dispatch (XADD / XREADGROUP)
  - Redis Pub/Sub for token streaming (PUBLISH / SUBSCRIBE)
"""

import redis.asyncio as aioredis

from .config import get_settings

_redis: aioredis.Redis | None = None

STREAM_KEY = "grid:jobs:text"
# Media (image/video) jobs ride a separate stream so the consumer group never
# hands a media job to a text worker (and vice versa) — mismatch-requeue is
# only needed for *model* mismatches within a type.
MEDIA_STREAM_KEY = "grid:jobs:media"
CONSUMER_GROUP = "grid:workers"
WORKER_ACTIVE_SET_KEY = "grid:workers:active"


async def init_redis():
    """Initialize the async Redis connection."""
    global _redis
    settings = get_settings()
    _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    # Create the consumer groups if they don't exist
    for stream in (STREAM_KEY, MEDIA_STREAM_KEY):
        try:
            await _redis.xgroup_create(stream, CONSUMER_GROUP, id="0", mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise


async def close_redis():
    """Close the Redis connection."""
    global _redis
    if _redis:
        try:
            await _redis.aclose()
        except AttributeError:
            await _redis.close()


def get_redis() -> aioredis.Redis:
    """Get the Redis client instance."""
    if _redis is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return _redis

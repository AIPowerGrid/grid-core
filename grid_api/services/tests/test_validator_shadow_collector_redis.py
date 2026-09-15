# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Production-shaped Redis consumer-group proof for the shadow collector.

Set VALIDATOR_SHADOW_TEST_REDIS_URL to a disposable Redis database. The test
uses a unique stream and never touches the Grid's ordinary job streams.
"""

from __future__ import annotations

import os
import secrets

import pytest
import redis.asyncio as aioredis

from grid_api.services import validator_shadow_collector as collector

REDIS_URL = os.environ.get("VALIDATOR_SHADOW_TEST_REDIS_URL", "")

pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="set VALIDATOR_SHADOW_TEST_REDIS_URL to a disposable Redis database",
)


@pytest.mark.asyncio
async def test_retry_remains_pending_then_is_reclaimed_and_acked(monkeypatch):
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    stream_key = f"test:grid:validator-shadow:{secrets.token_hex(8)}"
    monkeypatch.setattr(collector, "STREAM_KEY", stream_key)
    monkeypatch.setattr(collector, "CLAIM_IDLE_MS", 0)
    monkeypatch.setattr(collector, "get_redis", lambda: redis)

    attempts = 0

    async def retry_then_ack(_fields):
        nonlocal attempts
        attempts += 1
        return "retry" if attempts == 1 else "ack"

    monkeypatch.setattr(collector, "process_event", retry_then_ack)

    try:
        await collector.ensure_consumer_group()
        await redis.xadd(stream_key, {"kind": "route"})

        first = await collector.collect_once(consumer="consumer-a", block_ms=1)
        assert first == {"acked": 0, "retried": 1, "failed": 0}
        assert (await redis.xpending(stream_key, collector.CONSUMER_GROUP))["pending"] == 1

        second = await collector.collect_once(consumer="consumer-b", block_ms=1)
        assert second == {"acked": 1, "retried": 0, "failed": 0}
        assert (await redis.xpending(stream_key, collector.CONSUMER_GROUP))["pending"] == 0
        assert await redis.xlen(stream_key) == 0
        assert attempts == 2
    finally:
        await redis.delete(stream_key)
        await redis.aclose()


@pytest.mark.asyncio
async def test_invalid_event_is_reclaimed_only_after_durable_error(monkeypatch):
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    stream_key = f"test:grid:validator-shadow:{secrets.token_hex(8)}"
    monkeypatch.setattr(collector, "STREAM_KEY", stream_key)
    monkeypatch.setattr(collector, "CLAIM_IDLE_MS", 0)
    monkeypatch.setattr(collector, "get_redis", lambda: redis)
    recorded = []
    attempts = 0

    async def record_error(message_id, fields):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated database outage")
        recorded.append((message_id, fields))

    monkeypatch.setattr(collector, "_record_invalid_event", record_error)
    try:
        await collector.ensure_consumer_group()
        fields = {"kind": "route", "route_ref": "invalid"}
        message_id = await redis.xadd(stream_key, fields)
        first = await collector.collect_once(consumer="consumer-a", block_ms=1)
        assert first == {"acked": 0, "retried": 0, "failed": 1}
        assert (await redis.xpending(stream_key, collector.CONSUMER_GROUP))["pending"] == 1
        assert await redis.xlen(stream_key) == 1
        second = await collector.collect_once(consumer="consumer-b", block_ms=1)
        assert second == {"acked": 1, "retried": 0, "failed": 1}
        assert recorded == [(message_id, fields)]
        assert (await redis.xpending(stream_key, collector.CONSUMER_GROUP))["pending"] == 0
        assert await redis.xlen(stream_key) == 0
    finally:
        await redis.delete(stream_key)
        await redis.aclose()

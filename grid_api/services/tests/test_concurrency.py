# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-account in-flight concurrency cap — acquire/release, fail-open, floor."""

import pytest

from grid_api.services import concurrency


class _FakeRedis:
    def __init__(self, fail=False):
        self.store = {}
        self.fail = fail

    async def incr(self, k):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[k] = self.store.get(k, 0) + 1
        return self.store[k]

    async def decr(self, k):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[k] = self.store.get(k, 0) - 1
        return self.store[k]

    async def expire(self, k, ttl):
        return True

    async def set(self, k, v):
        self.store[k] = v


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("grid_api.redis_client.get_redis", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_caps_at_limit_then_frees_on_release(redis):
    aid = "acct"
    assert await concurrency.acquire(aid, "text", 2) is True   # 1
    assert await concurrency.acquire(aid, "text", 2) is True   # 2
    assert await concurrency.acquire(aid, "text", 2) is False  # 3 → over cap, rejected
    # the rejected acquire didn't consume a slot
    assert redis.store["grid:inflight:text:acct"] == 2
    await concurrency.release(aid, "text")                     # back to 1
    assert await concurrency.acquire(aid, "text", 2) is True   # slot freed


@pytest.mark.asyncio
async def test_kinds_are_independent(redis):
    aid = "acct"
    assert await concurrency.acquire(aid, "text", 1) is True
    assert await concurrency.acquire(aid, "text", 1) is False   # text full
    assert await concurrency.acquire(aid, "media", 1) is True   # media has its own counter


@pytest.mark.asyncio
async def test_release_floors_at_zero(redis):
    aid = "acct"
    await concurrency.release(aid, "text")  # release with nothing held
    await concurrency.release(aid, "text")
    assert redis.store["grid:inflight:text:acct"] == 0  # never negative


@pytest.mark.asyncio
async def test_fail_open_on_redis_error(monkeypatch):
    monkeypatch.setattr("grid_api.redis_client.get_redis", lambda: _FakeRedis(fail=True))
    # A store outage must never 429 legitimate traffic.
    assert await concurrency.acquire("acct", "text", 1) is True
    assert await concurrency.acquire("acct", "text", 1) is True
    await concurrency.release("acct", "text")  # no raise


@pytest.mark.asyncio
async def test_no_account_or_no_limit_is_noop(redis):
    assert await concurrency.acquire(None, "text", 5) is True
    assert await concurrency.acquire("acct", "text", 0) is True
    assert redis.store == {}  # nothing counted

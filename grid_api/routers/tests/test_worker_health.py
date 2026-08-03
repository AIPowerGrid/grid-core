# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from grid_api.routers import worker_ws


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def incr(self, key):
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def expire(self, key, seconds):
        return True


@pytest.mark.asyncio
async def test_same_job_counts_once_but_new_job_adds_a_strike(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(worker_ws, "get_redis", lambda: redis)

    assert await worker_ws._record_strike("worker-1", "job-1") == 1
    assert await worker_ws._record_strike("worker-1", "job-1") == 1
    assert await worker_ws._record_strike("worker-1", "job-2") == 2

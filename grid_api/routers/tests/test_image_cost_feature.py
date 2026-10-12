# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 AI Power Grid
from unittest.mock import AsyncMock

import pytest

from grid_api.routers import health


@pytest.mark.asyncio
async def test_health_advertises_image_cost_limit_without_account_details(monkeypatch):
    redis = AsyncMock()
    monkeypatch.setattr(health, "get_redis", lambda: redis)
    monkeypatch.setattr(health, "get_available_models", AsyncMock(return_value=[]))
    monkeypatch.setattr(health, "get_connected_worker_count", AsyncMock(return_value=0))
    result = await health.health()
    assert "image-max-cost-micro-usd-v1" in result["features"]
    assert "video-async-credit-jobs-v1" in result["features"]
    assert "account_id" not in result

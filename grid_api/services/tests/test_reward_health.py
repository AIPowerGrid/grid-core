# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from grid_api.services import reward_health as monitor


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,jobs,kinds", [
    (False, 1, []), (True, 0, []), (True, 1, ["unbacked_reward_eligibility"]),
])
async def test_monitor_is_default_dark_and_only_warns_on_exposure(monkeypatch, enabled, jobs, kinds):
    monkeypatch.setattr(monitor, "get_settings", lambda: SimpleNamespace(grid_reward_monitor_enabled=enabled))
    query = AsyncMock(return_value={"unbacked_jobs": jobs, "unbacked_den": float(jobs)})
    emit = Mock()
    monkeypatch.setattr(monitor, "reward_backing_health", query)
    monkeypatch.setattr(monitor.alerts, "emit", emit)
    before = datetime.now(UTC)
    await monitor.check_and_alert()
    after = datetime.now(UTC)
    assert [call.args[0] for call in emit.call_args_list] == kinds
    if enabled:
        start, end = query.call_args.args
        assert (end - start).total_seconds() == 3600
        assert end.minute == end.second == end.microsecond == 0
        assert before.replace(minute=0, second=0, microsecond=0) <= end <= after
    else:
        query.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_failure_is_unknown_not_healthy_and_redacted(monkeypatch, caplog):
    monkeypatch.setattr(monitor, "get_settings", lambda: SimpleNamespace(grid_reward_monitor_enabled=True))
    monkeypatch.setattr(monitor, "reward_backing_health", AsyncMock(side_effect=RuntimeError("private-db-url")))
    emit = Mock()
    monkeypatch.setattr(monitor.alerts, "emit", emit)
    await monitor.check_and_alert()
    assert emit.call_count == 1
    assert emit.call_args.args[0] == "reward_monitor_failed"
    assert "private-db-url" not in caplog.text + str(emit.call_args)


@pytest.mark.asyncio
async def test_shutdown_cancellation_propagates(monkeypatch):
    monkeypatch.setattr(monitor, "get_settings", lambda: SimpleNamespace(grid_reward_monitor_enabled=True))
    monkeypatch.setattr(monitor, "reward_backing_health", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await monitor.check_and_alert()

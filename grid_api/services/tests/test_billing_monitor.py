# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exercise monitor decisions with synthetic health; never inject live faults."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from grid_api import main
from grid_api.services import alerts, credits, reward_health, treasury_health


@pytest.fixture
def monitor(monkeypatch):
    checks = SimpleNamespace(
        health=AsyncMock(return_value={"ok": True, "stale_held": 0}),
        emit=Mock(),
        treasury=AsyncMock(),
        rewards=AsyncMock(),
        verify=AsyncMock(),
        stale=AsyncMock(),
        sleep=AsyncMock(side_effect=asyncio.CancelledError),
    )
    monkeypatch.setenv("GRID_BILLING_MONITOR_SECONDS", "1")
    monkeypatch.setenv("GRID_BILLING_HELD_WARNING_SECONDS", "123")
    monkeypatch.setattr(credits, "billing_health", checks.health)
    monkeypatch.setattr(alerts, "emit", checks.emit)
    monkeypatch.setattr(treasury_health, "check_and_alert", checks.treasury)
    monkeypatch.setattr(reward_health, "check_and_alert", checks.rewards)
    monkeypatch.setattr(main.x402_payments, "verify_reported_settlements", checks.verify)
    monkeypatch.setattr(main.x402_payments, "flag_stale_settlements", checks.stale)
    monkeypatch.setattr(main.asyncio, "sleep", checks.sleep)
    return checks


@pytest.mark.asyncio
@pytest.mark.parametrize("ok,held,kinds", [
    (True, 0, []),
    (False, 0, ["billing_invariant_failed"]),
    (True, 2, ["billing_holds_aging"]),
    (False, 2, ["billing_invariant_failed", "billing_holds_aging"]),
])
async def test_health_decisions_and_other_monitors(monitor, ok, held, kinds):
    monitor.health.return_value = {"ok": ok, "stale_held": held}
    with pytest.raises(asyncio.CancelledError):
        await main._billing_monitor()
    monitor.health.assert_awaited_once_with(held_warning_seconds=123)
    assert [call.args[0] for call in monitor.emit.call_args_list] == kinds
    for call in monitor.emit.call_args_list:
        if call.args[0] == "billing_invariant_failed":
            assert call.args[1] == "critical"
            assert call.kwargs["fields"] == monitor.health.return_value
        else:
            assert call.args[1] == "warning"
            assert call.kwargs["fields"] == {
                "held_count": held, "warning_age_seconds": 123,
            }
    monitor.verify.assert_awaited_once_with()
    monitor.stale.assert_awaited_once_with()
    monitor.treasury.assert_awaited_once_with()
    monitor.rewards.assert_awaited_once_with()
    monitor.sleep.assert_awaited_once_with(60)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_check", ["health", "verify", "stale"])
async def test_failure_is_redacted_and_does_not_skip_other_monitors(
    monitor, failed_check, caplog,
):
    getattr(monitor, failed_check).side_effect = RuntimeError("sensitive-provider-detail")
    with pytest.raises(asyncio.CancelledError):
        await main._billing_monitor()
    monitor.emit.assert_called_once()
    call = monitor.emit.call_args
    assert call.args[:2] == ("billing_monitor_failed", "critical")
    assert call.kwargs["fields"] == {"error_type": "RuntimeError"}
    assert "sensitive-provider-detail" not in str(call)
    assert "sensitive-provider-detail" not in caplog.text
    monitor.treasury.assert_awaited_once_with()
    monitor.rewards.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_monitor_recovers_on_the_next_iteration(monitor):
    monitor.health.side_effect = [RuntimeError("private"), {"ok": True, "stale_held": 0}]
    monitor.sleep.side_effect = [None, asyncio.CancelledError]
    with pytest.raises(asyncio.CancelledError):
        await main._billing_monitor()
    assert monitor.health.await_count == 2
    assert monitor.treasury.await_count == monitor.rewards.await_count == 2
    assert [call.args[0] for call in monitor.emit.call_args_list] == ["billing_monitor_failed"]
    monitor.verify.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_is_not_reported_as_a_billing_incident(monitor):
    monitor.health.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await main._billing_monitor()
    monitor.emit.assert_not_called()
    monitor.verify.assert_not_awaited()
    monitor.treasury.assert_not_awaited()
    monitor.rewards.assert_not_awaited()
    monitor.sleep.assert_not_awaited()

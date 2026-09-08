# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from grid_api.config import GridSettings
from grid_api.services import treasury_health as monitor


def settings(**overrides):
    values = dict(
        _env_file=None,
        grid_treasury_monitor_enabled=True,
        grid_treasury_monitor_wallet="0x" + "1" * 40,
        grid_treasury_monitor_token="0x" + "2" * 40,
        base_rpc_url="https://rpc.invalid/private-provider-credential",
        grid_treasury_min_eth_wei=10,
        grid_treasury_min_token_raw=20,
    )
    values.update(overrides)
    return GridSettings(**values)


def rpc_transport(*, eth=10, token=20, chain=8453, mutate=None):
    calls = []

    def respond(request):
        call = json.loads(request.content)
        calls.append(call)
        result = {
            "eth_chainId": hex(chain), "eth_blockNumber": "0x123",
            "eth_getBalance": hex(eth), "eth_call": "0x" + f"{token:064x}",
        }[call["method"]]
        body = {"jsonrpc": "2.0", "id": call["id"], "result": result}
        if mutate:
            body = mutate(body)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(respond), calls


@pytest.mark.parametrize("field,value", [
    ("grid_treasury_monitor_wallet", ""),
    ("grid_treasury_monitor_wallet", "0x" + "0" * 40),
    ("grid_treasury_monitor_token", "not-an-address"),
    ("grid_treasury_min_eth_wei", 0),
    ("grid_treasury_min_token_raw", -1),
    ("grid_treasury_min_token_raw", 2**256),
    ("base_rpc_url", None),
])
def test_enabled_monitor_requires_complete_policy(field, value):
    with pytest.raises(ValidationError):
        settings(**{field: value})


@pytest.mark.asyncio
async def test_reads_balances_at_one_block_with_no_signing_methods():
    transport, calls = rpc_transport(eth=9, token=19)
    policy = settings()
    async with httpx.AsyncClient(transport=transport) as client:
        report = await monitor.inspect(policy, client)
    assert report == {"eth_wei": 9, "token_raw": 19, "low_eth": True, "low_token": True}
    assert [call["method"] for call in calls] == ["eth_chainId", "eth_blockNumber", "eth_getBalance", "eth_call"]
    assert calls[2]["params"] == [policy.grid_treasury_monitor_wallet, "0x123"]
    assert calls[3]["params"] == [{
        "to": policy.grid_treasury_monitor_token,
        "data": "0x70a08231" + ("1" * 40).rjust(64, "0"),
    }, "0x123"]


@pytest.mark.asyncio
async def test_wrong_chain_stops_before_balances():
    transport, calls = rpc_transport(chain=1)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="Base mainnet"):
            await monitor.inspect(settings(), client)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [
    lambda body: {**body, "id": True},
    lambda body: {**body, "id": 99},
    lambda body: {**body, "error": {"message": "provider credential"}},
    lambda body: {**body, "result": None},
    lambda body: {**body, "result": "0x"},
    lambda body: {**body, "result": "0x" + "f" * 65},
    lambda body: {**body, "result": "0x0001"},
    lambda body: [],
])
async def test_malformed_rpc_is_unknown_not_zero(mutate):
    transport, _ = rpc_transport(mutate=mutate)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError):
            await monitor.inspect(settings(), client)


@pytest.mark.asyncio
@pytest.mark.parametrize("eth,token,kinds", [
    (10, 20, []), (9, 20, ["treasury_low_eth"]),
    (10, 19, ["treasury_low_aipg"]),
    (0, 0, ["treasury_low_eth", "treasury_low_aipg"]),
])
async def test_alert_thresholds(monkeypatch, eth, token, kinds):
    transport, _ = rpc_transport(eth=eth, token=token)
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(monitor, "get_settings", settings)
    monkeypatch.setattr(monitor.httpx, "AsyncClient", lambda **_kwargs: client)
    emit = Mock()
    monkeypatch.setattr(monitor.alerts, "emit", emit)
    await monitor.check_and_alert()
    assert [call.args[0] for call in emit.call_args_list] == kinds


@pytest.mark.asyncio
async def test_transport_failure_does_not_emit_false_empty_alert(monkeypatch, caplog):
    def fail(_request):
        raise httpx.ConnectError("private-provider-credential")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    monkeypatch.setattr(monitor, "get_settings", settings)
    monkeypatch.setattr(monitor.httpx, "AsyncClient", lambda **_kwargs: client)
    emit = Mock()
    monkeypatch.setattr(monitor.alerts, "emit", emit)
    await monitor.check_and_alert()
    assert emit.call_count == 1
    assert emit.call_args.args[0] == "treasury_monitor_failed"
    assert "private-provider-credential" not in caplog.text
    assert "private-provider-credential" not in str(emit.call_args)


@pytest.mark.asyncio
async def test_malformed_erc20_word_cannot_look_healthy():
    transport, _ = rpc_transport(mutate=lambda body: {
        **body, "result": "0x14" if body["id"] == 4 else body["result"],
    })
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="Invalid treasury RPC result"):
            await monitor.inspect(settings(), client)


@pytest.mark.asyncio
async def test_cancellation_propagates(monkeypatch):
    async def cancel(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor, "get_settings", settings)
    monkeypatch.setattr(monitor, "inspect", cancel)
    emit = Mock()
    monkeypatch.setattr(monitor.alerts, "emit", emit)
    with pytest.raises(asyncio.CancelledError):
        await monitor.check_and_alert()
    emit.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_does_no_network_or_alert(monkeypatch):
    monkeypatch.setattr(monitor, "get_settings", lambda: GridSettings(_env_file=None))
    client = Mock(side_effect=AssertionError("must not connect"))
    emit = Mock()
    monkeypatch.setattr(monitor.httpx, "AsyncClient", client)
    monkeypatch.setattr(monitor.alerts, "emit", emit)
    await monitor.check_and_alert()
    client.assert_not_called()
    emit.assert_not_called()

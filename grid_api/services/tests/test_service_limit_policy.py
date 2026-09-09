# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Runtime policy validation must not rely on service provisioning history."""

from uuid import uuid4
from types import SimpleNamespace

import pytest

from grid_api.services import credits, service_limits


@pytest.fixture(autouse=True)
def no_external_access(monkeypatch):
    def unexpected_redis():
        pytest.fail("invalid or unbounded policy must not access Redis")

    async def unexpected_event(*args, **kwargs):
        pytest.fail("invalid policy must not write an economic/event record")

    monkeypatch.setattr("grid_api.redis_client.get_redis", unexpected_redis)
    monkeypatch.setattr(service_limits, "record_event", unexpected_event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits",
    [
        None,
        {},
        {"per_request_micro": 100},
        {"daily_micro": 100},
        {"per_request_micro": 100, "daily_micro": 0},
        {"per_request_micro": 100, "daily_micro": -1},
        {"per_request_micro": 0, "daily_micro": 100},
        {"per_request_micro": -1, "daily_micro": 100},
        {"per_request_micro": True, "daily_micro": 100},
        {"per_request_micro": 1, "daily_micro": True},
        {"per_request_micro": "100", "daily_micro": 100},
        {"per_request_micro": 100, "daily_micro": 100.0},
        {"per_request_micro": 101, "daily_micro": 100},
        ["not a policy"],
    ],
)
async def test_direct_service_requires_two_valid_runtime_caps(limits):
    user = {
        "key_kind": "service",
        "service_id": "policy-test",
        "scopes": ["inference.submit", "inference.service_submit"],
        "service_limits": limits,
    }
    assert await service_limits.authorize(user, 1, "job") == (
        False, "service spending policy unavailable",
    )


@pytest.mark.asyncio
async def test_direct_service_cannot_bypass_caps_without_service_id():
    assert await service_limits.authorize({"key_kind": "service"}, 1, "job") == (
        False, "service spending policy unavailable",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["delegated_user", "user_token"])
@pytest.mark.parametrize("limits", [None, {}, {"per_request_micro": 100}])
async def test_delegated_users_may_have_no_app_wide_daily_cap(kind, limits):
    # Their account's durable credit reservation remains independently required.
    user = {"key_kind": kind, "service_id": "bridge", "service_limits": limits}
    assert await service_limits.authorize(user, 10, "job") == (True, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, -1, True, "100", 100.0])
async def test_optional_delegated_cap_is_not_unlimited_when_malformed(cap):
    user = {
        "key_kind": "delegated_user",
        "service_id": "bridge",
        "service_limits": {"daily_micro": cap},
    }
    assert await service_limits.authorize(user, 1, "job") == (
        False, "service spending policy unavailable",
    )


@pytest.mark.asyncio
async def test_personal_account_needs_no_service_policy():
    assert await service_limits.authorize({"key_kind": "user"}, 10, "job") == (True, None)


@pytest.mark.asyncio
async def test_valid_direct_service_reserves_daily_budget(monkeypatch):
    calls = []

    class Redis:
        async def eval(self, *args):
            calls.append(args)
            return 1

    monkeypatch.setattr("grid_api.redis_client.get_redis", Redis)
    user = {
        "key_kind": "service",
        "service_id": "bounded",
        "service_limits": {"per_request_micro": 100, "daily_micro": 200},
    }
    assert await service_limits.authorize(user, 50, "job") == (True, None)
    assert len(calls) == 1
    assert calls[0][3] == "grid:service-spend-ref:bounded:job"
    assert calls[0][4:6] == (50, 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("charging_mode", ["on", "allowlist"])
@pytest.mark.parametrize("limits", [
    None, {}, [], "invalid", {"per_request_micro": 1, "daily_micro": 0},
    {"per_request_micro": True, "daily_micro": 100},
    {"per_request_micro": 101, "daily_micro": 100},
])
@pytest.mark.parametrize(
    "modality,model",
    [
        ("text", "gpt-oss-120b"),
        ("image", "z-image-turbo"),
        ("video", "ltx-2.3"),
        ("audio", "ace-step-v1.5-xl-turbo"),
        ("3d", "trellis2"),
    ],
)
async def test_global_charging_rejects_unbounded_service_before_credit_movement(
    monkeypatch, modality, model, charging_mode, limits,
):
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", charging_mode)
    monkeypatch.setattr(credits, "CHARGING_ALLOW_ACCOUNTS", frozenset())
    monkeypatch.setattr(credits, "CHARGING_ALLOW_SERVICES", frozenset())
    monkeypatch.setattr(credits, "CHARGING_ALLOW_MODELS", frozenset({"other-model"}))
    monkeypatch.setattr(credits, "get_settings", lambda: SimpleNamespace(
        grid_charging_all_model_services=["legacy-unbounded"],
    ))

    async def no_discount(*args, **kwargs):
        return 0

    async def unchanged_cost(cost, **kwargs):
        return cost

    async def no_money(*args, **kwargs):
        pytest.fail("invalid service must not reserve any credit pocket")

    monkeypatch.setattr(credits, "holder_discount_bps", no_discount)
    monkeypatch.setattr(credits, "apply_holder_discount", unchanged_cost)
    monkeypatch.setattr(credits, "_promo_first", no_money)
    monkeypatch.setattr(credits, "_free_first", no_money)
    monkeypatch.setattr(credits, "new_session", no_money)
    monkeypatch.setattr(credits, "_economic_alert", lambda *args, **kwargs: None)
    user = {
        "account_id": uuid4(),
        "key_kind": "service",
        "service_id": "legacy-unbounded",
        "service_limits": limits,
        "scopes": ["inference.submit", "inference.service_submit"],
    }
    if modality == "text":
        result = await credits.authorize_request(
            user, model, 100, 100, uuid4(), record_reservation=True,
        )
    else:
        result = await credits.authorize_media(
            user["account_id"], model, modality, 1, 10, uuid4(),
            user=user, record_reservation=True,
        )
    assert result == {
        "ok": False,
        "reserved": 0,
        "status": "service_limit",
        "reason": "service spending policy unavailable",
    }

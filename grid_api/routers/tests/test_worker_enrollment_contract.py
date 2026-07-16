# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from grid_api.routers import worker_enrollment as enrollment_router


@pytest.mark.asyncio
async def test_enrollment_approval_requires_recent_account_manage_token(monkeypatch):
    observed = {}

    async def authenticate(key, *, required_scope):
        observed.update(key=key, required_scope=required_scope)
        return {
            "account_id": "11111111-1111-1111-1111-111111111111",
            "key_kind": "user_token",
            "token_claims": {"amr": "siwe", "auth_time": int(time.time())},
        }

    monkeypatch.setattr(enrollment_router.accounts, "authenticate", authenticate)
    user = await enrollment_router._recent_account("user-token", None)
    assert user["account_id"].startswith("1111")
    assert observed == {
        "key": "user-token",
        "required_scope": "account.manage",
    }


@pytest.mark.asyncio
async def test_enrollment_approval_rejects_non_user_credentials(monkeypatch):
    async def authenticate(*_args, **_kwargs):
        return {
            "account_id": "11111111-1111-1111-1111-111111111111",
            "key_kind": "service",
            "token_claims": {},
        }

    monkeypatch.setattr(enrollment_router.accounts, "authenticate", authenticate)
    with pytest.raises(HTTPException) as exc:
        await enrollment_router._recent_account("service-key", None)
    assert exc.value.status_code == 403


def test_worker_enrollment_is_default_off(monkeypatch):
    monkeypatch.setattr(
        enrollment_router,
        "get_settings",
        lambda: type("Settings", (), {"worker_enrollment_enabled": False})(),
    )
    with pytest.raises(HTTPException) as exc:
        enrollment_router._enabled()
    assert exc.value.status_code == 503

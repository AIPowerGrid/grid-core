# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Account-admin actions require a wallet-proven SESSION key.

A leaked inference key must not be able to change the payout wallet or mint/kill
keys. These exercise `_require_session` with a stubbed authenticate() so they
need no Postgres."""

import pytest
import time
from fastapi import HTTPException

from grid_api.routers import accounts as acc


def _auth_returning(user):
    async def _f(_key, *_args, **_kwargs):
        return user
    return _f


@pytest.mark.asyncio
async def test_session_gate_rejects_inference_key(monkeypatch):
    monkeypatch.setattr(acc.accounts_svc, "authenticate",
                        _auth_returning({"source": "v2", "account_id": "a", "is_session": False}))
    with pytest.raises(HTTPException) as e:
        await acc._require_session("infkey", None)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_session_gate_allows_recent_native_proof(monkeypatch):
    monkeypatch.setattr(acc.accounts_svc, "authenticate",
                        _auth_returning({
                            "source": "v2", "account_id": "a", "is_session": False,
                            "key_kind": "user_token", "scopes": ["account.manage"],
                            "token_claims": {"amr": "siwe", "auth_time": int(time.time())},
                        }))
    user = await acc._require_session("sesskey", None)
    assert user["account_id"] == "a"


@pytest.mark.asyncio
async def test_session_gate_rejects_legacy_key(monkeypatch):
    # legacy keys fail _require_v2 first (no v2 account) → 403, never reach admin.
    monkeypatch.setattr(acc.accounts_svc, "authenticate",
                        _auth_returning({"source": "legacy"}))
    with pytest.raises(HTTPException) as e:
        await acc._require_session("legacykey", None)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_inference_key_still_reads_account(monkeypatch):
    # An inference key CAN still authenticate for read/inference (_require_v2 ok).
    monkeypatch.setattr(acc.accounts_svc, "authenticate",
                        _auth_returning({"source": "v2", "account_id": "a", "is_session": False}))
    user = await acc._require_v2("infkey", None)
    assert user["account_id"] == "a" and user["is_session"] is False

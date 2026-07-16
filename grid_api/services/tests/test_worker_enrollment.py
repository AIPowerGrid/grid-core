# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import accounts, worker_enrollment
from grid_api.services.worker_identity import delegation_message
from grid_api.v2.schema import accounts as accounts_table
from grid_api.v2.schema import api_keys as api_keys_table
from grid_api.v2.schema import metadata


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    async def get(self, key):
        return self.values.get(key)

    async def ttl(self, key):
        return self.ttls.get(key, -2)

    async def delete(self, key):
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return 1

    async def eval(self, _script, _count, key, token):
        if self.values.get(key) == token:
            await self.delete(key)
            return 1
        return 0


@pytest_asyncio.fixture
async def account_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        yield
    finally:
        database._session_factory = old
        await engine.dispose()


@pytest.fixture
def enrollment_env(monkeypatch):
    redis = FakeRedis()
    settings = SimpleNamespace(
        worker_enrollment_ttl_seconds=900,
        worker_enrollment_console_url="https://console.example/dashboard/connect-worker",
        worker_identity_chain_id=8453,
        worker_identity_audience="api.example",
    )
    monkeypatch.setattr(worker_enrollment, "get_redis", lambda: redis)
    monkeypatch.setattr(worker_enrollment, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_enrollment, "hash_api_key", lambda value: hashlib.sha256(value.encode()).hexdigest())
    calls = {"payout": [], "install": [], "activate": [], "revoke": []}

    async def install(account_id, key_hash, *, payout_wallet, **kwargs):
        calls["payout"].append((str(account_id), payout_wallet))
        calls["install"].append((str(account_id), key_hash, kwargs))

    async def activate(account_id, key_hash):
        calls["activate"].append((str(account_id), key_hash))
        return True

    async def revoke(account_id, key_hash):
        calls["revoke"].append((str(account_id), key_hash))

    monkeypatch.setattr(worker_enrollment.accounts, "install_enrolled_worker_key", install)
    monkeypatch.setattr(worker_enrollment.accounts, "activate_prehashed_worker_key", activate)
    monkeypatch.setattr(worker_enrollment.accounts, "revoke_prehashed_worker_key", revoke)
    return redis, calls


async def _create(poll_token="p" * 48):
    worker = Account.create()
    result = await worker_enrollment.create_enrollment(
        worker_signer=worker.address,
        worker_name="audio-rig-1",
        profile_id="ace-step-v1.5-turbo",
        api_key="grid_" + "a" * 32,
        poll_token_hash=hashlib.sha256(poll_token.encode()).hexdigest(),
        valid_days=90,
    )
    return result, worker, poll_token


@pytest.mark.asyncio
async def test_full_enrollment_is_private_signed_and_ack_gated(enrollment_env):
    _redis, calls = enrollment_env
    created, worker, poll_token = await _create()
    enrollment_id = created["enrollment_id"]
    account_id = uuid4()
    wallet = Account.create()
    user = {
        "account_id": account_id,
        "wallet": "",
        "payout_wallet": "",
    }

    public = await worker_enrollment.public_enrollment(enrollment_id)
    encoded_public = json.dumps(public)
    assert public["worker_signer"] == worker.address.lower()
    assert "api_key" not in encoded_public
    assert "poll_token" not in encoded_public

    prepared = await worker_enrollment.prepare_enrollment(
        enrollment_id,
        user=user,
        payout_wallet=wallet.address,
        replace_payout_wallet=False,
    )
    signature = Account.sign_message(
        encode_defunct(text=prepared["message"]),
        wallet.key,
    ).signature.hex()
    assert prepared["message"] == delegation_message(prepared["payload"])

    assert await worker_enrollment.approve_enrollment(
        enrollment_id,
        user=user,
        signature=signature,
    ) == {"status": "complete"}
    assert len(calls["install"]) == 1
    assert calls["activate"] == []
    assert calls["payout"] == [(str(account_id), wallet.address.lower())]

    with pytest.raises(worker_enrollment.EnrollmentUnauthorized):
        await worker_enrollment.poll_enrollment(
            enrollment_id,
            poll_token="wrong" * 10,
        )
    polled = await worker_enrollment.poll_enrollment(
        enrollment_id,
        poll_token=poll_token,
    )
    assert polled["certificate"]["signature"] == signature
    assert "api_key" not in json.dumps(polled)

    assert await worker_enrollment.acknowledge_enrollment(
        enrollment_id,
        poll_token=poll_token,
    ) == {"status": "activated"}
    assert len(calls["activate"]) == 1


@pytest.mark.asyncio
async def test_prepare_refuses_account_or_wallet_takeover(enrollment_env):
    await _create_result(enrollment_env)


async def _create_result(enrollment_env):
    _redis, _calls = enrollment_env
    created, _worker, _token = await _create()
    first_id = uuid4()
    first_wallet = Account.create()
    first = {"account_id": first_id, "payout_wallet": first_wallet.address, "wallet": ""}

    with pytest.raises(worker_enrollment.EnrollmentConflict, match="replacement"):
        await worker_enrollment.prepare_enrollment(
            created["enrollment_id"],
            user=first,
            payout_wallet=Account.create().address,
            replace_payout_wallet=False,
        )

    prepared = await worker_enrollment.prepare_enrollment(
        created["enrollment_id"],
        user=first,
        payout_wallet=first_wallet.address,
        replace_payout_wallet=False,
    )
    assert prepared["payload"]["payout_wallet"] == first_wallet.address.lower()
    with pytest.raises(worker_enrollment.EnrollmentConflict, match="another account"):
        await worker_enrollment.prepare_enrollment(
            created["enrollment_id"],
            user={"account_id": uuid4(), "payout_wallet": "", "wallet": ""},
            payout_wallet=first_wallet.address,
            replace_payout_wallet=False,
        )


@pytest.mark.asyncio
async def test_duplicate_approval_does_not_issue_another_key(enrollment_env):
    _redis, calls = enrollment_env
    created, _worker, _token = await _create()
    user = {"account_id": uuid4(), "payout_wallet": "", "wallet": ""}
    wallet = Account.create()
    prepared = await worker_enrollment.prepare_enrollment(
        created["enrollment_id"],
        user=user,
        payout_wallet=wallet.address,
        replace_payout_wallet=False,
    )
    signature = Account.sign_message(
        encode_defunct(text=prepared["message"]),
        wallet.key,
    ).signature.hex()

    await worker_enrollment.approve_enrollment(
        created["enrollment_id"],
        user=user,
        signature=signature,
    )
    assert await worker_enrollment.approve_enrollment(
        created["enrollment_id"],
        user=user,
        signature=signature,
    ) == {"status": "complete"}
    assert len(calls["install"]) == 1


@pytest.mark.asyncio
async def test_invalid_wallet_signature_never_installs_key(enrollment_env):
    _redis, calls = enrollment_env
    created, _worker, _token = await _create()
    user = {"account_id": uuid4(), "payout_wallet": "", "wallet": ""}
    wallet = Account.create()
    prepared = await worker_enrollment.prepare_enrollment(
        created["enrollment_id"],
        user=user,
        payout_wallet=wallet.address,
        replace_payout_wallet=False,
    )
    wrong = Account.create()
    signature = Account.sign_message(
        encode_defunct(text=prepared["message"]),
        wrong.key,
    ).signature.hex()

    with pytest.raises(worker_enrollment.EnrollmentUnauthorized, match="payout wallet"):
        await worker_enrollment.approve_enrollment(
            created["enrollment_id"],
            user=user,
            signature=signature,
        )
    assert calls["install"] == []
    assert calls["payout"] == []


@pytest.mark.asyncio
async def test_worker_key_is_narrow_and_temporary_until_ack(account_db):
    account, _key = await accounts.create_account(issue_initial_key=False)
    plain_key = "grid_" + "a" * 32
    key_hash = accounts.hash_api_key(plain_key)
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)

    await accounts.install_prehashed_worker_key(
        account["id"],
        key_hash,
        label="worker:audio-rig-1",
        expires_at=expires,
    )
    async with await database.new_session() as session:
        row = (
            (
                await session.execute(
                    sa.select(api_keys_table).where(api_keys_table.c.hash == key_hash),
                )
            )
            .mappings()
            .one()
        )
    assert row["key_kind"] == "worker"
    assert row["scopes"] == ["worker.connect"]
    assert row["is_session"] is False
    assert row["expires_at"] is not None

    resolved = await accounts.resolve_api_key(plain_key)
    assert resolved["key_kind"] == "worker"
    assert resolved["key_label"] == "worker:audio-rig-1"

    assert await accounts.activate_prehashed_worker_key(account["id"], key_hash)
    async with await database.new_session() as session:
        activated = await session.scalar(
            sa.select(api_keys_table.c.expires_at).where(
                api_keys_table.c.hash == key_hash,
            ),
        )
    assert activated is None


@pytest.mark.asyncio
async def test_worker_key_activation_revokes_the_previous_rig_key(account_db):
    account, _key = await accounts.create_account(issue_initial_key=False)
    expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
    old_hash = "ab" * 32
    new_hash = "cd" * 32
    await accounts.install_prehashed_worker_key(
        account["id"],
        old_hash,
        label="worker:audio-rig-1",
        expires_at=expiry,
    )
    assert await accounts.activate_prehashed_worker_key(account["id"], old_hash)
    await accounts.install_prehashed_worker_key(
        account["id"],
        new_hash,
        label="worker:audio-rig-1",
        expires_at=expiry,
    )
    assert await accounts.activate_prehashed_worker_key(account["id"], new_hash)

    async with await database.new_session() as session:
        rows = (
            (
                await session.execute(
                    sa.select(
                        api_keys_table.c.hash,
                        api_keys_table.c.revoked,
                        api_keys_table.c.expires_at,
                    ).where(api_keys_table.c.hash.in_([old_hash, new_hash])),
                )
            )
            .mappings()
            .all()
        )
    by_hash = {row["hash"]: row for row in rows}
    assert by_hash[old_hash]["revoked"] is True
    assert by_hash[new_hash]["revoked"] is False
    assert by_hash[new_hash]["expires_at"] is None


@pytest.mark.asyncio
async def test_enrollment_key_collision_cannot_partially_change_payout_wallet(account_db):
    target, _ = await accounts.create_account(issue_initial_key=False)
    other, _ = await accounts.create_account(issue_initial_key=False)
    original_wallet = Account.create().address.lower()
    replacement_wallet = Account.create().address.lower()
    target_id = UUID(target["id"])
    await accounts.set_payout_wallet(target_id, original_wallet)
    key_hash = "ef" * 32
    expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
    await accounts.install_prehashed_worker_key(
        other["id"],
        key_hash,
        label="worker:other-rig",
        expires_at=expiry,
    )

    with pytest.raises(IntegrityError):
        await accounts.install_enrolled_worker_key(
            target_id,
            key_hash,
            label="worker:audio-rig-1",
            expires_at=expiry,
            payout_wallet=replacement_wallet,
        )

    async with await database.new_session() as session:
        stored = await session.scalar(
            sa.select(accounts_table.c.payout_wallet).where(
                accounts_table.c.id == target_id,
            ),
        )
    assert stored == original_wallet

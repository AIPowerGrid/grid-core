# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from grid_api.routers import accounts, stats
from grid_api.v2.schema import (
    accounts as accounts_table,
)
from grid_api.v2.schema import (
    ledger,
    metadata,
    payouts,
    workers,
)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/workers/self",
            "headers": [],
            "client": ("127.0.0.1", 50000),
        },
    )


@pytest.mark.asyncio
async def test_worker_self_requires_a_manager_credential(monkeypatch):
    async def authenticate(_key, *, required_scope):
        assert required_scope == "worker.connect"
        return {
            "source": "v2",
            "key_kind": "user",
            "key_label": "worker:rig-a",
        }

    monkeypatch.setattr(accounts.accounts_svc, "authenticate", authenticate)

    with pytest.raises(HTTPException) as exc:
        await accounts._require_worker_self("grid_test", None)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_worker_self_is_exactly_bound_and_redacts_account_payout(monkeypatch):
    now = datetime.now(UTC)
    account_id = uuid4()
    worker_id = uuid4()
    sibling_id = uuid4()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        async with sessions() as session:
            await session.execute(
                sa.insert(accounts_table).values(
                    id=account_id,
                    username="private-operator",
                    wallet="0x" + "1" * 40,
                    payout_wallet="0x" + "2" * 40,
                    flags={},
                    created=now,
                ),
            )
            await session.execute(
                sa.insert(workers),
                [
                    {
                        "id": worker_id,
                        "account_id": account_id,
                        "name": "rig-a",
                        "type": "text",
                        "models": ["model-a"],
                        "capabilities": {
                            "job_types": ["text"],
                            "signer_address": "0x" + "3" * 40,
                        },
                        "maintenance": False,
                        "first_seen": now,
                        "last_seen": now,
                    },
                    {
                        "id": sibling_id,
                        "account_id": account_id,
                        "name": "private-sibling",
                        "type": "image",
                        "models": ["private-model"],
                        "capabilities": {"job_types": ["image"]},
                        "maintenance": False,
                        "first_seen": now,
                        "last_seen": now,
                    },
                ],
            )
            await session.execute(
                sa.insert(ledger),
                [
                    {
                        "job_id": uuid4(),
                        "worker_id": worker_id,
                        "model": "model-a",
                        "job_type": "text",
                        "den": 2.25,
                        "output_units": 10,
                        "created": now,
                    },
                    {
                        "job_id": uuid4(),
                        "worker_id": sibling_id,
                        "model": "private-model",
                        "job_type": "image",
                        "den": 9000,
                        "output_units": 1,
                        "created": now,
                    },
                ],
            )
            await session.execute(
                sa.insert(payouts).values(
                    period_id="private-period",
                    account_id=account_id,
                    address="0x" + "2" * 40,
                    den=9002.25,
                    aipg_amount=Decimal("1234.5"),
                    status="confirmed",
                    tx_hash="0x" + "4" * 64,
                    created=now,
                    paid=now,
                ),
            )
            await session.commit()

        async def new_session():
            return sessions()

        async def authenticate(key, *, required_scope):
            assert key == "grid_worker_test"
            assert required_scope == "worker.connect"
            return {
                "source": "v2",
                "key_kind": "worker",
                "key_label": "worker:rig-a",
                "account_id": account_id,
                "payout_wallet": "0x" + "2" * 40,
            }

        async def active_workers():
            return [
                {"worker_id": str(worker_id), "name": "rig-a"},
                {"worker_id": str(sibling_id), "name": "private-sibling"},
            ]

        monkeypatch.setattr(accounts, "new_session", new_session)
        monkeypatch.setattr(accounts.accounts_svc, "authenticate", authenticate)
        monkeypatch.setattr(stats, "_active_workers", active_workers)

        handler = getattr(
            accounts.get_worker_self_status,
            "__wrapped__",
            accounts.get_worker_self_status,
        )
        result = await handler(_request(), apikey="grid_worker_test", authorization=None)

        assert result["schema"] == "aipg.worker.self.v1"
        assert result["worker"] == {
            "name": "rig-a",
            "online": True,
            "maintenance": False,
            "last_seen": now.replace(tzinfo=None).isoformat(),
            "models": ["model-a"],
            "job_types": ["text"],
            "jobs_completed": 1,
            "den_recorded": 2.25,
        }
        assert result["payout"] == {
            "scope": "account",
            "wallet_configured": True,
            "latest_status": "confirmed",
            "last_paid_at": now.replace(tzinfo=None).isoformat(),
        }
        rendered = str(result)
        for secret in (
            "private-operator",
            "private-sibling",
            "private-model",
            "private-period",
            "1234.5",
            "0x" + "2" * 40,
            "0x" + "4" * 64,
        ):
            assert secret not in rendered
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_self_canary_uses_exact_bound_online_worker(monkeypatch):
    now = datetime.now(UTC)
    account_id = uuid4()
    worker_id = uuid4()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        async with sessions() as session:
            await session.execute(
                sa.insert(accounts_table).values(
                    id=account_id,
                    username="operator",
                    flags={},
                    created=now,
                ),
            )
            await session.execute(
                sa.insert(workers).values(
                    id=worker_id,
                    account_id=account_id,
                    name="rig-a",
                    type="text",
                    models=["model-a", "model-b"],
                    capabilities={"job_types": ["text"]},
                    maintenance=False,
                    first_seen=now,
                    last_seen=now,
                ),
            )
            await session.commit()

        async def new_session():
            return sessions()

        async def authenticate(key, *, required_scope):
            assert key == "grid_worker_test"
            assert required_scope == "worker.connect"
            return {
                "source": "v2",
                "key_kind": "worker",
                "key_label": "worker:rig-a",
                "account_id": account_id,
            }

        async def active_workers():
            return [
                {"worker_id": str(uuid4()), "name": "unrelated"},
                {"worker_id": str(worker_id), "name": "rig-a"},
            ]

        observed = {}

        async def run_canary(**kwargs):
            observed.update(kwargs)
            return {
                "schema": "aipg.worker.canary.v1",
                "status": "passed",
                "economic_effect": "none",
            }

        monkeypatch.setattr(accounts, "new_session", new_session)
        monkeypatch.setattr(accounts.accounts_svc, "authenticate", authenticate)
        monkeypatch.setattr(stats, "_active_workers", active_workers)
        monkeypatch.setattr(
            accounts.worker_canaries,
            "run_text_connectivity_canary",
            run_canary,
        )

        handler = getattr(
            accounts.run_worker_self_canary,
            "__wrapped__",
            accounts.run_worker_self_canary,
        )
        result = await handler(
            _request(),
            apikey="grid_worker_test",
            authorization=None,
        )

        assert result["status"] == "passed"
        assert observed == {
            "worker_id": str(worker_id),
            "worker_name": "rig-a",
            "model": "model-a",
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_self_canary_selects_bound_media_modality(monkeypatch):
    now = datetime.now(UTC)
    account_id = uuid4()
    worker_id = uuid4()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        async with sessions() as session:
            await session.execute(
                sa.insert(accounts_table).values(
                    id=account_id,
                    username="media-operator",
                    flags={},
                    created=now,
                ),
            )
            await session.execute(
                sa.insert(workers).values(
                    id=worker_id,
                    account_id=account_id,
                    name="audio-rig",
                    type="media",
                    models=["ace-step-v1.5-xl-turbo"],
                    capabilities={"job_types": ["audio"]},
                    maintenance=False,
                    first_seen=now,
                    last_seen=now,
                ),
            )
            await session.commit()

        async def new_session():
            return sessions()

        async def authenticate(_key, *, required_scope):
            assert required_scope == "worker.connect"
            return {
                "source": "v2",
                "key_kind": "worker",
                "key_label": "worker:audio-rig",
                "account_id": account_id,
            }

        async def active_workers():
            return [{"worker_id": str(worker_id), "name": "audio-rig"}]

        observed = {}

        async def run_canary(**kwargs):
            observed.update(kwargs)
            return {
                "schema": "aipg.worker.canary.v1",
                "status": "passed",
                "economic_effect": "none",
            }

        monkeypatch.setattr(accounts, "new_session", new_session)
        monkeypatch.setattr(accounts.accounts_svc, "authenticate", authenticate)
        monkeypatch.setattr(stats, "_active_workers", active_workers)
        monkeypatch.setattr(
            accounts.worker_canaries,
            "run_media_connectivity_canary",
            run_canary,
        )

        handler = getattr(
            accounts.run_worker_self_canary,
            "__wrapped__",
            accounts.run_worker_self_canary,
        )
        result = await handler(_request(), apikey="grid_worker_test", authorization=None)

        assert result["status"] == "passed"
        assert observed == {
            "worker_id": str(worker_id),
            "worker_name": "audio-rig",
            "models": ["ace-step-v1.5-xl-turbo"],
            "job_type": "audio",
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_self_canary_rejects_offline_worker_before_dispatch(monkeypatch):
    now = datetime.now(UTC)
    account_id = uuid4()
    worker_id = uuid4()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        async with sessions() as session:
            await session.execute(
                sa.insert(accounts_table).values(
                    id=account_id,
                    username="operator",
                    flags={},
                    created=now,
                ),
            )
            await session.execute(
                sa.insert(workers).values(
                    id=worker_id,
                    account_id=account_id,
                    name="rig-a",
                    type="text",
                    models=["model-a"],
                    capabilities={"job_types": ["text"]},
                    maintenance=False,
                    first_seen=now,
                    last_seen=now,
                ),
            )
            await session.commit()

        async def new_session():
            return sessions()

        async def authenticate(_key, *, required_scope):
            assert required_scope == "worker.connect"
            return {
                "source": "v2",
                "key_kind": "worker",
                "key_label": "worker:rig-a",
                "account_id": account_id,
            }

        async def active_workers():
            return []

        async def forbidden(**_kwargs):
            raise AssertionError("offline workers must not receive a canary")

        monkeypatch.setattr(accounts, "new_session", new_session)
        monkeypatch.setattr(accounts.accounts_svc, "authenticate", authenticate)
        monkeypatch.setattr(stats, "_active_workers", active_workers)
        monkeypatch.setattr(
            accounts.worker_canaries,
            "run_text_connectivity_canary",
            forbidden,
        )

        handler = getattr(
            accounts.run_worker_self_canary,
            "__wrapped__",
            accounts.run_worker_self_canary,
        )
        with pytest.raises(HTTPException) as exc:
            await handler(_request(), apikey="grid_worker_test", authorization=None)
        assert exc.value.status_code == 409
        assert exc.value.detail == "Bound worker is not online"
    finally:
        await engine.dispose()

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real scoped credentials/SQL; no worker, GPU, Redis or storage simulation."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import auth, database
from grid_api.routers import worker_media
from grid_api.services import accounts as account_service
from grid_api.v2.schema import accounts, credit_ledger, ledger, media_jobs, metadata, reservations, workers


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def setup(request, monkeypatch):
    monkeypatch.setattr(auth, "_API_KEY_SALT", uuid4().hex)
    namespace = "worker_media_test_" + uuid4().hex
    postgres = request.param == "postgres"
    if postgres:
        url = os.environ.get("CREDITS_TEST_DB_URL", "")
        if not url.startswith("postgresql+asyncpg://"):
            pytest.skip("Disposable CREDITS_TEST_DB_URL required")
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": namespace}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        async with engine.begin() as connection:
            if postgres:
                await connection.execute(sa.schema.CreateSchema(namespace))
            else:
                await connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            await connection.run_sync(metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(database, "_session_factory", factory)
        monkeypatch.setattr(worker_media.limiter, "enabled", False)
        owner, customer, sibling, foreign = uuid4(), uuid4(), uuid4(), uuid4()
        wid = uuid4()
        async with factory() as session:
            await session.execute(accounts.insert(), [{"id": aid, "flags": {}} for aid in (owner, customer, foreign)])
            await session.execute(
                workers.insert(),
                [
                    {"id": wid, "account_id": owner, "name": "rig-a", "type": "video"},
                    {"id": sibling, "account_id": owner, "name": "rig-b", "type": "video"},
                    {"id": foreign, "account_id": foreign, "name": "foreign-rig", "type": "video"},
                ],
            )
            await session.commit()
        key = await account_service.issue_key(owner, "worker:rig-a", scopes=["worker.connect"], key_kind="worker")
        app = FastAPI()
        app.include_router(worker_media.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield factory, client, key, owner, customer, wid, sibling, foreign
    finally:
        try:
            if postgres:
                async with engine.begin() as connection:
                    await connection.execute(sa.schema.DropSchema(namespace, cascade=True, if_exists=True))
        finally:
            await engine.dispose()


async def add_job(factory, customer, worker_id, *, status="held", age=0, deadline=60, leased=True):
    job_id, now = str(uuid4()), datetime.now(UTC)
    async with factory() as session:
        await session.execute(
            reservations.insert().values(
                job_id=job_id,
                account_id=customer,
                model="private-model",
                reserved_micro=99,
                status=status,
                media_result={"private": "never exposed"} if status == "settled" else None,
            ),
        )
        await session.execute(
            media_jobs.insert().values(
                job_id=job_id,
                account_id=customer,
                idempotency_hash=uuid4().hex * 2,
                request_hash="a" * 64,
                payload_hash="b" * 64,
                model="private-model",
                payload={"prompt": "private customer prompt"},
                route_models=["private-model"],
                max_cost_micro=99,
                execution_worker_id=worker_id,
                execution_started=now - timedelta(seconds=age) if worker_id else None,
                execution_model="private-model" if leased and worker_id else None,
                execution_token=str(uuid4()) if leased and worker_id else None,
                execution_deadline=now + timedelta(seconds=deadline) if leased and worker_id else None,
            ),
        )
        await session.commit()
    return job_id


async def query(client, key, job_ids):
    return await client.post("/v1/workers/self/media-jobs", headers={"apikey": key}, json={"job_ids": job_ids})


@pytest.mark.asyncio
async def test_only_assigned_rig_sees_irreversible_closure_and_live_holds(setup):
    factory, client, key, _, customer, wid, sibling, foreign = setup
    states = {}
    for expected, kwargs in [
        ("held", {}),
        ("closed", {"status": "settled"}),
        ("closed", {"status": "settled", "deadline": -60}),
        ("unknown", {"deadline": -1}),
        ("unknown", {"age": 1801}),
        ("unknown", {"age": -120}),
        ("unknown", {"leased": False}),
        ("unknown", {"worker_id": sibling, "status": "settled"}),
        ("unknown", {"worker_id": foreign, "status": "settled"}),
        ("unknown", {"worker_id": None}),
    ]:
        worker_id = kwargs.pop("worker_id", wid)
        states[await add_job(factory, customer, worker_id, **kwargs)] = expected
    states[str(uuid4())] = "unknown"
    result = await query(client, key, list(states))
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json() == {
        "schema": worker_media.SCHEMA,
        "worker_id": str(wid),
        "jobs": [{"job_id": jid, "state": state} for jid, state in states.items()],
    }
    assert "private" not in result.text and str(customer) not in result.text
    async with factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(credit_ledger)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(ledger)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(reservations)) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,scopes,label",
    [
        ("user", ["worker.connect"], "worker:rig-a"),
        ("worker", ["inference.submit"], "worker:rig-a"),
        ("worker", ["worker.connect"], "rig-a"),
    ],
)
async def test_real_credentials_cannot_broaden_read_authority(setup, kind, scopes, label):
    _, client, _, owner, *_ = setup
    key = await account_service.issue_key(owner, label, scopes=scopes, key_kind=kind)
    result = await query(client, key, [str(uuid4())])
    assert result.status_code == 403
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_invalid_or_revoked_key_and_unregistered_worker_fail_closed(setup):
    factory, client, key, owner, _, wid, *_ = setup
    assert (await query(client, "not-a-key", [str(uuid4())])).status_code == 401
    async with factory() as session:
        await session.execute(workers.delete().where(workers.c.id == wid))
        await session.commit()
    result = await query(client, key, [str(uuid4())])
    assert result.status_code == 404 and result.headers["cache-control"] == "no-store"
    missing = await account_service.issue_key(owner, "worker:never-registered", scopes=["worker.connect"], key_kind="worker")
    assert (await query(client, missing, [str(uuid4())])).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"job_ids": []},
        {"job_ids": ["not-uuid"]},
        {"job_ids": [str(uuid4()).upper()]},
        {"job_ids": [str(uuid4())] * 2},
        {"job_ids": [str(uuid4()) for _ in range(33)]},
        {"job_ids": [str(uuid4())], "worker_id": "target-another-rig"},
    ],
)
async def test_typed_body_bounds_and_redacted_errors(setup, body):
    _, client, key, *_ = setup
    result = await client.post("/v1/workers/self/media-jobs", headers={"apikey": key}, json=body)
    assert result.status_code == 422
    assert result.json() == {"detail": "Invalid worker status request"}
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_raw_body_bound_precedes_json_and_database_unavailability_is_private(setup, monkeypatch):
    _, client, key, *_ = setup
    result = await client.post(
        "/v1/workers/self/media-jobs",
        headers={"apikey": key, "content-type": "application/json"},
        content=b"x" * 2049,
    )
    assert result.status_code == 413 and result.headers["cache-control"] == "no-store"

    async def unavailable():
        raise RuntimeError("private database credentials")

    monkeypatch.setattr(worker_media, "new_session", unavailable)
    result = await query(client, key, [str(uuid4())])
    assert result.status_code == 503 and "private" not in result.text

# SPDX-License-Identifier: AGPL-3.0-or-later
"""A lost HTTP response must not lose a committed billed-media result."""

import copy
import importlib.util
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from grid_api import database
from grid_api.routers import media_results as router
from grid_api.services import accounts, credits, media_results, user_tokens
from grid_api.v2.schema import accounts as accounts_t, account_aliases, ledger, metadata, reservations

PG = os.environ.get("VALIDATORS_TEST_DB_URL", "")
MODEL = "Krea 2 Turbo"
RESULT = {"media": [{"url": "https://assets.example/job.webp", "key": "job.webp",
                     "sha256": "a" * 64, "seed": 12}],
          "model": MODEL, "worker": "media-test", "gen_time": 2.0, "recipe_root": None}


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def db(request, monkeypatch):
    if request.param == "postgres" and not PG.startswith("postgresql"):
        pytest.skip("set VALIDATORS_TEST_DB_URL to a disposable PostgreSQL database")
    namespace = "media_result_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        PG if request.param == "postgres" else "sqlite+aiosqlite:///:memory:",
        **({"execution_options": {"schema_translate_map": {None: namespace}}}
           if request.param == "postgres" else {}),
    )
    created = False
    try:
        async with engine.begin() as conn:
            if request.param == "postgres":
                await conn.execute(sa.schema.CreateSchema(namespace))
            await conn.run_sync(metadata.create_all)
        created = True
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(database, "_session_factory", factory)
        monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
        monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "on")
        yield factory, request.param
    finally:
        if created and request.param == "postgres":
            async with engine.begin() as conn:
                await conn.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


async def seed(db, *, client_ref="gallery-request"):
    aid, job_id = uuid.uuid4(), str(uuid.uuid4())
    async with db[0]() as session:
        await session.execute(sa.insert(accounts_t).values(id=aid, flags={}))
        await session.commit()
    await credits.credit(aid, 1_000_000, "test funding", ref=f"seed:{job_id}")
    authorized = await credits.authorize_media(aid, MODEL, "image", 1, None, job_id,
                                               record_reservation=True, client_ref=client_ref)
    assert authorized["ok"] and authorized["reserved"] > 0
    values = dict(job_id=job_id, worker_id=str(uuid.uuid4()), wallet="", model=MODEL,
                  job_type="image", den=0.32, output_units=1, prompt_hash="b" * 64,
                  result_hash="c" * 64)
    return aid, job_id, values, authorized["reserved"]


async def terminal(values, result=None):
    return await credits.record_and_settle(ledger_values=values, exact=True,
                                           media_result=RESULT if result is None else result)


async def state(db, job_id):
    async with db[0]() as session:
        row = (await session.execute(sa.select(reservations).where(
            reservations.c.job_id == job_id))).mappings().one()
        count = await session.scalar(sa.select(sa.func.count()).select_from(ledger).where(
            ledger.c.job_id == uuid.UUID(job_id)))
        return row, count


@pytest.mark.asyncio
async def test_committed_result_survives_new_sessions_and_duplicate_terminal(db):
    aid, job_id, values, cost = await seed(db)
    pending = await media_results.recover(aid, client_ref="gallery-request")
    assert pending["state"] == "pending" and pending["result"] is None
    assert await terminal(values) == "settled"
    # There is no Redis or collector in this test. Every read opens a new session.
    recovered = await media_results.recover(aid, client_ref="gallery-request")
    assert recovered == {"job_id": job_id, "state": "completed", "actual_micro": cost,
                         "result": RESULT}
    changed = copy.deepcopy(RESULT)
    changed["media"][0]["url"] = "https://assets.example/duplicate.webp"
    assert await terminal(values, changed) == "duplicate"
    assert (await media_results.recover(aid, job_id=uuid.UUID(job_id))) == recovered
    assert await credits.get_balance(aid) == 1_000_000 - cost
    assert (await state(db, job_id))[1] == 1


@pytest.mark.asyncio
async def test_receipt_write_failure_rolls_back_ledger_and_settlement(db):
    aid, job_id, values, cost = await seed(db)
    # Fail after the payout insert, at the UPDATE which persists the receipt.
    from sqlalchemy import event
    engine = db[0].kw["bind"]
    def reject(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE") and "media_result" in statement:
            raise RuntimeError("receipt storage unavailable")
    event.listen(engine.sync_engine, "before_cursor_execute", reject)
    try:
        assert await terminal(values) == "error"
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", reject)
    row, count = await state(db, job_id)
    assert row["status"] == "held" and row["media_result"] is None and count == 0
    assert await credits.get_balance(aid) == 1_000_000 - cost


@pytest.mark.asyncio
async def test_release_before_late_success_never_exposes_output(db):
    aid, job_id, values, _ = await seed(db)
    await credits.release_job(job_id)
    assert await terminal(values) == "stale_no_payout"
    result = await media_results.recover(aid, client_ref="gallery-request")
    assert result["state"] == "closed_without_result" and result["result"] is None
    assert await credits.get_balance(aid) == 1_000_000
    assert (await state(db, job_id))[1] == 0


@pytest.mark.asyncio
async def test_owner_is_required_and_aliases_remain_recoverable(db):
    aid, job_id, values, _ = await seed(db)
    assert await terminal(values) == "settled"
    other = uuid.uuid4()
    assert await media_results.recover(other, job_id=uuid.UUID(job_id)) is None
    assert await media_results.recover(other, client_ref="gallery-request") is None
    assert await media_results.recover(None, client_ref="gallery-request") is None
    async with db[0]() as session:
        await session.execute(sa.insert(accounts_t).values(id=other, flags={}))
        await session.execute(sa.insert(account_aliases).values(
            source_account_id=aid, canonical_account_id=other, reason="proved test merge",
            merge_ref=f"test-merge:{aid}"))
        await session.commit()
    assert (await media_results.recover(other, job_id=uuid.UUID(job_id)))["result"] == RESULT


@pytest.mark.asyncio
async def test_reused_client_reference_is_ambiguous_not_a_dispatch_idempotency_key(db):
    aid, job_id, values, _ = await seed(db)
    assert await terminal(values) == "settled"
    async with db[0]() as session:
        await session.execute(sa.insert(reservations).values(
            job_id=str(uuid.uuid4()), account_id=aid, model=MODEL, reserved_micro=0,
            media_client_ref="gallery-request", status="held"))
        await session.commit()
    with pytest.raises(media_results.AmbiguousResult):
        await media_results.recover(aid, client_ref="gallery-request")
    assert (await media_results.recover(aid, job_id=uuid.UUID(job_id)))["state"] == "completed"


@pytest.mark.asyncio
async def test_invalid_or_oversized_result_does_not_charge(db):
    _, job_id, values, _ = await seed(db)
    for field, value in [("media", []), ("model", "other-model"), ("gen_time", float("nan")),
                         ("worker", "x" * 70000), ("prompt", "must not store")]:
        bad = {**RESULT, field: value}
        assert await terminal(values, bad) == "error"
        row, count = await state(db, job_id)
        assert row["media_result"] is None and row["status"] == "held" and count == 0


@pytest.mark.asyncio
async def test_pg_concurrent_success_and_release_keep_result_consistent(db):
    if db[1] != "postgres":
        pytest.skip("requires independent PostgreSQL transactions")
    aid, job_id, values, cost = await seed(db)
    from grid_api.services.tests.test_paid_validator_audit_postgres import blocked_race
    async def success():
        return await terminal(values)
    async def release():
        await credits.release_job(job_id)
        return "release"
    await blocked_race(db[0], job_id, [success] * 10 + [release] * 2)
    row, count = await state(db, job_id)
    assert row["status"] == "settled"
    if count:
        assert count == 1 and row["media_result"] == RESULT
        assert await credits.get_balance(aid) == 1_000_000 - cost
    else:
        assert row["media_result"] is None
        assert await credits.get_balance(aid) == 1_000_000


@pytest.mark.asyncio
async def test_http_recovery_requires_real_auth_delegation_and_ownership(db, monkeypatch):
    aid, job_id, values, _ = await seed(db)
    assert await terminal(values) == "settled"
    other = uuid.uuid4()
    async def resolve(key):
        if key == "service":
            return {"source": "v2", "key_kind": "service", "service_id": "gallery",
                    "account_id": other, "scopes": ["inference.submit"]}
        if key == "stranger":
            return {"source": "v2", "key_kind": "user", "account_id": other,
                    "scopes": ["inference.submit"]}
        return None
    def verify(token, *, audience):
        assert token == "delegated-test-token" and audience == "gallery"
        return {"sub": str(aid), "service_id": "gallery", "scopes": ["inference.submit"]}
    async def account_auth(subject, *, scopes):
        return {"account_id": uuid.UUID(subject), "scopes": scopes}
    monkeypatch.setattr(accounts, "resolve_api_key", resolve)
    monkeypatch.setattr(accounts, "_account_auth", account_auth)
    monkeypatch.setattr(user_tokens, "verify", verify)
    monkeypatch.setattr(router.limiter, "enabled", False)
    app = FastAPI()
    app.include_router(router.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/v1/media/results?job_id={job_id}"
        for key, status in [("invalid", 401), ("service", 401), ("stranger", 404)]:
            response = await client.get(url, headers={"apikey": key})
            assert response.status_code == status
            assert response.headers["cache-control"] == "no-store"
            assert "assets.example" not in response.text
        response = await client.get(url, headers={"apikey": "service", "X-Grid-User-Token": "delegated-test-token"})
        assert response.status_code == 200 and response.json()["result"] == RESULT
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_worker_commits_result_before_done_and_worker_ack(db, monkeypatch):
    from grid_api.routers import worker_ws
    aid, job_id, values, _ = await seed(db)
    events = []
    async def done(reported_job_id, full_text):
        assert reported_job_id == job_id
        result = await media_results.recover(aid, job_id=uuid.UUID(job_id))
        assert result["state"] == "completed"
        assert result["result"] == json.loads(full_text)
        events.append("done")
    class Socket:
        async def send_json(self, value):
            if value["type"] == "ack":
                assert (await media_results.recover(aid, client_ref="gallery-request"))["state"] == "completed"
                events.append("ack")
        async def receive_json(self):
            return {"type": "done", "results": [{"index": 0, "sha256": "a" * 64, "seed": 12}]}
    monkeypatch.setattr(worker_ws.storage, "presign_outputs", lambda *_a, **_k: [{
        "public_url": RESULT["media"][0]["url"], "key": "job.webp",
        "put_url": "https://upload.example/slot", "content_type": "image/webp"}])
    monkeypatch.setattr(worker_ws.storage, "uploaded_outputs_present", lambda *_a, **_k: True)
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", done)
    assert await worker_ws._handle_media_job(Socket(), {
        "job_id": job_id, "job_type": "image", "payload": {"n": 1, "steps": 4},
    }, MODEL, values["worker_id"], {"name": "media-test", "wallet_address": ""})
    assert events == ["done", "ack"]


@pytest.mark.asyncio
async def test_lost_submit_response_keeps_owner_correlation_and_hold(db, monkeypatch):
    from grid_api.services import media
    aid, _, _, _ = await seed(db, client_ref="unrelated-seed")
    async def lost_response(*_args, **kwargs):
        kwargs["dispatch_state"]["attempted"] = True
        raise TimeoutError("reply lost after dispatch")
    monkeypatch.setattr(media, "_submit_and_wait_inner", lost_response)
    with pytest.raises(TimeoutError):
        await media.submit_and_wait(MODEL, "image", {"n": 1}, 30,
                                    account_id=aid, progress_token="survives-gallery-restart")
    recovered = await media_results.recover(aid, client_ref="survives-gallery-restart")
    assert recovered["state"] == "pending" and recovered["result"] is None
    row, count = await state(db, recovered["job_id"])
    assert row["status"] == "held" and count == 0


@pytest.mark.asyncio
async def test_migration_roundtrip_and_refusal_to_delete_recovery_data(db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0040_media_result_recovery.py"
    spec = importlib.util.spec_from_file_location("media_recovery_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = db[0].kw["bind"]
    namespace = engine.get_execution_options().get("schema_translate_map", {}).get(None)
    def roundtrip(connection):
        if namespace:
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{namespace}"')
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
        inspector = sa.inspect(connection)
        columns = {c["name"]: c for c in inspector.get_columns(reservations.name, schema=namespace)}
        assert columns["media_result"]["nullable"] and columns["media_client_ref"]["nullable"]
        assert "ix_grid_reservations_media_owner_ref" in {
            index["name"] for index in inspector.get_indexes(reservations.name, schema=namespace)}
    async with engine.begin() as connection:
        await connection.run_sync(roundtrip)
    _, job_id, values, _ = await seed(db)
    assert await terminal(values) == "settled"
    def refuse(connection):
        if namespace:
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{namespace}"')
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="Cannot discard"):
                migration.downgrade()
    async with engine.begin() as connection:
        await connection.run_sync(refuse)
    assert (await state(db, job_id))[0]["media_result"] == RESULT

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private async foundation: real holds, SQL races and disposable Redis loss.

GPU/storage/pricing remain synthetic. These tests do not prove a running worker
can resume a Comfy prompt after a Core crash, or that public async admission exists.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from redis.asyncio import Redis
from redis.exceptions import ConnectionError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.routers import worker_ws
from grid_api.services import credits, job_queue, pricing, recipes
from grid_api.services import media_jobs as jobs
from grid_api.services.recipe_import import recipe_root
from grid_api.v2.schema import account_aliases, accounts, credit_ledger, ledger, media_jobs, metadata, reservations

ROOT = Path(__file__).resolve().parents[3]
MODEL = "LTX-2.5 Distilled BF16"
OPENING = 1_000_000
INTENT_HASH = hashlib.sha256(b"synthetic normalized client intent").hexdigest()


def frozen_video_fixture(_slot, *, job_id, expected_sha256):
    return {"key": f"video/{job_id}/final-fixture.mp4", "url": f"https://assets.example/video/{job_id}/final-fixture.mp4",
            "sha256": expected_sha256, "bytes": 36, "content_type": "video/mp4"}


@pytest.fixture(autouse=True)
def synthetic_dependencies(monkeypatch):
    for name in ("_BY_ROOT", "_BY_NAME", "_BY_ID", "_BY_MODEL", "_LOCAL_RECIPES", "_ONCHAIN_RECIPES"):
        monkeypatch.setattr(recipes, name, {})
    for path in (ROOT / "recipes/qualification").glob("*.json"):
        graph = json.loads(path.read_text())
        recipes.register_recipe(recipe_root(graph), graph["_grid"]["name"], graph)
    monkeypatch.setitem(pricing.PRICING, MODEL.lower(), pricing.ModelPrice(0, 0, video_per_second=0.02))
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "on")
    monkeypatch.setattr(credits, "_promo_first", AsyncMock(return_value=0))
    monkeypatch.setattr(credits, "_free_first", AsyncMock(return_value=0))
    monkeypatch.setattr(credits.free_credits, "release", AsyncMock())
    async def no_discount(cost, **_kwargs):
        return cost
    monkeypatch.setattr(credits, "apply_holder_discount", no_discount)


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def db(request, monkeypatch):
    namespace = "media_journal_test_" + uuid4().hex
    postgres = request.param == "postgres"
    if postgres:
        url = os.environ.get("CREDITS_TEST_DB_URL", "")
        if not url.startswith("postgresql+asyncpg://"):
            pytest.skip("CREDITS_TEST_DB_URL required for real PostgreSQL concurrency proof")
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": namespace}},
                                     pool_size=5, max_overflow=30)
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
        yield factory, request.param
    finally:
        try:
            if postgres:
                async with engine.begin() as connection:
                    await connection.execute(sa.schema.DropSchema(namespace, cascade=True, if_exists=True))
        finally:
            await engine.dispose()


@pytest_asyncio.fixture
async def redis(monkeypatch):
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server required for lost queue-reply proof")
    with tempfile.TemporaryDirectory(prefix="aipg-mj-", dir="/tmp") as directory:
        path = f"{directory}/redis.sock"
        process = subprocess.Popen(
            [binary, "--port", "0", "--unixsocket", path, "--unixsocketperm", "700",
             "--save", "", "--appendonly", "no", "--dir", directory],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        client = Redis(unix_socket_path=path, decode_responses=True, socket_timeout=3)
        try:
            for _ in range(100):
                assert process.poll() is None
                try:
                    if await client.ping():
                        break
                except ConnectionError:
                    await asyncio.sleep(0.02)
            else:
                pytest.fail("disposable Redis did not start")
            await client.xgroup_create(job_queue.MEDIA_STREAM_KEY, job_queue.CONSUMER_GROUP, id="0", mkstream=True)
            monkeypatch.setattr(job_queue, "get_redis", lambda: client)
            yield client
        finally:
            await client.aclose()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


async def owner(amount=OPENING):
    aid = uuid4()
    async with await database.new_session() as session:
        await session.execute(accounts.insert().values(id=aid, flags={}))
        if amount:
            await credits._credit_in_session(session, aid, amount, "usdc_deposit", f"deposit:{aid}", funded_micro=amount)
        await session.commit()
    return aid


def prepared(aid, *, key=None, request_hash=INTENT_HASH, seed=0, cap=100_000):
    resolved = recipes.resolve_for_model(MODEL, {"seed": seed, "prompt": "synthetic video"}, has_source=False)
    execution = resolved["execution"]
    payload = {**execution, "n": 1, "prompt": "synthetic video", "seed": resolved["seed"],
               "recipe_engine": resolved["engine"], "recipe_spec": resolved["spec"],
               "recipe_root": resolved["recipe_root"], "ext": "mp4"}
    return jobs.prepare(account_id=aid, idempotency_key=str(key or uuid4()), request_hash=request_hash,
                        model=MODEL, payload=payload, route_models=resolved["required_models"], max_cost_micro=cap)


async def counts():
    async with await database.new_session() as session:
        return tuple([await session.scalar(sa.select(sa.func.count()).select_from(table))
                      for table in (media_jobs, reservations, ledger)])


async def snapshot(job):
    async with await database.new_session() as session:
        journal = (await session.execute(sa.select(media_jobs).where(media_jobs.c.job_id == job.job_id))).mappings().one()
        holds = (await session.execute(sa.select(reservations).where(reservations.c.job_id == job.job_id))).mappings().one()
        debits = (await session.execute(sa.select(credit_ledger).where(credit_ledger.c.ref == job.job_id))).mappings().all()
    return dict(journal), dict(holds), debits


async def claim(job, **changes):
    args = dict(job_id=job.job_id, payload=job.payload, route_models=job.route_models,
                preferred_worker=job.preferred_worker, selected_model=job.route_models[0],
                worker_id=uuid4(), job_type="video", execution_token=str(uuid4()))
    return await jobs.claim_execution(**{**args, **changes})


@pytest.mark.asyncio
async def test_reserve_replay_keeps_first_seed_and_one_funded_hold(db):
    aid, key = await owner(), uuid4()
    first = prepared(aid, key=key, seed=10)
    stored = await jobs.reserve(first)
    second = prepared(aid, key=key, seed=99)
    replayed = await jobs.reserve(second)
    assert stored == replayed and stored["payload"]["seed"] == 10
    assert first.job_id == second.job_id
    assert await counts() == (1, 1, 0)
    _, hold, debits = await snapshot(first)
    assert len(debits) == 1 and -debits[0]["delta_micro"] == hold["reserved_micro"]
    assert debits[0]["funded_delta_micro"] == debits[0]["delta_micro"]
    assert await credits.get_balance(aid) == OPENING - hold["reserved_micro"]
    assert credits._promo_first.await_count == credits._free_first.await_count == 1


@pytest.mark.asyncio
async def test_changed_body_conflicts_before_pocket_draws(db):
    aid, key = await owner(), uuid4()
    await jobs.reserve(prepared(aid, key=key))
    with pytest.raises(jobs.MediaJobConflict):
        await jobs.reserve(prepared(aid, key=key, request_hash="f" * 64))
    assert await counts() == (1, 1, 0)
    assert credits._promo_first.await_count == credits._free_first.await_count == 1


@pytest.mark.asyncio
async def test_journal_insert_failure_rolls_back_debit_and_hold(db, monkeypatch):
    aid = await owner()
    job = prepared(aid)
    monkeypatch.setattr(jobs, "insert_in_session", AsyncMock(side_effect=RuntimeError("synthetic write outage")))
    with pytest.raises(jobs.MediaJobPaymentRequired, match="reservation_failed"):
        await jobs.reserve(job)
    assert await counts() == (0, 0, 0)
    assert await credits.get_balance(aid) == OPENING
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(credit_ledger).where(
            credit_ledger.c.ref == job.job_id)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "unpriced", "cost_limit", "insufficient"])
async def test_async_never_authorizes_free_or_over_budget_jobs(db, monkeypatch, mode):
    aid = await owner(amount=0 if mode == "insufficient" else OPENING)
    job = prepared(aid, cap=1 if mode == "cost_limit" else 100_000)
    if mode == "off":
        monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    if mode == "unpriced":
        monkeypatch.setattr(pricing, "is_priced_for", lambda *_args: False)
    with pytest.raises(jobs.MediaJobPaymentRequired):
        await jobs.reserve(job)
    assert await counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_same_key_on_another_account_is_independent_and_private(db):
    first, second, key = await owner(), await owner(), uuid4()
    a, b = prepared(first, key=key), prepared(second, key=key)
    await jobs.reserve(a)
    await jobs.reserve(b)
    assert a.job_id != b.job_id and await counts() == (2, 2, 0)
    assert await jobs.recover(second, a.job_id) is None
    with pytest.raises(jobs.MediaJobError, match="billing principal"):
        await jobs.reserve(prepared(first), user={"account_id": second})


@pytest.mark.asyncio
async def test_proved_alias_can_recover_but_merged_duplicate_keys_are_ambiguous(db):
    first, second, key = await owner(), await owner(), uuid4()
    a = prepared(first, key=key)
    await jobs.reserve(a)
    async with await database.new_session() as session:
        await session.execute(account_aliases.insert().values(source_account_id=first, canonical_account_id=second,
                                                             merge_ref="synthetic-merge"))
        await session.commit()
    assert (await jobs.recover(second, a.job_id))["job_id"] == a.job_id
    replay = await jobs.lookup_request(second, str(key), INTENT_HASH)
    assert replay["job_id"] == a.job_id
    # Construct an earlier independently enrolled row, not a new ambiguous admission.
    b = prepared(second, key=key)
    authorized = await credits.authorize_media(second, MODEL, "video", 1, b.seconds, b.job_id,
        record_reservation=True, max_cost_micro_usd=b.max_cost_micro, prepared_media_job=b)
    assert authorized["ok"]
    with pytest.raises(jobs.MediaJobConflict, match="ambiguous"):
        await jobs.lookup_request(second, str(key), INTENT_HASH)


@pytest.mark.asyncio
async def test_execution_is_irrevocable_and_changed_queue_fields_cannot_dispatch(db):
    job = prepared(await owner())
    await jobs.reserve(job)
    assert await claim(job) == "claimed"
    assert await claim(job) == "duplicate"
    assert (await jobs.recover(job.account_id, job.job_id))["state"] == "execution_uncertain"
    changed = job.payload
    changed["seed"] = 123
    for kwargs in ({"payload": changed}, {"route_models": ["another-model"]},
                   {"preferred_worker": "unapproved"}, {"selected_model": "another-model"},
                   {"job_type": "image"}):
        with pytest.raises(jobs.MediaJobError):
            await claim(job, **kwargs)
    await credits.release_job(job.job_id)
    assert await claim(job) == "closed"
    assert await credits.get_balance(job.account_id) == OPENING


@pytest.mark.asyncio
async def test_result_uses_existing_atomic_terminal_and_survives_disabled_charging(db, monkeypatch):
    job = prepared(await owner())
    await jobs.reserve(job)
    assert await claim(job) == "claimed"
    journal = (await snapshot(job))[0]
    output = {"media": [{"url": "https://assets.example/video.mp4", "key": "video.mp4", "sha256": "a" * 64, "seed": 0}],
              "model": job.route_models[0], "worker": "test-worker", "gen_time": 1.0, "recipe_root": job.payload["recipe_root"]}
    values = dict(job_id=job.job_id, worker_id=str(journal["execution_worker_id"]), wallet="", model=job.route_models[0],
                  job_type="video", den=1.0, output_units=81, prompt_hash="b" * 64, result_hash="c" * 64)
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    token = (await snapshot(job))[0]["execution_token"]
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_result=output,
                                           media_execution_token=token) == "settled"
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_result=output,
                                           media_execution_token=token) == "duplicate"
    recovered = await jobs.recover(job.account_id, job.job_id)
    assert recovered["state"] == "completed" and recovered["result"] == output
    assert recovered["actual_micro"] == recovered["reserved_micro"]
    assert await counts() == (1, 1, 1)
    assert await claim(job) == "closed"
    assert await jobs.reserve(job)  # Idempotent recovery is not new charging admission.


@pytest.mark.asyncio
async def test_duplicate_and_invalid_journal_jobs_never_reach_ws_or_presign(db, monkeypatch):
    job = prepared(await owner())
    await jobs.reserve(job)
    assert await claim(job) == "claimed"
    socket = AsyncMock()
    presign = AsyncMock()
    monkeypatch.setattr(worker_ws.storage, "presign_outputs", presign)
    queue_job = {"job_id": job.job_id, "job_type": "video", "payload": job.payload, "models": job.route_models}
    assert await worker_ws._handle_media_job(socket, queue_job, job.route_models[0], str(uuid4()), {}) is True
    socket.send_json.assert_not_awaited()
    presign.assert_not_called()
    bad = {**queue_job, "payload": {**job.payload, jobs.MARKER: True}}
    assert await worker_ws._handle_media_job(socket, bad, job.route_models[0], str(uuid4()), {}) is False
    missing = {**queue_job, "job_id": str(uuid4())}
    assert await worker_ws._handle_media_job(socket, missing, job.route_models[0], str(uuid4()), {}) is False
    socket.send_json.assert_not_awaited()
    presign.assert_not_called()


@pytest.mark.asyncio
async def test_presign_failure_releases_once_and_never_clears_execution(db, monkeypatch):
    job = prepared(await owner())
    await jobs.reserve(job)
    socket = AsyncMock()
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("storage outage")
    monkeypatch.setattr(worker_ws.storage, "presign_outputs", unavailable)
    monkeypatch.setattr(worker_ws.token_stream, "publish_error", AsyncMock())
    queue_job = {"job_id": job.job_id, "job_type": "video", "payload": job.payload, "models": job.route_models}
    assert await worker_ws._handle_media_job(socket, queue_job, job.route_models[0], str(uuid4()), {}) is True
    assert await claim(job) == "closed"
    assert await credits.get_balance(job.account_id) == OPENING
    socket.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_worker_terminal_commits_result_before_done_and_never_rerenders(db, monkeypatch):
    job = prepared(await owner())
    await jobs.reserve(job)
    messages = []
    async def committed_done(job_id, full_text):
        recovered = await jobs.recover(job.account_id, job_id)
        assert recovered["state"] == "completed" and recovered["result"] == json.loads(full_text)
        messages.append("done")
    class Socket:
        async def send_json(self, message):
            if message["type"] == "job":
                assert (await snapshot(job))[0]["execution_started"] is not None
            if message["type"] == "ack":
                assert (await jobs.recover(job.account_id, job.job_id))["state"] == "completed"
            messages.append(message["type"])
        async def receive_json(self):
            return {"type": "done", "id": job.job_id, "results": [{"index": 0, "sha256": "a" * 64, "seed": 0}]}
    monkeypatch.setattr(worker_ws.storage, "presign_outputs", lambda *_a, **_k: [{
        "public_url": "https://assets.example/video.mp4", "key": "video.mp4",
        "put_url": "https://upload.example/slot", "content_type": "video/mp4"}])
    monkeypatch.setattr(worker_ws.storage, "uploaded_outputs_present", lambda *_a, **_k: True)
    monkeypatch.setattr(worker_ws.video_output, "freeze", frozen_video_fixture)
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", committed_done)
    monkeypatch.setattr(worker_ws.route_events, "capture_route", lambda **_k: None)
    monkeypatch.setattr(worker_ws.route_events, "capture_outcome", lambda **_k: None)
    monkeypatch.setattr(worker_ws, "record_job_complete", lambda **_k: None)
    queued = {"job_id": job.job_id, "job_type": "video", "payload": job.payload, "models": job.route_models}
    assert await worker_ws._handle_media_job(Socket(), queued, job.route_models[0], str(uuid4()), {}) is True
    assert messages == ["job", "done", "ack"]
    assert await worker_ws._handle_media_job(Socket(), queued, job.route_models[0], str(uuid4()), {}) is True
    assert messages == ["job", "done", "ack"]
    assert await counts() == (1, 1, 1)
    result = (await jobs.recover(job.account_id, job.job_id))["result"]
    assert result["media"][0]["key"] == f"video/{job.job_id}/final-fixture.mp4"
    assert result["media"][0]["url"].endswith("final-fixture.mp4")
    async with await database.new_session() as session:
        row = (await session.execute(sa.select(ledger).where(ledger.c.job_id == worker_ws.ledger_svc.as_uuid(job.job_id)))).mappings().one()
    assert row["result_hash"] == worker_ws.ledger_svc.canonical_hash(
        worker_ws._media_result_commitment("video", job.payload, result["media"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["invalid", "uncertain", "superseded"])
async def test_video_freezing_failure_never_pays_or_publishes_success(db, monkeypatch, outcome):
    job, worker = prepared(await owner()), uuid4()
    await jobs.reserve(job)
    socket = AsyncMock()
    socket.receive_json.return_value = {"type": "done", "id": job.job_id,
                                      "results": [{"index": 0, "sha256": "a" * 64, "seed": 0}]}
    monkeypatch.setattr(worker_ws.storage, "presign_outputs", lambda *_a, **_k: [{
        "key": "fixture-only-put-slot", "public_url": "https://assets.example/mutable.mp4",
        "put_url": "https://upload.example/slot", "content_type": "video/mp4"}])
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", AsyncMock())
    monkeypatch.setattr(worker_ws.token_stream, "publish_error", AsyncMock())
    monkeypatch.setattr(worker_ws.route_events, "capture_route", lambda **_k: None)
    monkeypatch.setattr(worker_ws.route_events, "capture_outcome", lambda **_k: None)
    monkeypatch.setattr(worker_ws, "record_job_complete", AsyncMock())
    if outcome == "superseded":
        assert await claim(job, worker_id=worker) == "claimed"
        old_token = (await snapshot(job))[0]["execution_token"]
        await expire_execution(job.job_id)
        assert await resume(worker) is not None
        current_token = (await snapshot(job))[0]["execution_token"]
        queued = {"job_id": job.job_id, "job_type": "video", "payload": job.payload,
                  "_media_execution_token": old_token}
        monkeypatch.setattr(worker_ws.video_output, "freeze", frozen_video_fixture)
        assert await worker_ws._dispatch_media_job(socket, queued, MODEL, str(worker), {}) is False
        assert (await snapshot(job))[0]["execution_token"] == current_token
        worker_ws.token_stream.publish_error.assert_not_awaited()
    else:
        def fail(*_args, **_kwargs):
            if outcome == "invalid":
                raise worker_ws.video_output.InvalidVideoOutput("synthetic hash mismatch")
            raise TimeoutError("synthetic R2 outage")
        monkeypatch.setattr(worker_ws.video_output, "freeze", fail)
        queued = {"job_id": job.job_id, "job_type": "video", "payload": job.payload, "models": job.route_models}
        assert await worker_ws._handle_media_job(socket, queued, MODEL, str(worker), {}) is (outcome == "invalid")
        worker_ws.token_stream.publish_error.assert_awaited_once()
    worker_ws.token_stream.publish_done.assert_not_awaited()
    worker_ws.record_job_complete.assert_not_called()
    assert [message.args[0]["type"] for message in socket.send_json.await_args_list] == ["job"]
    assert await counts() == (1, 1, 0)
    row = (await snapshot(job))[1]
    assert row["status"] == ("settled" if outcome == "invalid" else "held")
    assert row["media_result"] is None
    balance = await credits.get_balance(job.account_id)
    assert balance == (OPENING if outcome == "invalid" else OPENING - row["reserved_micro"])


@pytest.mark.asyncio
async def test_dispatch_respects_attempt_cap_and_refund_even_after_lease_commit(db, monkeypatch):
    job = prepared(await owner())
    await jobs.reserve(job)
    send = AsyncMock(return_value="123-0")
    monkeypatch.setattr(job_queue, "submit_job", send)
    async with await database.new_session() as session:
        await session.execute(media_jobs.update().where(media_jobs.c.job_id == job.job_id).values(
            dispatch_attempts=jobs.MAX_DISPATCH_ATTEMPTS))
        await session.commit()
    assert await jobs.dispatch_one(job.job_id) is False
    send.assert_not_awaited()
    await credits.release_job(job.job_id)
    assert await jobs.dispatch_one(job.job_id) is False
    send.assert_not_awaited()
    assert await credits.get_balance(job.account_id) == OPENING


@pytest.mark.asyncio
async def test_lost_sql_commit_reply_never_sends_a_job_or_clears_claim(db, monkeypatch):
    job = prepared(await owner())
    await jobs.reserve(job)
    async def commit_reply_lost_session():
        session = await database.new_session()
        commit = session.commit
        async def reply_lost():
            await commit()
            raise TimeoutError("synthetic lost database commit reply")
        session.commit = reply_lost
        return session
    socket = AsyncMock()
    queued = {"job_id": job.job_id, "job_type": "video", "payload": job.payload, "models": job.route_models}
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "new_session", commit_reply_lost_session)
        assert await worker_ws._handle_media_job(socket, queued, job.route_models[0], str(uuid4()), {}) is False
    assert (await snapshot(job))[0]["execution_started"] is not None
    assert await worker_ws._handle_media_job(socket, queued, job.route_models[0], str(uuid4()), {}) is True
    socket.send_json.assert_not_awaited()
    assert (await jobs.recover(job.account_id, job.job_id))["state"] == "execution_uncertain"
    # A deadline refund uses the existing authority, even with new charging off.
    async with await database.new_session() as session:
        await session.execute(reservations.update().where(reservations.c.job_id == job.job_id).values(
            created=datetime.now(UTC) - timedelta(hours=2)))
        await session.commit()
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    assert await credits.sweep_stale_reservations() == 1
    assert (await jobs.recover(job.account_id, job.job_id))["state"] == "closed_without_result"
    assert await claim(job) == "closed"
    assert await credits.get_balance(job.account_id) == OPENING
    assert await counts() == (1, 1, 0)


@pytest.mark.asyncio
async def test_lost_xadd_reply_retries_same_payload_but_only_one_execution(db, redis, monkeypatch):
    job = prepared(await owner())
    await jobs.reserve(job)
    submit = job_queue.submit_job
    async def accepted_but_reply_lost(*args, **kwargs):
        await submit(*args, **kwargs)
        raise TimeoutError("synthetic lost XADD reply")
    monkeypatch.setattr(job_queue, "submit_job", accepted_but_reply_lost)
    with pytest.raises(TimeoutError):
        await jobs.dispatch_one(job.job_id)
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 1
    assert await jobs.dispatch_one(job.job_id) is False  # Unexpired lease.
    async with await database.new_session() as session:
        await session.execute(media_jobs.update().where(media_jobs.c.job_id == job.job_id).values(
            dispatch_deadline=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()
    monkeypatch.setattr(job_queue, "submit_job", submit)
    assert await jobs.dispatch_one(job.job_id) is True
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 2
    deliveries = await redis.xrange(job_queue.MEDIA_STREAM_KEY)
    assert deliveries[0][1]["payload"] == deliveries[1][1]["payload"]
    first = await job_queue.pop_job(str(uuid4()), timeout_ms=1, job_types=["video"])
    second = await job_queue.pop_job(str(uuid4()), timeout_ms=1, job_types=["video"])
    outcomes = []
    for queued in (first, second):
        outcomes.append(await jobs.claim_execution(job_id=queued["job_id"], payload=queued["payload"],
            route_models=queued["models"], preferred_worker=queued["preferred_worker"],
            selected_model=queued["models"][0], worker_id=uuid4(), job_type=queued["job_type"],
            execution_token=str(uuid4())))
    assert outcomes == ["claimed", "duplicate"]
    assert await counts() == (1, 1, 0)


async def expire_dispatch(job_id):
    async with await database.new_session() as session:
        await session.execute(media_jobs.update().where(media_jobs.c.job_id == job_id).values(
            dispatch_deadline=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()


@pytest.mark.asyncio
async def test_outbox_recovers_deleted_queue_but_never_republishes_claimed_work(db, redis, monkeypatch):
    job = prepared(await owner())
    await jobs.reserve(job)
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    assert await jobs.dispatch_batch() == {"queued": 1, "exhausted": 0, "errors": 0}
    assert await jobs.dispatch_batch() == {"queued": 0, "exhausted": 0, "errors": 0}
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 1
    await redis.delete(job_queue.MEDIA_STREAM_KEY)
    await expire_dispatch(job.job_id)
    assert await jobs.dispatch_batch() == {"queued": 1, "exhausted": 0, "errors": 0}
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 1
    assert await claim(job) == "claimed"
    await expire_dispatch(job.job_id)
    assert await jobs.dispatch_batch() == {"queued": 0, "exhausted": 0, "errors": 0}
    assert (await snapshot(job))[1]["status"] == "held"
    assert await counts() == (1, 1, 0)


@pytest.mark.asyncio
async def test_outbox_exhaustion_refunds_only_unstarted_requests(db, monkeypatch):
    job = prepared(await owner())
    await jobs.reserve(job)
    submit = AsyncMock(side_effect=TimeoutError("synthetic uncertain queue reply"))
    monkeypatch.setattr(job_queue, "submit_job", submit)
    for _ in range(jobs.MAX_DISPATCH_ATTEMPTS):
        assert await jobs.dispatch_batch() == {"queued": 0, "exhausted": 0, "errors": 1}
        await expire_dispatch(job.job_id)
    assert await jobs.dispatch_batch() == {"queued": 0, "exhausted": 1, "errors": 0}
    assert await jobs.dispatch_batch() == {"queued": 0, "exhausted": 0, "errors": 0}
    assert submit.await_count == jobs.MAX_DISPATCH_ATTEMPTS
    assert (await snapshot(job))[1]["status"] == "settled"
    assert await credits.get_balance(job.account_id) == OPENING
    assert await counts() == (1, 1, 0)
    # A claimed execution is not exhausted/released even if its attempt count
    # and lease would otherwise select it. Terminal/sweeper authority is separate.
    other = prepared(await owner())
    await jobs.reserve(other)
    assert await claim(other) == "claimed"
    async with await database.new_session() as session:
        await session.execute(media_jobs.update().where(media_jobs.c.job_id == other.job_id).values(
            dispatch_attempts=jobs.MAX_DISPATCH_ATTEMPTS))
        await session.commit()
    assert await jobs.dispatch_batch() == {"queued": 0, "exhausted": 0, "errors": 0}
    assert (await snapshot(other))[1]["status"] == "held"


@pytest.mark.asyncio
async def test_outbox_failure_does_not_skip_other_rows(db, monkeypatch):
    first, second = prepared(await owner()), prepared(await owner())
    await jobs.reserve(first)
    await jobs.reserve(second)

    async def submit(job_id, *_args, **_kwargs):
        if job_id == first.job_id:
            raise RuntimeError("synthetic private queue details")
        return "123-1"

    monkeypatch.setattr(job_queue, "submit_job", submit)
    assert await jobs.dispatch_batch(limit=2) == {"queued": 1, "exhausted": 0, "errors": 1}
    assert (await snapshot(first))[0]["dispatch_state"] == "dispatching"
    assert (await snapshot(second))[0]["dispatch_state"] == "queued"


@pytest.mark.asyncio
async def test_pg_competing_outbox_schedulers_take_one_lease(db, redis):
    if db[1] != "postgres":
        pytest.skip("SQLite is not evidence for competing outbox schedulers")
    job = prepared(await owner())
    await jobs.reserve(job)
    outcomes = await asyncio.wait_for(asyncio.gather(*(jobs.dispatch_batch(limit=1) for _ in range(12))), timeout=30)
    assert sum(item["queued"] for item in outcomes) == 1
    assert not sum(item["errors"] for item in outcomes)
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 1
    assert (await snapshot(job))[0]["dispatch_attempts"] == 1


@pytest.mark.asyncio
async def test_pg_concurrent_requests_and_claims_are_single_winner(db):
    if db[1] != "postgres":
        pytest.skip("SQLite is not evidence for this concurrency invariant")
    aid, key = await owner(), uuid4()
    candidates = [prepared(aid, key=key, seed=index) for index in range(25)]
    rows = await asyncio.wait_for(asyncio.gather(*(jobs.reserve(job) for job in candidates)), timeout=30)
    assert all(row == rows[0] for row in rows)
    assert await counts() == (1, 1, 0)
    winning_payload = rows[0]["payload"]
    job = next(item for item in candidates if item.payload == winning_payload)
    outcomes = await asyncio.wait_for(asyncio.gather(*(claim(job) for _ in range(25))), timeout=30)
    assert outcomes.count("claimed") == 1 and outcomes.count("duplicate") == 24
    assert credits._promo_first.await_count == credits._free_first.await_count == 1
    assert len((await snapshot(job))[2]) == 1


@pytest.mark.asyncio
async def test_pg_conflicting_requests_do_not_take_a_second_hold(db):
    if db[1] != "postgres":
        pytest.skip("SQLite is not evidence for this concurrency invariant")
    aid, key = await owner(), uuid4()
    candidates = [prepared(aid, key=key, request_hash=hashlib.sha256(str(i).encode()).hexdigest()) for i in range(12)]
    outcomes = await asyncio.wait_for(asyncio.gather(*(jobs.reserve(job) for job in candidates), return_exceptions=True), timeout=30)
    assert sum(isinstance(result, dict) for result in outcomes) == 1
    assert sum(isinstance(result, jobs.MediaJobConflict) for result in outcomes) == 11
    assert await counts() == (1, 1, 0)
    assert credits._promo_first.await_count == credits._free_first.await_count == 1


@pytest.mark.asyncio
async def test_pg_refund_vs_execution_race_and_late_success_never_mint_payout(db):
    if db[1] != "postgres":
        pytest.skip("SQLite is not evidence for this concurrency invariant")
    job = prepared(await owner())
    await jobs.reserve(job)
    outcomes = await asyncio.wait_for(asyncio.gather(claim(job), credits.release_job(job.job_id)), timeout=30)
    assert outcomes[0] in {"claimed", "closed"}
    assert await claim(job) == "closed"
    values = dict(job_id=job.job_id, worker_id=str(uuid4()), wallet="", model=job.route_models[0],
                  job_type="video", den=1.0, output_units=81, prompt_hash="b" * 64, result_hash="c" * 64)
    assert await credits.record_and_settle(ledger_values=values, exact=True) == "execution_superseded"
    assert await credits.get_balance(job.account_id) == OPENING
    assert await counts() == (1, 1, 0)


@pytest.mark.asyncio
async def test_queue_giveup_cannot_refund_another_delivery_running_the_job(db):
    job = prepared(await owner())
    await jobs.reserve(job)
    assert await claim(job) == "claimed"
    queued = {"job_id": job.job_id, "payload": job.payload}
    await worker_ws._release_undispatched_job(queued)
    assert (await snapshot(job))[1]["status"] == "held"
    other = prepared(job.account_id)
    await jobs.reserve(other)
    await worker_ws._release_undispatched_job({"job_id": other.job_id, "payload": other.payload})
    assert await claim(other) == "closed"
    assert await credits.get_balance(job.account_id) == OPENING - (await snapshot(job))[1]["reserved_micro"]


@pytest.mark.asyncio
async def test_pg_queue_giveup_vs_execution_is_atomic(db):
    if db[1] != "postgres":
        pytest.skip("SQLite is not evidence for this concurrency invariant")
    for _ in range(10):
        job = prepared(await owner())
        await jobs.reserve(job)
        outcomes = await asyncio.wait_for(asyncio.gather(
            claim(job), worker_ws._release_undispatched_job({"job_id": job.job_id, "payload": job.payload}),
        ), timeout=30)
        row, hold, _ = await snapshot(job)
        if outcomes[0] == "claimed":
            assert row["execution_started"] is not None and hold["status"] == "held"
        else:
            assert outcomes[0] == "closed" and row["execution_started"] is None and hold["status"] == "settled"
        await credits.release_job(job.job_id)  # Original terminal / sweeper can still refund.
        assert await credits.get_balance(job.account_id) == OPENING


@pytest.mark.parametrize("change", [{"max_cost_micro": True}, {"max_cost_micro": 0}, {"max_cost_micro": 2**63},
                                   {"request_hash": "invalid"}, {"job_id": str(uuid4())}])
def test_prepared_snapshot_rejects_invalid_identity_or_limits(change):
    job = prepared(uuid4())
    with pytest.raises(jobs.MediaJobError):
        jobs._validate_prepared(replace(job, **change))


def test_prepared_snapshot_is_immutable_and_bounded():
    job = prepared(uuid4())
    payload = job.payload
    payload["seed"] = 999
    assert job.payload["seed"] == 0
    with pytest.raises(jobs.MediaJobError):
        jobs.prepare(account_id=job.account_id, idempotency_key=str(uuid4()), request_hash=INTENT_HASH,
                     model=MODEL, payload={**job.payload, "prompt": "x" * jobs.MAX_PAYLOAD_BYTES},
                     route_models=job.route_models, max_cost_micro=100_000)
    with pytest.raises(jobs.MediaJobError):
        jobs._key_hash("a friendly retry key")
    for extra in ({"api_key": "synthetic-not-a-credential"}, {"nested": {"Authorization": "synthetic"}},
                  {"source_image_url": "data:image/png;base64,synthetic"}):
        with pytest.raises(jobs.MediaJobError):
            jobs.prepare(account_id=job.account_id, idempotency_key=str(uuid4()), request_hash=INTENT_HASH,
                         model=MODEL, payload={**job.payload, **extra},
                         route_models=job.route_models, max_cost_micro=100_000)


def lease_migration():
    path = ROOT / "alembic/versions/0044_media_execution_lease.py"
    spec = importlib.util.spec_from_file_location("media_lease_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


def test_migration_matches_schema_and_cannot_erase_execution_history():
    path = ROOT / "alembic/versions/0043_media_job_journal.py"
    spec = importlib.util.spec_from_file_location("media_journal_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    leases = lease_migration()
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            metadata.create_all(connection, tables=[table for table in metadata.sorted_tables if table != media_jobs])
            with Operations.context(MigrationContext.configure(connection)):
                revision.upgrade()
                leases.upgrade()
                inspector = sa.inspect(connection)
                actual = {column["name"]: column["nullable"] for column in inspector.get_columns(media_jobs.name)}
                assert actual == {column.name: column.nullable for column in media_jobs.columns}
                assert {c["name"] for c in inspector.get_check_constraints(media_jobs.name)} == {
                    c.name for c in media_jobs.constraints if isinstance(c, sa.CheckConstraint)}
                assert {c["name"] for c in inspector.get_unique_constraints(media_jobs.name)} == {"uq_grid_media_jobs_owner_key"}
                assert {c["name"] for c in inspector.get_indexes(media_jobs.name)} == {
                    "ix_grid_media_jobs_dispatch", "ix_grid_media_jobs_resume"}
                assert {c["referred_table"] for c in inspector.get_foreign_keys(media_jobs.name)} == {
                    "grid_reservations", "grid_accounts"}
                leases.downgrade()
                revision.downgrade()
                revision.upgrade()
                leases.upgrade()
                aid = uuid4()
                job = prepared(aid)
                connection.execute(accounts.insert().values(id=aid, flags={}))
                connection.execute(reservations.insert().values(job_id=job.job_id, account_id=aid, model=MODEL,
                    reserved_micro=1, prompt_toks=0, status="held", created=datetime.now(UTC)))
                connection.execute(media_jobs.insert().values(job_id=job.job_id, account_id=aid,
                    idempotency_hash=job.idempotency_hash, request_hash=job.request_hash, payload_hash=job.payload_hash,
                    payload=job.payload, model=MODEL, route_models=job.route_models, max_cost_micro=10,
                    created=datetime.now(UTC)))
                with pytest.raises(RuntimeError, match="refuse to discard"):
                    revision.downgrade()
                assert connection.scalar(sa.select(sa.func.count()).select_from(media_jobs)) == 1
                connection.execute(media_jobs.update().values(execution_worker_id=uuid4(), execution_started=datetime.now(UTC),
                    execution_model=MODEL, execution_token=str(uuid4()), execution_deadline=datetime.now(UTC)))
                with pytest.raises(RuntimeError, match="refuse to discard"):
                    leases.downgrade()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_migration_matches_actual_postgres_table_contract(db):
    if db[1] != "postgres":
        pytest.skip("PostgreSQL DDL proof requires disposable PostgreSQL")
    path = ROOT / "alembic/versions/0043_media_job_journal.py"
    spec = importlib.util.spec_from_file_location("media_journal_pg_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    leases = lease_migration()
    def migrated(connection):
        media_jobs.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            leases.upgrade()
            inspector = sa.inspect(connection)
            assert {c["name"]: c["nullable"] for c in inspector.get_columns(media_jobs.name)} == {
                column.name: column.nullable for column in media_jobs.columns}
            assert {c["name"] for c in inspector.get_check_constraints(media_jobs.name)} == {
                c.name for c in media_jobs.constraints if isinstance(c, sa.CheckConstraint)}
            assert {c["name"] for c in inspector.get_unique_constraints(media_jobs.name)} == {"uq_grid_media_jobs_owner_key"}
            assert {c["referred_table"] for c in inspector.get_foreign_keys(media_jobs.name)} == {
                "grid_reservations", "grid_accounts"}
            leases.downgrade()
            revision.downgrade()
            revision.upgrade()
            leases.upgrade()
    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(migrated)


async def expire_execution(job_id):
    async with await database.new_session() as session:
        await session.execute(
            media_jobs.update().where(media_jobs.c.job_id == job_id).values(execution_deadline=datetime.now(UTC) - timedelta(seconds=1)),
        )
        await session.commit()


async def resume(worker, model=MODEL, features=None):
    return await jobs.claim_resumption(
        worker_id=worker, models=[model], media_features=["async-video-resume-v1"] if features is None else features,
    )


def terminal_values(job, worker):
    return dict(
        job_id=job.job_id,
        worker_id=str(worker),
        wallet="",
        model=job.route_models[0],
        job_type="video",
        den=1.0,
        output_units=81,
        prompt_hash="b" * 64,
        result_hash="c" * 64,
    )


@pytest.mark.asyncio
async def test_resume_requires_original_worker_expired_lease_and_exact_capabilities(db, monkeypatch):
    job, worker, token = prepared(await owner(), seed=17), uuid4(), str(uuid4())
    await jobs.reserve(job)
    assert await claim(job, worker_id=worker, execution_token=token) == "claimed"
    assert await resume(worker) is None
    await expire_execution(job.job_id)
    assert await resume(uuid4()) is None
    assert await resume(worker, model="another-model") is None
    assert await resume(worker, features=[]) is None
    async with await database.new_session() as session:
        # Simulate a row predating 0044: no authority is fabricated from its graph.
        await session.execute(
            media_jobs.update()
            .where(media_jobs.c.job_id == job.job_id)
            .values(execution_token=None, execution_model=None, execution_deadline=None),
        )
        await session.commit()
    assert await resume(worker) is None
    async with await database.new_session() as session:
        await session.execute(
            media_jobs.update()
            .where(media_jobs.c.job_id == job.job_id)
            .values(execution_token=token, execution_model=MODEL, execution_deadline=datetime.now(UTC) - timedelta(seconds=1)),
        )
        await session.commit()
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    recovered = await resume(worker)
    assert recovered["job_id"] == job.job_id and recovered["payload"] == job.payload
    assert recovered["selected_model"] == job.route_models[0]
    assert recovered["execution_token"] != token and recovered["payload"]["seed"] == 17
    assert await counts() == (1, 1, 0) and len((await snapshot(job))[2]) == 1
    assert (await snapshot(job))[0]["execution_worker_id"] == worker
    assert await resume(worker) is None


@pytest.mark.asyncio
async def test_reconnect_cannot_bypass_required_multi_pin_feature(db):
    worker = uuid4()
    base = prepared(await owner())
    job = jobs.prepare(
        account_id=base.account_id,
        idempotency_key=str(uuid4()),
        request_hash=INTENT_HASH,
        model=MODEL,
        payload={**base.payload, "required_media_features": ["recipe-image-bindings-v1", "async-video-resume-v1"]},
        route_models=base.route_models,
        max_cost_micro=base.max_cost_micro,
    )
    await jobs.reserve(job)
    assert await claim(job, worker_id=worker) == "claimed"
    await expire_execution(job.job_id)
    assert await resume(worker) is None
    assert await resume(worker, features=["recipe-image-bindings-v1", "async-video-resume-v1"]) is not None


@pytest.mark.asyncio
async def test_old_connection_cannot_settle_refund_or_detach_successor(db):
    job, worker, old = prepared(await owner()), uuid4(), str(uuid4())
    await jobs.reserve(job)
    assert await claim(job, worker_id=worker, execution_token=old) == "claimed"
    await expire_execution(job.job_id)
    new = (await resume(worker))["execution_token"]
    for token in (None, old, str(uuid4())):
        assert (
            await credits.record_and_settle(ledger_values=terminal_values(job, worker), exact=True, media_execution_token=token)
            == "execution_superseded"
        )
    assert await credits.release_job(job.job_id, media_execution_token=old) is False
    assert await jobs.renew_execution(job_id=job.job_id, worker_id=worker, execution_token=old) is False
    await jobs.detach_execution(job_id=job.job_id, execution_token=old)
    assert await resume(worker) is None
    assert (await snapshot(job))[1]["status"] == "held"
    assert await counts() == (1, 1, 0)
    assert (
        await credits.record_and_settle(ledger_values=terminal_values(job, uuid4()), exact=True, media_execution_token=new)
        == "execution_superseded"
    )
    assert (
        await credits.record_and_settle(
            ledger_values={**terminal_values(job, worker), "model": "wrong"}, exact=True, media_execution_token=new,
        )
        == "execution_superseded"
    )
    assert await credits.record_and_settle(ledger_values=terminal_values(job, worker), exact=True, media_execution_token=new) == "settled"
    assert await credits.record_and_settle(ledger_values=terminal_values(job, worker), exact=True, media_execution_token=new) == "duplicate"
    assert await counts() == (1, 1, 1)


@pytest.mark.asyncio
async def test_expired_session_cannot_renew_or_move_money_and_total_budget_never_resets(db):
    job, worker, token = prepared(await owner()), uuid4(), str(uuid4())
    await jobs.reserve(job)
    assert await claim(job, worker_id=worker, execution_token=token) == "claimed"
    await expire_execution(job.job_id)
    assert await jobs.renew_execution(job_id=job.job_id, worker_id=worker, execution_token=token) is False
    assert await credits.release_job(job.job_id, media_execution_token=token) is False
    assert (
        await credits.record_and_settle(ledger_values=terminal_values(job, worker), exact=True, media_execution_token=token)
        == "execution_superseded"
    )
    new = (await resume(worker))["execution_token"]
    assert await jobs.renew_execution(job_id=job.job_id, worker_id=worker, execution_token=new) is True
    async with await database.new_session() as session:
        await session.execute(
            media_jobs.update()
            .where(media_jobs.c.job_id == job.job_id)
            .values(execution_started=datetime.now(UTC) - timedelta(seconds=jobs.MAX_EXECUTION_SECONDS + 1)),
        )
        await session.commit()
    assert await jobs.renew_execution(job_id=job.job_id, worker_id=worker, execution_token=new) is False
    await expire_execution(job.job_id)
    assert await resume(worker) is None
    # The existing stale-hold/maintainer authority may close uncertainty exactly once.
    await credits.release_job(job.job_id)
    await credits.release_job(job.job_id)
    assert await credits.get_balance(job.account_id) == OPENING
    assert await counts() == (1, 1, 0)
    assert await resume(worker) is None


@pytest.mark.asyncio
async def test_pg_competing_reconnects_take_one_private_execution_lease(db):
    if db[1] != "postgres":
        pytest.skip("SQLite is not a concurrent resumption proof")
    job, worker = prepared(await owner()), uuid4()
    await jobs.reserve(job)
    assert await claim(job, worker_id=worker) == "claimed"
    await expire_execution(job.job_id)
    outcomes = await asyncio.wait_for(asyncio.gather(*(resume(worker) for _ in range(25))), timeout=30)
    winners = [item for item in outcomes if item is not None]
    assert len(winners) == 1
    assert (await snapshot(job))[0]["execution_token"] == winners[0]["execution_token"]
    assert await counts() == (1, 1, 0) and len((await snapshot(job))[2]) == 1


@pytest.mark.asyncio
async def test_pg_refund_racing_reconnect_never_pays_after_refund(db):
    if db[1] != "postgres":
        pytest.skip("SQLite is not a refund/resume concurrency proof")
    for _ in range(8):
        job, worker = prepared(await owner()), uuid4()
        await jobs.reserve(job)
        assert await claim(job, worker_id=worker) == "claimed"
        await expire_execution(job.job_id)
        recovered, _ = await asyncio.wait_for(asyncio.gather(resume(worker), credits.release_job(job.job_id)), timeout=10)
        if recovered is not None:
            assert (
                await credits.record_and_settle(
                    ledger_values=terminal_values(job, worker), exact=True, media_execution_token=recovered["execution_token"],
                )
                == "stale_no_payout"
            )
        assert await credits.get_balance(job.account_id) == OPENING
        assert (await snapshot(job))[1]["status"] == "settled"
        assert await resume(worker) is None
    assert await counts() == (8, 8, 0)


@pytest.mark.asyncio
async def test_actual_media_dispatch_recovers_original_job_with_fresh_presign(db, monkeypatch):
    job, worker = prepared(await owner(), seed=19), uuid4()
    await jobs.reserve(job)
    sends, presigns = [], []

    def presign(*_args, **_kwargs):
        presigns.append(len(presigns))
        return [
            {
                "public_url": "https://assets.example/video.mp4",
                "key": "video.mp4",
                "put_url": f"https://upload.example/attempt-{len(presigns)}",
                "content_type": "video/mp4",
            },
        ]

    class Socket:
        def __init__(self, disconnect=False):
            self.disconnect = disconnect

        async def send_json(self, message):
            sends.append(message)

        async def receive_json(self):
            if self.disconnect:
                raise worker_ws.WebSocketDisconnect(code=1006)
            return {"type": "done", "id": job.job_id, "results": [{"index": 0, "sha256": "a" * 64, "seed": 19}]}

    monkeypatch.setattr(worker_ws.storage, "presign_outputs", presign)
    monkeypatch.setattr(worker_ws.storage, "uploaded_outputs_present", lambda *_a, **_k: True)
    monkeypatch.setattr(worker_ws.video_output, "freeze", frozen_video_fixture)
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", AsyncMock())
    monkeypatch.setattr(worker_ws.route_events, "capture_route", lambda **_k: None)
    monkeypatch.setattr(worker_ws.route_events, "capture_outcome", lambda **_k: None)
    monkeypatch.setattr(worker_ws, "record_job_complete", lambda **_k: None)
    queued = {"job_id": job.job_id, "job_type": "video", "payload": job.payload, "models": job.route_models}
    with pytest.raises(worker_ws.WebSocketDisconnect):
        await worker_ws._handle_media_job(Socket(disconnect=True), queued, MODEL, str(worker), {})
    assert (await snapshot(job))[1]["status"] == "held"
    recovered = await resume(worker)
    assert recovered is not None
    assert await worker_ws._handle_media_job(Socket(), recovered, MODEL, str(worker), {}, resume_token=recovered["execution_token"]) is True
    first, second = [message for message in sends if message["type"] == "job"]
    assert first["id"] == second["id"] == job.job_id
    assert first["payload"] == second["payload"] == job.payload
    assert "resume" not in first and second["resume"] is True
    assert first["upload"][0]["put_url"] != second["upload"][0]["put_url"]
    assert "execution_token" not in second and "_media_execution_token" not in second["payload"]
    assert await counts() == (1, 1, 1) and len((await snapshot(job))[2]) == 1
    assert (await jobs.recover(job.account_id, job.job_id))["state"] == "completed"
    worker_ws.token_stream.publish_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrong_job_frame_never_refunds_running_execution(db, monkeypatch):
    job, worker = prepared(await owner()), uuid4()
    await jobs.reserve(job)
    socket = AsyncMock()
    socket.receive_json.return_value = {"type": "error", "id": str(uuid4()), "message": "another job"}
    monkeypatch.setattr(
        worker_ws.storage,
        "presign_outputs",
        lambda *_a, **_k: [
            {
                "public_url": "https://assets.example/video.mp4",
                "key": "video.mp4",
                "put_url": "https://upload.example/slot",
                "content_type": "video/mp4",
            },
        ],
    )
    monkeypatch.setattr(worker_ws.token_stream, "publish_error", AsyncMock())
    monkeypatch.setattr(worker_ws.route_events, "capture_route", lambda **_k: None)
    queued = {"job_id": job.job_id, "job_type": "video", "payload": job.payload, "models": job.route_models}
    with pytest.raises(worker_ws.WebSocketDisconnect):
        await worker_ws._handle_media_job(socket, queued, MODEL, str(worker), {})
    assert (await snapshot(job))[1]["status"] == "held" and await counts() == (1, 1, 0)
    worker_ws.token_stream.publish_error.assert_not_awaited()

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Candidate video graphs share the existing durable money authority.

Prices, storage, transport, discounts and free pockets are synthetic. SQL holds,
ledger writes, recovery and the worker terminal are real; no Base funds move.
"""

import asyncio
import copy
import json
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.routers import worker_ws
from grid_api.services import credits, generation_admission, job_queue, media, media_results, pricing, recipes, token_stream
from grid_api.services.recipe_import import recipe_root
from grid_api.v2.schema import accounts, credit_ledger, credits as balances, ledger, metadata, reservations

ROOT = Path(__file__).resolve().parents[3]
DISTILLED = "LTX-2.5 Distilled BF16"
DEV = "LTX-2.5 Dev BF16"
OPENING_MICRO = 1_000_000


@pytest.fixture(autouse=True)
def synthetic_dependencies(monkeypatch):
    from grid_api.config import GridSettings
    monkeypatch.setattr(generation_admission, "get_settings", lambda: GridSettings(
        _env_file=None, media_async_admission_mode="open"))
    for name in ("_BY_ROOT", "_BY_NAME", "_BY_ID", "_BY_MODEL", "_LOCAL_RECIPES", "_ONCHAIN_RECIPES"):
        monkeypatch.setattr(recipes, name, {})
    for path in (ROOT / "recipes/qualification").glob("*.json"):
        graph = json.loads(path.read_text())
        recipes.register_recipe(recipe_root(graph), graph["_grid"]["name"], graph)
    for model in (DISTILLED, DEV):
        # Test-only rate, not a customer tariff or activation decision.
        monkeypatch.setitem(pricing.PRICING, model.lower(), pricing.ModelPrice(0, 0, video_per_second=0.02))
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "on")
    monkeypatch.setattr(credits, "_promo_first", AsyncMock(return_value=0))
    monkeypatch.setattr(credits, "_free_first", AsyncMock(return_value=0))
    monkeypatch.setattr(credits.free_credits, "release", AsyncMock())
    async def no_discount(cost, **_kwargs):
        return cost
    monkeypatch.setattr(credits, "apply_holder_discount", no_discount)
    monkeypatch.setattr(generation_admission, "require_media", lambda *_args: None)
    monkeypatch.setattr(worker_ws.route_events, "capture_route", lambda **_kwargs: None)
    monkeypatch.setattr(worker_ws.route_events, "capture_outcome", lambda **_kwargs: None)
    monkeypatch.setattr(worker_ws, "record_job_complete", lambda **_kwargs: None)
    monkeypatch.setattr(worker_ws, "get_available_models", AsyncMock(return_value=[DISTILLED, DEV]))


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def db(request, monkeypatch):
    namespace = "ltx25_billing_test_" + uuid.uuid4().hex
    postgres = request.param == "postgres"
    if postgres:
        url = os.environ.get("CREDITS_TEST_DB_URL", "")
        if not url.startswith("postgresql+asyncpg://"):
            pytest.skip("CREDITS_TEST_DB_URL required for PostgreSQL video money proof")
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": namespace}},
                                     pool_size=5, max_overflow=20)
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


async def funded_account(amount=OPENING_MICRO):
    account_id = uuid.uuid4()
    async with await database.new_session() as session:
        await session.execute(accounts.insert().values(id=account_id, flags={}))
        if amount:
            # Emulates the verified funding adapter, not an untrusted reason label.
            await credits._credit_in_session(session, account_id, amount, "usdc_deposit",
                                             f"test-deposit:{account_id}", funded_micro=amount)
        await session.commit()
    return account_id


@pytest.mark.asyncio
async def test_private_pilot_cannot_be_bypassed_by_shared_waiting_submit(db, monkeypatch):
    from grid_api.config import GridSettings
    monkeypatch.setattr(generation_admission, "get_settings", lambda: GridSettings(_env_file=None))
    prepare, charge = AsyncMock(), AsyncMock()
    monkeypatch.setattr(media, "prepare_dispatch", prepare)
    monkeypatch.setattr(credits, "authorize_media", charge)
    with pytest.raises(HTTPException) as error:
        await media.submit_and_wait(DISTILLED, "video", {}, 1, account_id=uuid.uuid4())
    assert error.value.status_code == 503
    prepare.assert_not_awaited()
    charge.assert_not_awaited()


async def conserved_balance(account_id):
    async with await database.new_session() as session:
        row = (await session.execute(sa.select(balances).where(
            balances.c.account_id == account_id))).mappings().one()
        totals = (await session.execute(sa.select(
            sa.func.sum(credit_ledger.c.delta_micro), sa.func.sum(credit_ledger.c.funded_delta_micro),
        ).where(credit_ledger.c.account_id == account_id))).one()
    assert (row["balance_micro"], row["funded_balance_micro"]) == tuple(totals)
    return tuple(totals)


async def job_state(job_id):
    async with await database.new_session() as session:
        row = (await session.execute(sa.select(reservations).where(
            reservations.c.job_id == job_id))).mappings().one()
        completed = (await session.execute(sa.select(ledger).where(
            ledger.c.job_id == uuid.UUID(job_id)))).mappings().all()
    return row, completed


def expected_result(model, root=None):
    return {"media": [{"url": "https://assets.example/video.mp4", "key": "video.mp4",
                       "sha256": "a" * 64, "seed": 0}],
            "model": model, "worker": "video-test", "gen_time": 1.0, "recipe_root": root}


async def reserve_video(account_id, model=DISTILLED):
    job_id = str(uuid.uuid4())
    authorized = await credits.authorize_media(account_id, model, "video", 1, 81 / 24, job_id,
                                               record_reservation=True, client_ref="video-request")
    assert authorized["ok"]
    values = dict(job_id=job_id, worker_id=str(uuid.uuid4()), wallet="", model=model,
                  job_type="video", den=1.0, output_units=81, prompt_hash="b" * 64,
                  result_hash="c" * 64)
    return job_id, values, authorized["reserved"]


@pytest.mark.asyncio
async def test_shot_image_cap_blocks_repricing_without_reserve_or_dispatch(db, monkeypatch):
    account_id = await funded_account()
    # The agent observed 2500, but the live reserve sees 5000. A quote cannot
    # freeze pricing; the server-enforced ceiling is what protects the account.
    monkeypatch.setattr(pricing, "quote_image", lambda *_args: 5000)
    dispatch = AsyncMock()
    monkeypatch.setattr(job_queue, "submit_job", dispatch)
    with pytest.raises(HTTPException) as exc:
        await media.submit_and_wait(
            "z-image-turbo", "image", {"n": 1}, 1,
            account_id=account_id, max_cost_micro_usd=3000,
        )
    assert exc.value.status_code == 402
    dispatch.assert_not_awaited()
    credits._promo_first.assert_not_awaited()
    credits._free_first.assert_not_awaited()
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(reservations)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(ledger)) == 0
    assert await conserved_balance(account_id) == (OPENING_MICRO,) * 2


@pytest.mark.asyncio
async def test_shot_image_at_cost_ceiling_retains_durable_result_and_one_charge(db):
    account_id = await funded_account()
    job_id, model = str(uuid.uuid4()), "z-image-turbo"
    cost = pricing.quote_image(model, 1)
    authorized = await credits.authorize_media(
        account_id, model, "image", 1, None, job_id,
        record_reservation=True, client_ref="shot-image-test", max_cost_micro_usd=cost,
    )
    assert authorized["ok"] and authorized["reserved"] == cost
    values = dict(job_id=job_id, worker_id=str(uuid.uuid4()), wallet="", model=model,
                  job_type="image", den=1.0, output_units=1, prompt_hash="b" * 64,
                  result_hash="c" * 64)
    result = {"media": [{"url": "https://images.aipg.art/synthetic.png", "key": "synthetic.png",
                         "sha256": "a" * 64, "seed": 42}],
              "model": model, "worker": "image-test", "gen_time": 2.0}
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_result=result) == "settled"
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_result=result) == "duplicate"
    recovered = await media_results.recover(account_id, client_ref="shot-image-test")
    assert recovered["state"] == "completed" and recovered["actual_micro"] == cost
    assert recovered["result"]["media"][0]["seed"] == 42
    assert len((await job_state(job_id))[1]) == 1
    assert await conserved_balance(account_id) == (OPENING_MICRO - cost,) * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("model,steps", [(DISTILLED, 8), (DEV, 40)])
@pytest.mark.parametrize("source", ["none", "single", "multi"])
async def test_real_hold_precedes_dispatch_and_worker_commits_before_done(db, monkeypatch, model, steps, source):
    account_id = await funded_account()
    observed = {}
    frames = (337 if model == DISTILLED else 33) if source == "multi" else 81
    async def dispatch(job_id, payload, models, **_kwargs):
        cost = pricing.quote_video(model, payload["frames"] / payload["fps"])
        row, completed = await job_state(job_id)
        assert row["status"] == "held" and row["reserved_micro"] == cost
        assert row["media_result"] is None and not completed
        assert await conserved_balance(account_id) == (OPENING_MICRO - cost,) * 2
        assert payload["frames"] == payload["recipe_spec"]["latent_video"]["inputs"]["length"] == frames
        assert payload["recipe_spec"]["latent_audio"]["inputs"]["frames_number"] == frames
        assert payload["steps"] == steps and payload["seconds"] == frames / 24
        assert models == [model]
        if source != "none":
            assert payload["recipe_image_inputs"] == "key_0.inputs.image"
        if source == "multi":
            assert payload["required_media_feature"] == "recipe-image-bindings-v1"
            assert payload["recipe_image_bindings"] == [
                {"image_index": 0, "path": "key_0.inputs.image"},
                {"image_index": 1, "path": "key_1.inputs.image"},
            ]
            assert payload["recipe_spec"]["guide_1"]["inputs"]["frame_idx"] == frames - 1
        observed.update(job_id=job_id, payload=payload, cost=cost)
    async def publish_done(job_id, full_text):
        recovered = await media_results.recover(account_id, job_id=uuid.UUID(job_id))
        assert recovered["state"] == "completed"
        assert recovered["actual_micro"] == observed["cost"]
        assert recovered["result"] == json.loads(full_text)
        observed["full_text"] = full_text
        observed["events"].append("done")
    class Socket:
        async def send_json(self, message):
            if message["type"] == "ack":
                assert (await media_results.recover(account_id, client_ref="video-request"))["state"] == "completed"
                observed["events"].append("ack")
        async def receive_json(self):
            return {"type": "done", "results": [{"index": 0, "sha256": "a" * 64, "seed": 0}]}
    async def events(job_id, **_kwargs):
        observed["events"] = []
        assert await worker_ws._handle_media_job(Socket(), {
            "job_id": job_id, "job_type": "video", "payload": observed["payload"],
        }, model, str(uuid.uuid4()), {"name": "video-test", "wallet_address": ""})
        yield {"text": token_stream.DONE_SENTINEL, "full_text": observed["full_text"]}
    monkeypatch.setattr(job_queue, "submit_job", dispatch)
    monkeypatch.setattr(token_stream, "subscribe_tokens", events)
    monkeypatch.setattr(token_stream, "publish_done", publish_done)
    monkeypatch.setattr(worker_ws.storage, "presign_outputs", lambda *_a, **_k: [{
        "public_url": "https://assets.example/video.mp4", "key": "video.mp4",
        "put_url": "https://upload.example/slot", "content_type": "video/mp4"}])
    monkeypatch.setattr(worker_ws.storage, "uploaded_outputs_present", lambda *_a, **_k: True)
    payload = {"prompt": "a moving red ball", "seed": 0, "n": 1, "recipe_inputs": {"frames": frames}}
    if source != "none":
        payload["source_image_url"] = "https://assets.example/source.png"
    if source == "multi":
        payload["source_image_urls"] = [payload["source_image_url"], "https://assets.example/last.png"]
        payload["recipe_inputs"]["keyframes"] = [{"frame": 0}, {"frame": frames - 1, "strength": 0.5}]
    _, meta = await media.submit_and_wait(model, "video", payload, 1, account_id=account_id,
                                         progress_token="video-request")
    assert observed["events"] == ["done", "ack"]
    row, completed = await job_state(observed["job_id"])
    assert row["status"] == "settled" and len(completed) == 1
    assert completed[0]["output_units"] == frames and completed[0]["model"] == model
    assert meta["execution"]["steps"] == steps
    assert meta["recipe_root"] == observed["payload"]["recipe_root"]
    assert await conserved_balance(account_id) == (OPENING_MICRO - observed["cost"],) * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["insufficient", "unpriced"])
async def test_no_money_authority_means_no_dispatch_or_payout(db, monkeypatch, reason):
    account_id = await funded_account(1 if reason == "insufficient" else OPENING_MICRO)
    if reason == "unpriced":
        monkeypatch.delitem(pricing.PRICING, DISTILLED.lower())
    dispatch = AsyncMock()
    monkeypatch.setattr(job_queue, "submit_job", dispatch)
    with pytest.raises(HTTPException) as exc:
        await media.submit_and_wait(DISTILLED, "video", {"recipe_inputs": {"frames": 81}}, 1,
                                    account_id=account_id)
    assert exc.value.status_code == 402
    dispatch.assert_not_awaited()
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(reservations)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(ledger)) == 0
    assert await conserved_balance(account_id) == ((1 if reason == "insufficient" else OPENING_MICRO),) * 2


@pytest.mark.asyncio
async def test_video_duplicate_terminal_keeps_first_charge_and_result(db):
    account_id = await funded_account()
    job_id, values, cost = await reserve_video(account_id)
    result = expected_result(DISTILLED)
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_result=result) == "settled"
    changed = copy.deepcopy(result)
    changed["media"][0]["url"] = "https://assets.example/not-the-winning-video.mp4"
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_result=changed) == "duplicate"
    assert (await media_results.recover(account_id, client_ref="video-request"))["result"] == result
    assert await conserved_balance(account_id) == (OPENING_MICRO - cost,) * 2
    assert len((await job_state(job_id))[1]) == 1


@pytest.mark.asyncio
async def test_video_release_then_late_success_cannot_mint_worker_reward(db):
    account_id = await funded_account()
    job_id, values, _ = await reserve_video(account_id)
    await credits.release_job(job_id)
    await credits.release_job(job_id)
    assert await credits.record_and_settle(ledger_values=values, exact=True,
                                           media_result=expected_result(DISTILLED)) == "stale_no_payout"
    assert await conserved_balance(account_id) == (OPENING_MICRO,) * 2
    assert not (await job_state(job_id))[1]
    recovered = await media_results.recover(account_id, job_id=uuid.UUID(job_id))
    assert recovered["state"] == "closed_without_result" and recovered["result"] is None


@pytest.mark.asyncio
async def test_video_result_write_failure_rolls_back_payout_and_settlement(db):
    account_id = await funded_account()
    job_id, values, cost = await reserve_video(account_id)
    engine = db[0].kw["bind"]
    def reject_result(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE") and "media_result" in statement:
            raise RuntimeError("synthetic receipt storage failure")
    sa.event.listen(engine.sync_engine, "before_cursor_execute", reject_result)
    try:
        assert await credits.record_and_settle(ledger_values=values, exact=True,
                                               media_result=expected_result(DISTILLED)) == "error"
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", reject_result)
    row, completed = await job_state(job_id)
    assert row["status"] == "held" and row["media_result"] is None and not completed
    assert await conserved_balance(account_id) == (OPENING_MICRO - cost,) * 2
    assert (await media_results.recover(account_id, client_ref="video-request"))["state"] == "pending"


@pytest.mark.asyncio
async def test_post_dispatch_http_timeout_keeps_hold_and_recovers_late_video(db, monkeypatch):
    account_id = await funded_account()
    observed = {}
    async def dispatch(job_id, payload, models, **_kwargs):
        observed.update(job_id=job_id, root=payload["recipe_root"])
    async def no_terminal(*_a, **_k):
        if False:
            yield {}
    monkeypatch.setattr(job_queue, "submit_job", dispatch)
    monkeypatch.setattr(token_stream, "subscribe_tokens", no_terminal)
    with pytest.raises(HTTPException) as exc:
        await media.submit_and_wait(DISTILLED, "video", {"recipe_inputs": {"frames": 81}}, 1,
                                    account_id=account_id, progress_token="video-request")
    assert exc.value.status_code == 504
    job_id = observed["job_id"]
    row, completed = await job_state(job_id)
    assert row["status"] == "held" and not completed
    cost = row["reserved_micro"]
    values = dict(job_id=job_id, worker_id=str(uuid.uuid4()), wallet="", model=DISTILLED,
                  job_type="video", den=1.0, output_units=81, prompt_hash="b" * 64,
                  result_hash="c" * 64)
    result = expected_result(DISTILLED, observed["root"])
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_result=result) == "settled"
    recovered = await media_results.recover(account_id, client_ref="video-request")
    assert recovered == {"job_id": job_id, "state": "completed", "actual_micro": cost, "result": result}
    assert await conserved_balance(account_id) == (OPENING_MICRO - cost,) * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("release_racer", [False, True])
async def test_postgres_competing_terminals_conserve_video_money_and_result(db, release_racer):
    if db[1] != "postgres":
        pytest.skip("competing independent transactions require PostgreSQL")
    from grid_api.services.tests.test_paid_validator_audit_postgres import blocked_race
    account_id = await funded_account()
    job_id, values, cost = await reserve_video(account_id)
    result = expected_result(DISTILLED)
    async def success():
        return await credits.record_and_settle(ledger_values=values, exact=True, media_result=result)
    async def release():
        await credits.release_job(job_id)
        return "release"
    outcomes = await blocked_race(db[0], job_id, [success] * 20 + ([release] if release_racer else []))
    assert "error" not in outcomes
    row, completed = await job_state(job_id)
    assert row["status"] == "settled"
    if completed:
        assert len(completed) == 1 and outcomes.count("settled") == 1
        assert row["media_result"] == result and row["actual_micro"] == cost
        assert await conserved_balance(account_id) == (OPENING_MICRO - cost,) * 2
    else:
        assert release_racer and set(outcomes) == {"stale_no_payout", "release"}
        assert row["media_result"] is None and row["actual_micro"] == 0
        assert await conserved_balance(account_id) == (OPENING_MICRO,) * 2


@pytest.mark.asyncio
async def test_postgres_video_reserves_cannot_overdraw_under_concurrency(db):
    if db[1] != "postgres":
        pytest.skip("concurrent reservations require PostgreSQL")
    cost = pricing.quote_video(DISTILLED, 81 / 24)
    account_id = await funded_account(cost * 3)
    jobs = [str(uuid.uuid4()) for _ in range(20)]
    authorized = await asyncio.wait_for(asyncio.gather(*[
        credits.authorize_media(account_id, DISTILLED, "video", 1, 81 / 24, job_id,
                                record_reservation=True) for job_id in jobs
    ]), timeout=30)
    assert sum(item["ok"] for item in authorized) == 3
    assert sum(item["status"] == "insufficient" for item in authorized) == 17
    assert await conserved_balance(account_id) == (0, 0)
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(reservations)) == 3
        assert await session.scalar(sa.select(sa.func.count()).select_from(ledger)) == 0

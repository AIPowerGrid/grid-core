# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Embeddings against the real credit ledger and the real worker handshake
(SQLite, no mocks of reserve or settle): the charge equals the reservation
whatever the worker reports, a malformed body is refunded, and the format
survives registration."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.config import GridSettings
from grid_api.routers import _passthrough as pt
from grid_api.routers import worker_ws
from grid_api.services import credits, free_credits, generation_admission, promotions
from grid_api.v2.schema import metadata as v2_metadata

SEED = 10_000_000


@pytest_asyncio.fixture
async def ledger(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(v2_metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setenv("GRID_SALT", "embeddings-money-test-only-salt")
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "on")
    monkeypatch.setattr(free_credits, "FREE_SPENDABLE_LIVE", False)
    monkeypatch.setattr(promotions, "PROMO_SPENDABLE_LIVE", False)
    settings = GridSettings(_env_file=None, generation_enabled_paths=["openai-embeddings"])
    monkeypatch.setattr(generation_admission, "get_settings", lambda: settings)
    monkeypatch.setattr(pt.prompt_screen, "observe", lambda *a, **k: None)
    alerts = []
    monkeypatch.setattr(credits, "_economic_alert", lambda kind, *a, **k: alerts.append(kind))
    captured = {"alerts": alerts, "done": [], "errors": []}

    async def publish_error(job_id, message, code=502):
        captured["errors"].append((message, code))

    async def publish_done(*a, **k):
        captured["done"].append(k)

    monkeypatch.setattr(worker_ws.token_stream, "publish_error", publish_error)
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", publish_done)
    monkeypatch.setattr(worker_ws.token_stream, "is_cancelled", AsyncMock(return_value=False))
    monkeypatch.setattr(worker_ws, "_clear_strikes", AsyncMock())
    monkeypatch.setattr(worker_ws, "_record_strike", AsyncMock(return_value=1))
    monkeypatch.setattr(worker_ws, "record_job_complete", lambda **k: None)
    monkeypatch.setattr(worker_ws, "record_job_failed", lambda **k: None)
    monkeypatch.setattr(worker_ws.route_events, "capture_route", lambda **_: None)
    monkeypatch.setattr(worker_ws.route_events, "capture_outcome", lambda **_: None)
    try:
        yield captured
    finally:
        database._session_factory = old
        await engine.dispose()


class _Socket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def send_json(self, value):
        self.sent.append(value)

    async def receive_json(self):
        return self.incoming.pop(0)


async def _reserve(account, model, api_format, request, budget):
    job_id = str(uuid.uuid4())
    auth = await pt.authorize_passthrough({"account_id": account}, model, api_format, request, budget, job_id)
    return job_id, auth


async def _finish(job_id, model, api_format, request, budget, full_json, usage=None):
    job = {"job_id": job_id, "payload": {"api_format": api_format, "max_length": budget, "request": request}}
    ws = _Socket([{"type": "done", "usage": usage, "full_json": full_json}])
    assert await worker_ws._handle_raw_passthrough(
        ws, job, model, str(uuid.uuid4()), {"name": "rig", "wallet_address": "", "max_context_length": 1_000_000})
    return ws.sent[-1]


# ── money ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 999999, "completion_tokens": 4096}])
async def test_embeddings_charge_equals_the_reservation_whatever_the_worker_reports(ledger, usage):
    account = uuid.uuid4()
    await credits.credit(account, SEED, "topup", ref=f"seed-{uuid.uuid4()}")
    raw = {"model": "nomic-embed-text", "input": ["The grid counts this text. " * 40, "And this one."]}
    job_id, auth = await _reserve(account, "nomic-embed-text", pt.EMBEDDINGS_FORMAT, raw, 0)
    assert auth["ok"] and auth["reserved"] > 0
    full = {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
    ack = await _finish(job_id, "nomic-embed-text", pt.EMBEDDINGS_FORMAT, raw, 0, full, usage=usage)
    assert SEED - await credits.get_balance(account) == auth["reserved"]
    assert ack["den"] == worker_ws.calculate_den(output_tokens=0)      # the minimum award, not 4096 tokens' worth
    assert not ledger["alerts"]


@pytest.mark.asyncio
async def test_a_malformed_embeddings_body_is_refunded_in_full(ledger):
    account = uuid.uuid4()
    await credits.credit(account, SEED, "topup", ref=f"seed-{uuid.uuid4()}")
    raw = {"model": "nomic-embed-text", "input": ["one", "two"]}
    job_id, auth = await _reserve(account, "nomic-embed-text", pt.EMBEDDINGS_FORMAT, raw, 0)
    assert auth["ok"]
    ack = await _finish(job_id, "nomic-embed-text", pt.EMBEDDINGS_FORMAT, raw, 0,
                        {"data": [{"embedding": [0.1, 0.2]}]})          # one vector for two inputs
    assert await credits.get_balance(account) == SEED
    assert ack == {"type": "ack", "id": job_id, "den": 0} and not ledger["done"]
    assert ledger["errors"] == [("Worker returned a malformed embeddings response.", 502)]
    worker_ws._record_strike.assert_awaited_once()


# ── registration ─────────────────────────────────────────────────────────────


class _Worker:
    def __init__(self, init):
        self.sent = []
        self.incoming = [init]

    async def accept(self):
        pass

    async def close(self, **_kwargs):
        pass

    async def send_json(self, message):
        self.sent.append(message)

    async def receive_json(self):
        if self.incoming:
            return self.incoming.pop(0)
        raise WebSocketDisconnect(code=1000)


async def _register(monkeypatch, account, api_formats):
    registered = []
    monkeypatch.setattr(worker_ws.accounts_svc, "resolve_api_key", AsyncMock(return_value={
        "id": str(account), "account_id": account, "source": "v2", "scopes": ["worker.connect"]}))
    monkeypatch.setattr(worker_ws.worker_identity_svc, "verify_registration", AsyncMock(return_value=None))
    monkeypatch.setattr(worker_ws, "_is_in_cooldown", AsyncMock(return_value=False))

    async def register_worker(worker_id, info):
        registered.append(info)

    monkeypatch.setattr(worker_ws, "register_worker", register_worker)
    for name in ("unregister_worker", "refresh_worker"):
        monkeypatch.setattr(worker_ws, name, AsyncMock())
    monkeypatch.setattr(worker_ws.media_jobs, "claim_resumption", AsyncMock(return_value=None))

    class _Result:
        def first(self):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args, **_kwargs):
            return _Result()

        async def commit(self):
            pass

    async def new_session():
        return _Session()

    monkeypatch.setattr(worker_ws, "new_session", new_session)

    async def never(*_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(worker_ws.job_queue, "pop_job", never)
    init = {"apikey": "k", "name": f"rig-{uuid.uuid4().hex[:6]}", "models": ["nomic-embed-text"], "job_types": ["text"]}
    if api_formats is not None:
        init["api_formats"] = api_formats
    worker = _Worker(init)
    try:
        await asyncio.wait_for(worker_ws.worker_websocket(worker), timeout=1)
    except TimeoutError:
        pass
    return (registered[0] if registered else None), worker.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("formats,stored", [
    (["openai-embeddings"], ["openai-embeddings"]),
    (["openai-embeddings", "openai-embeddings"], ["openai-embeddings"]),
    (["openai-chat", "openai-embeddings", "bogus"], ["openai-chat", "openai-embeddings"]),
])
async def test_the_embeddings_format_is_stored_once_at_registration(ledger, monkeypatch, formats, stored):
    info, _ = await _register(monkeypatch, uuid.uuid4(), formats)
    assert info["api_formats"] == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("formats", [None, "openai-embeddings", [], ["bogus"], [5, None]])
async def test_legacy_and_malformed_format_lists_still_mean_a_chat_worker(ledger, monkeypatch, formats):
    info, _ = await _register(monkeypatch, uuid.uuid4(), formats)
    assert info["api_formats"] == ["openai-chat"]

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Actual dispatch/terminal code against isolated PG, with simulated worker I/O.

Auth, queue transport, registry, R2 and client delivery are fixtures. Monetary
authorization, refunds, terminal transactions and worker registration use PG.
The subprocess test kills Core's refund transaction process, not a running
Uvicorn coordinator with its Redis queue. No production services are contacted.
"""

import asyncio
import os
import secrets
import sys
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import WebSocketDisconnect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from grid_api import auth, database, safe_logging
from grid_api.routers import worker_ws as transport
from grid_api.services import credits, free_credits, promotions
from grid_api.v2 import schema as tables

PG = os.environ.get("CREDITS_TEST_DB_URL", "")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG.startswith("postgresql"),
        reason="requires disposable CREDITS_TEST_DB_URL",
    ),
]
FORMATS = ["openai-chat", "openai-responses", "anthropic", "image", "video", "audio"]
INITIAL = 1_000_000


@pytest_asyncio.fixture
async def pg(monkeypatch):
    monkeypatch.setenv("GRID_SALT", secrets.token_urlsafe(32))
    monkeypatch.setattr(auth, "_API_KEY_SALT", None)
    safe_logging._log_key.cache_clear()
    namespace = "billing_worker_test_" + uuid.uuid4().hex
    engine = create_async_engine(PG, execution_options={"schema_translate_map": {None: namespace}})
    created = False
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.schema.CreateSchema(namespace))
            created = True
            await conn.run_sync(tables.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(database, "_session_factory", factory)
        monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "on")
        # Explicit purchased-only test policy; no external Redis allowance calls.
        monkeypatch.setattr(free_credits, "FREE_SPENDABLE_LIVE", False)
        monkeypatch.setattr(promotions, "PROMO_SPENDABLE_LIVE", False)
        yield factory
    finally:
        try:
            if created:
                async with engine.begin() as conn:
                    await conn.execute(sa.schema.DropSchema(namespace, cascade=True))
        finally:
            await engine.dispose()
            safe_logging._log_key.cache_clear()


async def reserve(pg, fmt):
    account, job_id = uuid.uuid4(), str(uuid.uuid4())
    kind = fmt if fmt in {"image", "video", "audio"} else "text"
    model = {"image": "z-image-turbo", "video": "ltx-2.3", "audio": "ace-step-v1.5-xl-turbo"}.get(kind, "gpt-oss-120b")
    async with pg() as session:
        await session.execute(sa.insert(tables.accounts).values(id=account))
        await session.commit()
    assert await credits.credit(account, INITIAL, "test:seed", ref=f"seed:{job_id}")
    if kind == "text":
        auth = await credits.authorize_request(
            {"account_id": account},
            model,
            100,
            100,
            job_id,
            record_reservation=True,
        )
    else:
        auth = await credits.authorize_media(
            account,
            model,
            kind,
            1,
            10,
            job_id,
            record_reservation=True,
        )
    assert auth["ok"] and auth["reserved"] > 0, auth
    assert await credits.get_balance(account) == INITIAL - auth["reserved"]
    job = {
        "job_id": job_id,
        "job_type": kind,
        "models": [model],
        "stream_id": "1-0",
        "stream": "fixture-stream",
        "payload": {
            "api_format": fmt if kind == "text" else "openai-chat",
            "prompt": "Fixture prompt",
            "max_length": 100,
            "n": 1,
            "seconds": 10,
            "width": 1024,
            "height": 1024,
            "steps": 8,
        },
    }
    return account, job, auth["reserved"]


class Worker:
    def __init__(self, job, incoming):
        self.sent = []
        self.incoming = [
            {
                "apikey": secrets.token_urlsafe(32),
                "name": "fixture-worker",
                "models": job["models"],
                "job_types": [job["job_type"]],
                "api_formats": [job["payload"]["api_format"]],
            },
        ] + incoming

    async def accept(self):
        pass

    async def close(self, **_kwargs):
        pass

    async def send_json(self, message):
        self.sent.append(message)

    async def receive_json(self):
        value = self.incoming.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


async def dispatch(monkeypatch, account, job, incoming, *, retry=False, disconnected=False):
    worker = Worker(job, incoming)
    ended = asyncio.Event()
    pending = [job]
    requeues, acks, errors, successes = [], [], [], []

    async def pop(*_args, **_kwargs):
        if pending:
            return pending.pop()
        await asyncio.Future()

    async def requeue(*args, **kwargs):
        requeues.append((args, kwargs))
        ended.set()
        return "2-0" if retry else None

    original_release = credits.release_job

    async def release(job_id):
        await original_release(job_id)
        ended.set()

    async def ack(*args, **kwargs):
        acks.append((args, kwargs))

    async def error(*args, **kwargs):
        errors.append((args, kwargs))

    async def done(*args, **kwargs):
        successes.append((args, kwargs))

    @asynccontextmanager
    async def claim(_job):
        yield

    monkeypatch.setattr(
        transport.accounts_svc,
        "resolve_api_key",
        AsyncMock(
            return_value={
                "id": str(account),
                "account_id": account,
                "source": "v2",
                "scopes": ["worker.connect"],
            },
        ),
    )
    monkeypatch.setattr(transport.worker_identity_svc, "verify_registration", AsyncMock(return_value=None))
    monkeypatch.setattr(transport, "_requires_managed_profile", lambda *_: False)
    for name in ("register_worker", "unregister_worker", "refresh_worker", "_clear_strikes"):
        monkeypatch.setattr(transport, name, AsyncMock())
    monkeypatch.setattr(transport, "_is_in_cooldown", AsyncMock(return_value=False))
    monkeypatch.setattr(transport, "_record_strike", AsyncMock(return_value=1))
    monkeypatch.setattr(transport.job_queue, "pop_job", pop)
    monkeypatch.setattr(transport.job_queue, "requeue_job", requeue)
    monkeypatch.setattr(transport.job_queue, "ack_job", ack)
    monkeypatch.setattr(transport.job_queue, "maintain_job_claim", claim)
    monkeypatch.setattr(transport.token_stream, "publish_error", error)
    monkeypatch.setattr(transport.token_stream, "publish_done", done)
    monkeypatch.setattr(transport.token_stream, "publish_token", AsyncMock())
    monkeypatch.setattr(transport.token_stream, "publish_raw_event", AsyncMock())
    monkeypatch.setattr(transport.token_stream, "is_cancelled", AsyncMock(return_value=False))
    monkeypatch.setattr(transport.route_events, "capture_route", lambda **_: None)
    monkeypatch.setattr(transport.route_events, "capture_outcome", lambda **_: None)
    monkeypatch.setattr(credits, "release_job", release)
    monkeypatch.setattr(
        transport.storage,
        "presign_outputs",
        lambda *_a, **_k: [
            {
                "put_url": "https://upload.invalid/fixture",
                "key": "fixture/output",
                "public_url": "https://media.invalid/fixture",
                "content_type": "application/octet-stream",
            },
        ],
    )
    runner = asyncio.create_task(transport.worker_websocket(worker))
    terminal = asyncio.create_task(ended.wait())
    try:
        if disconnected:
            await asyncio.wait_for(runner, timeout=5)
        else:
            completed, _ = await asyncio.wait({runner, terminal}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            assert terminal in completed, worker.sent
            # Let the terminal branch clear current_job before ending idle I/O.
            await asyncio.sleep(0)
    finally:
        if not runner.done():
            runner.cancel()
        terminal.cancel()
        result, _ = await asyncio.gather(runner, terminal, return_exceptions=True)
        await asyncio.sleep(0)
    assert result is None or isinstance(result, asyncio.CancelledError), result
    assert any(message["type"] == "ready" for message in worker.sent), worker.sent
    assert sum(message["type"] == "job" for message in worker.sent) == 1
    assert not successes
    assert not any(message["type"] == "ack" and message.get("den", 0) > 0 for message in worker.sent)
    return requeues, acks, errors


async def assert_money(pg, account, job, reserved, *, held):
    expected = INITIAL - reserved if held else INITIAL
    assert await credits.get_balance(account) == expected
    async with pg() as session:
        row = (await session.execute(sa.select(tables.reservations).where(tables.reservations.c.job_id == job["job_id"]))).mappings().one()
        assert row["status"] == ("held" if held else "settled")
        assert row["actual_micro"] is None
        assert not await session.scalar(sa.select(sa.func.count()).select_from(tables.ledger))
        movements = (
            (
                await session.execute(
                    sa.select(tables.credit_ledger.c.delta_micro).where(
                        tables.credit_ledger.c.ref.in_([job["job_id"], job["job_id"] + ":refund"]),
                    ),
                )
            )
            .scalars()
            .all()
        )
        assert sorted(movements) == ([-reserved] if held else [-reserved, reserved])
    assert (await credits.billing_health())["ok"]


@pytest.mark.parametrize("fmt", FORMATS)
async def test_explicit_worker_error_refunds_once(pg, monkeypatch, fmt):
    account, job, cost = await reserve(pg, fmt)
    requeues, acks, errors = await dispatch(
        monkeypatch,
        account,
        job,
        [
            {"type": "error", "message": "Fixture backend rejected the request", "client_error": True},
        ],
    )
    assert not requeues and len(acks) == 1 and len(errors) == 1
    await assert_money(pg, account, job, cost, held=False)
    await credits.release_job(job["job_id"])
    await assert_money(pg, account, job, cost, held=False)


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("fault", ["disconnect", "timeout"])
@pytest.mark.parametrize("retry", [False, True])
async def test_lost_worker_retains_retry_hold_or_refunds_on_giveup(pg, monkeypatch, fmt, fault, retry):
    account, job, cost = await reserve(pg, fmt)
    exception = WebSocketDisconnect(code=1006) if fault == "disconnect" else TimeoutError()
    requeues, acks, errors = await dispatch(
        monkeypatch,
        account,
        job,
        [exception],
        retry=retry,
        disconnected=True,
    )
    assert not acks and len(requeues) == 1
    assert requeues[0][0][0] == job["job_id"]
    assert requeues[0][1]["job_type"] == job["job_type"]
    assert len(errors) == (0 if retry else 1)
    await assert_money(pg, account, job, cost, held=retry)
    if retry:
        # Exhausting a later attempt returns the original hold, not another debit.
        await credits.release_job(job["job_id"])
        await assert_money(pg, account, job, cost, held=False)


async def test_chat_failure_after_partial_output_refunds_without_retry(pg, monkeypatch):
    account, job, cost = await reserve(pg, "openai-chat")
    requeues, acks, errors = await dispatch(
        monkeypatch,
        account,
        job,
        [
            {"type": "token", "delta": {"content": "Partial output"}},
            {"type": "error", "message": "Fixture backend failure"},
        ],
    )
    assert not requeues and len(acks) == 1 and len(errors) == 1
    await assert_money(pg, account, job, cost, held=False)


@pytest.mark.parametrize("fmt", FORMATS)
async def test_failed_refund_transaction_remains_recoverable_by_sweeper(pg, monkeypatch, fmt):
    account, job, cost = await reserve(pg, fmt)

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("fixture refund transaction interrupted")

    with monkeypatch.context() as failed_refund:
        failed_refund.setattr(credits, "_credit_in_session", unavailable)
        requeues, acks, errors = await dispatch(
            failed_refund,
            account,
            job,
            [
                {"type": "error", "message": "Fixture backend failure", "client_error": True},
            ],
        )
        assert not requeues and len(acks) == 1 and len(errors) == 1
        # The status update and failed refund roll back together on PostgreSQL.
        await assert_money(pg, account, job, cost, held=True)

    assert await credits.sweep_stale_reservations(older_than_seconds=0) == 1
    await assert_money(pg, account, job, cost, held=False)
    assert await credits.sweep_stale_reservations(older_than_seconds=0) == 0
    # A late completion cannot turn this refunded attempt into a worker reward.
    result = await credits.record_and_settle(
        ledger_values={
            "job_id": job["job_id"],
            "worker_id": job["worker_id"],
            "wallet": "",
            "model": job["models"][0],
            "job_type": job["job_type"],
            "den": 100.0,
            "output_units": 10,
            "prompt_hash": "a" * 64,
            "result_hash": "b" * 64,
        },
        completion_tokens=10,
        exact=job["job_type"] != "text",
    )
    assert result == "stale_no_payout"
    await assert_money(pg, account, job, cost, held=False)


REFUND_CHILD = """
import asyncio
import os
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from grid_api import database
from grid_api.services import credits

async def run():
    engine = create_async_engine(os.environ['TEST_PG'], execution_options={
        'schema_translate_map': {None: os.environ['TEST_SCHEMA']},
    })
    database._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async def paused_credit(*args, **kwargs):
        # settle_job has already updated the held row in its open transaction.
        print('refund-transaction-open', flush=True)
        await asyncio.Event().wait()
    credits._credit_in_session = paused_credit
    await credits.release_job(os.environ['TEST_JOB'])
    raise RuntimeError('refund did not reach its transaction barrier')

asyncio.run(run())
"""


@pytest.mark.parametrize("fmt", FORMATS)
async def test_process_kill_during_refund_rolls_back_then_sweeper_recovers(pg, fmt):
    account, job, cost = await reserve(pg, fmt)
    namespace = pg.kw["bind"].get_execution_options()["schema_translate_map"][None]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        REFUND_CHILD,
        env={
            "PATH": os.environ.get("PATH", ""),
            "GRID_SALT": secrets.token_urlsafe(32),
            "TEST_PG": PG,
            "TEST_SCHEMA": namespace,
            "TEST_JOB": job["job_id"],
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=15)
        assert line == b"refund-transaction-open\n", line
        # Other connections cannot observe the uncommitted terminal state.
        await assert_money(pg, account, job, cost, held=True)
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode != 0
    await assert_money(pg, account, job, cost, held=True)
    assert await credits.sweep_stale_reservations(older_than_seconds=0) == 1
    await assert_money(pg, account, job, cost, held=False)
    assert await credits.sweep_stale_reservations(older_than_seconds=0) == 0

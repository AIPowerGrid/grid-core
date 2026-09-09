# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Kill a real Uvicorn Core, retain PG/Redis, and restart its safety loops.

Requires a disposable CREDITS_TEST_DB_URL with CREATE DATABASE permission and
redis-server. Each case owns a new database and Redis process. Real scoped-key
authentication, HTTP, worker WebSockets, queue, billing and recovery run here;
worker output and R2 presence are synthetic, not GPU/model or object-store
qualification. Dependency addresses, clocks and crash barriers are injected.
Audio uses a real ephemeral wallet delegation and signed completion receipt.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as redis
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from websockets.asyncio.client import connect

from grid_api.v2 import schema as tables

PG = os.environ.get("CREDITS_TEST_DB_URL", "")
ROOT = Path(__file__).resolve().parents[3]
INITIAL = 1_000_000
MODEL = "gpt-oss-120b"
MEDIA = {"image": "z-image-turbo", "video": "ltx-2.3", "audio": "ace-step-v1.5-xl-turbo"}
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG.startswith("postgresql") or not shutil.which("redis-server") or os.name != "posix",
        reason="requires disposable PostgreSQL, redis-server and POSIX SIGKILL",
    ),
]

# Pause AFTER the real transaction returns, not inside a mocked money path.
# SIGKILL is sent by the parent only after independently reading the DB/PEL.
CHILD = r"""
import asyncio, os, socket
from pathlib import Path
from grid_api.config import GridSettings
GridSettings.model_config["env_file"] = None
GridSettings.async_database_url = property(lambda self: os.environ["TEST_DB_URL"])
from grid_api.services import credits, job_queue
original_sweep = credits.sweep_stale_reservations
async def sweep(*args, **kwargs):
    result = await original_sweep(*args, **kwargs)
    Path(os.environ["TEST_SWEEP"]).touch()
    return result
credits.sweep_stale_reservations = sweep
job_queue.STALE_JOB_MS = 0
job_queue.STALE_JOB_MS_MEDIA = 0
from grid_api.services import storage
def slots(job_id, n, ext, **kwargs):
    return [{"key": f"{job_id}/{i}.{ext}",
             "public_url": f"https://fixture.invalid/{job_id}/{i}.{ext}",
             "put_url": f"https://fixture.invalid/{job_id}/{i}.{ext}",
             "content_type": "audio/wav" if ext == "wav" else "video/mp4" if ext == "mp4" else "image/webp"}
            for i in range(n)]
storage.presign_outputs = slots
storage.uploaded_outputs_present = lambda *args, **kwargs: True
async def barrier():
    Path(os.environ["TEST_BARRIER"]).touch()
    await asyncio.Event().wait()
if os.environ["TEST_PHASE"] == "reserved":
    original = job_queue.submit_job
    async def submit(*args, **kwargs):
        await barrier()
        return await original(*args, **kwargs)
    job_queue.submit_job = submit
elif os.environ["TEST_PHASE"] == "committed":
    original = credits.record_and_settle
    async def settle(*args, **kwargs):
        result = await original(*args, **kwargs)
        assert result == "settled", result
        await barrier()
        return result
    credits.record_and_settle = settle
import uvicorn
from grid_api.main import app
sock = socket.socket(fileno=int(os.environ["TEST_SOCKET"]))
asyncio.run(uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).serve(sockets=[sock]))
"""


def bound_socket():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


async def eventually(check, *, timeout=45):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for independently observed recovery state")


class Core:
    def __init__(self, env, path):
        self.env, self.path = env, path
        self.process = None
        self.log = None
        self.wallet = Account.create()
        self.signer = Account.create()
        self.socket = bound_socket()
        self.port = self.socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    async def start(self, phase="none", *, orphan=False):
        marker = self.path / "barrier"
        marker.unlink(missing_ok=True)
        swept = self.path / "swept"
        swept.unlink(missing_ok=True)
        self.log = (self.path / "core.log").open("ab")
        env = dict(
            self.env,
            TEST_PHASE=phase,
            TEST_BARRIER=str(marker),
            TEST_SWEEP=str(swept),
            TEST_SOCKET=str(self.socket.fileno()),
            RESERVATION_STALE_SECONDS="0" if orphan else "3600",
        )
        self.process = subprocess.Popen(
            [sys.executable, "-c", CHILD],
            cwd=ROOT,
            env=env,
            pass_fds=(self.socket.fileno(),),
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:

            async def ready():
                assert self.process.poll() is None, (self.path / "core.log").read_text()[-8000:]
                try:
                    return (await client.get(self.url + "/health")).status_code == 200 and swept.exists()
                except httpx.TransportError:
                    return False

            await eventually(ready, timeout=60)

    async def kill(self):
        code = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            await asyncio.to_thread(self.process.wait, timeout=10)
            code = self.process.returncode
            self.process = None
        if self.log:
            self.log.close()
            self.log = None
        return code


@pytest_asyncio.fixture
async def rig(tmp_path):
    name = "core_crash_" + uuid.uuid4().hex
    admin = create_async_engine(PG, isolation_level="AUTOCOMMIT")
    engine = None
    server = None
    client = None
    core = None
    created = False
    try:
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
        created = True
        db_url = make_url(PG).set(database=name).render_as_string(hide_password=False)
        engine = create_async_engine(db_url)
        # Choose a private loopback Redis port. No existing instance is flushed.
        with bound_socket() as reserved:
            port = reserved.getsockname()[1]
        with (tmp_path / "redis.log").open("wb") as log:
            server = subprocess.Popen(
                [
                    shutil.which("redis-server"),
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                    "--dir",
                    str(tmp_path),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        client = redis.from_url(f"redis://127.0.0.1:{port}/0", decode_responses=True)

        async def redis_ready():
            assert server.poll() is None
            if "Ready to accept connections" not in (tmp_path / "redis.log").read_text():
                return False
            try:
                return await client.ping()
            except redis.ConnectionError:
                return False

        await eventually(redis_ready)
        salt = secrets.token_hex(32)
        # Deliberately do not inherit operator credentials, proxy, RPC or flags.
        env = {k: os.environ[k] for k in ("PATH", "HOME", "SYSTEMROOT", "TIKTOKEN_CACHE_DIR") if k in os.environ}
        env.update(
            TEST_DB_URL=db_url,
            GRID_SALT=salt,
            REDIS_IP="127.0.0.1",
            REDIS_PORT=str(port),
            REDIS_STREAM_DB="0",
            GRID_CHARGING_MODE="on",
            GENERATION_ENABLED_PATHS=json.dumps(["openai-chat", "openai-responses", "anthropic", "image", "video", "audio"]),
            AUDIO_ENABLED="1",
            APPROVED_WORKER_PROFILE_DIGESTS="a" * 64,
            GRID_FREE_SPENDABLE_LIVE="0",
            GRID_PROMO_SPENDABLE_LIVE="0",
            RESERVATION_SWEEP_SECONDS="1",
            PYTHONUNBUFFERED="1",
        )
        core = Core(env, tmp_path)
        await core.start()
        account, worker_account = uuid.uuid4(), uuid.uuid4()
        key, worker_key = "grid_" + secrets.token_urlsafe(32), "grid_" + secrets.token_urlsafe(32)
        async with engine.begin() as conn:
            await conn.execute(
                sa.insert(tables.accounts),
                [
                    {"id": account, "payout_wallet": None},
                    {"id": worker_account, "payout_wallet": core.wallet.address.lower()},
                ],
            )
            for aid, plaintext, scopes in (
                (account, key, ["account.read", "inference.submit"]),
                (worker_account, worker_key, ["worker.connect"]),
            ):
                await conn.execute(
                    sa.insert(tables.api_keys).values(
                        hash=hashlib.sha256((salt + plaintext).encode()).hexdigest(),
                        account_id=aid,
                        scopes=scopes,
                    ),
                )
            await conn.execute(sa.insert(tables.credits).values(account_id=account, balance_micro=INITIAL))
            await conn.execute(
                sa.insert(tables.credit_ledger).values(
                    account_id=account,
                    delta_micro=INITIAL,
                    reason="test:seed",
                    ref="seed",
                ),
            )
        yield core, engine, client, account, key, worker_key
    finally:
        if core:
            await core.kill()
            core.socket.close()
        if client:
            await client.aclose()
        if server:
            if server.poll() is None:
                server.terminate()
            await asyncio.to_thread(server.wait, timeout=10)
        if engine:
            await engine.dispose()
        if created:
            async with admin.connect() as conn:
                await conn.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


def audio_identity(core, init):
    from grid_api.services import audio, worker_identity

    now = int(time.time())
    profile = {
        "id": MEDIA["audio"],
        "version": "0.1.0",
        "digest": "a" * 64,
        "signing_key_id": "test-only",
        "capability_tier": "audio.ace-step.standard",
        "runtime_adapter": audio.ACE_STEP_RUNTIME_ADAPTER,
        "runtime_digest": audio.ACE_STEP_RUNTIME_DIGEST,
        "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
        "canary_completed_at": datetime.now(UTC).isoformat(),
        "canary_elapsed_seconds": 1.0,
    }
    delegation = {
        "version": 1,
        "chain_id": 8453,
        "audience": "api.aipowergrid.io",
        "delegation_id": secrets.token_hex(16),
        "payout_wallet": core.wallet.address.lower(),
        "worker_signer": core.signer.address.lower(),
        "worker_name": init["name"],
        "issued_at": now - 1,
        "expires_at": now + 3600,
    }
    payload = {
        "version": 1,
        "timestamp": now,
        "nonce": secrets.token_hex(16),
        "worker_signer": core.signer.address.lower(),
        "worker_name": init["name"],
        "models": init["models"],
        "job_types": init["job_types"],
        "bridge_agent": init["bridge_agent"],
        "profile_digest": profile["digest"],
        "profile_recipe_root": profile["recipe_root"],
    }
    init.update(
        worker_profile=profile,
        worker_identity={
            "payload": payload,
            "signature": core.signer.sign_message(encode_defunct(text=worker_identity.registration_message(payload))).signature.hex(),
            "delegation": {
                "payload": delegation,
                "signature": core.wallet.sign_message(encode_defunct(text=worker_identity.delegation_message(delegation))).signature.hex(),
            },
        },
    )


async def worker(core, key, fmt):
    ws = await connect(f"ws://127.0.0.1:{core.port}/v1/workers/ws")
    init = {
        "apikey": key,
        "name": "crash-fixture",
        "models": [MEDIA.get(fmt, MODEL)],
        "job_types": [fmt if fmt in MEDIA else "text"],
        "max_length": 512,
        "max_context_length": 8192,
        "bridge_agent": "crash-fixture/ws:1",
        "api_formats": ["openai-chat", "openai-responses", "anthropic"],
    }
    if fmt == "video":
        init["models"] = [MEDIA[fmt], "LTX-2.3"]  # public alias and recipe checkpoint
    if fmt == "audio":
        audio_identity(core, init)
    await ws.send(json.dumps(init))
    try:
        ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert ready["type"] == "ready", ready
    except BaseException:
        await ws.close()
        raise
    return ws


async def frame(ws, kind):
    async with asyncio.timeout(30):
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            else:
                assert msg["type"] == kind, msg
                return msg


def request(fmt):
    if fmt in MEDIA:
        body = {"model": MEDIA[fmt], "prompt": "Recovery fixture", "seed": 42}
        if fmt == "image":
            body.update(size="1024x1024", steps=8)
        elif fmt == "video":
            body.update(size="768x512", seconds=4, fps=24)
        else:
            body["seconds"] = 10
        resource = {"image": "images", "video": "videos", "audio": "audio"}[fmt]
        return f"/v1/{resource}/generations", body
    body = {"model": MODEL, "max_tokens": 128, "stream": False}
    if fmt == "openai-responses":
        body.pop("max_tokens")
        body.update(input="Say recovery verified.", max_output_tokens=128)
        return "/v1/responses", body
    body["messages"] = [{"role": "user", "content": "Say recovery verified."}]
    return ("/v1/messages" if fmt == "anthropic" else "/v1/chat/completions"), body


async def complete(ws, fmt, core, job, streaming=False):
    if fmt in MEDIA:
        from grid_api.services import ledger, signing

        digest = hashlib.sha256(b"synthetic media fixture").hexdigest()
        payload = job["payload"]
        done = {"type": "done", "results": [{"index": 0, "sha256": digest, "seed": 42}], "recipe_root": payload.get("recipe_root")}
        commitment = {"outputs": [digest], "recipe_root": payload.get("recipe_root")} if fmt == "audio" else [digest]
        done["worker_sig"] = core.signer.sign_message(
            encode_defunct(
                text=signing.signed_message(job["id"], ledger.canonical_hash(commitment)),
            ),
        ).signature.hex()
        await ws.send(json.dumps(done))
        return
    text = "Recovery verified."
    done = {"type": "done", "full_text": text, "usage": {"completion_tokens": 3, "prompt_tokens": 4, "output_tokens": 3, "input_tokens": 4}}
    if fmt == "openai-responses":
        done["full_json"] = {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
    elif fmt == "anthropic":
        done["full_json"] = {"content": [{"type": "text", "text": text}]}
    if streaming:
        if fmt == "openai-chat":
            await ws.send(json.dumps({"type": "token", "text": text}))
        else:
            data = (
                {"type": "response.output_text.delta", "delta": text}
                if fmt == "openai-responses"
                else {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
            )
            await ws.send(json.dumps({"type": "raw", "event": data["type"], "data": json.dumps(data)}))
    await ws.send(json.dumps(done))


async def snapshot(engine, account):
    async with engine.connect() as conn:
        holds = (await conn.execute(sa.select(tables.reservations))).mappings().all()
        ledger = (await conn.execute(sa.select(tables.ledger))).mappings().all()
        moves = (await conn.execute(sa.select(tables.credit_ledger).order_by(tables.credit_ledger.c.id))).mappings().all()
        balance = await conn.scalar(sa.select(tables.credits.c.balance_micro).where(tables.credits.c.account_id == account))
    assert balance == sum(row["delta_micro"] for row in moves)
    assert balance >= 0
    return holds, ledger, moves, balance


@pytest.mark.parametrize(
    "fmt,streaming",
    [
        *[(fmt, streaming) for fmt in ("openai-chat", "openai-responses", "anthropic") for streaming in (False, True)],
        *[(fmt, False) for fmt in MEDIA],
    ],
)
@pytest.mark.parametrize("phase", ["reserved", "dispatched", "committed"])
async def test_core_sigkill_then_restart_preserves_money(rig, fmt, streaming, phase):
    core, engine, queue, account, key, worker_key = rig
    await core.kill()
    await core.start(phase)
    ws = await worker(core, worker_key, fmt)
    stream = "grid:jobs:media" if fmt in MEDIA else "grid:jobs:text"
    pending = None
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=90) as client:
            path, body = request(fmt)
            if fmt not in MEDIA:
                body["stream"] = streaming
            else:
                body["progress_token"] = "crash-" + secrets.token_hex(16)
            pending = asyncio.create_task(client.post(core.url + path, json=body, headers={"Authorization": "Bearer " + key}))
            if phase != "reserved":
                job = await frame(ws, "job")
                if phase == "committed":
                    await complete(ws, fmt, core, job, streaming)
            if phase != "dispatched":

                async def barrier():
                    assert not pending.done(), pending.result().text if pending.done() else ""
                    return (core.path / "barrier").exists()

                await eventually(barrier)
            before = await snapshot(engine, account)
            holds, ledger, moves, balance = before
            assert len(holds) == 1
            hold = holds[0]
            assert hold["reserved_micro"] > 0
            pel = await queue.xpending(stream, "grid:workers")
            assert pel["pending"] == (0 if phase == "reserved" else 1)
            assert hold["status"] == ("settled" if phase == "committed" else "held")
            assert len(ledger) == (1 if phase == "committed" else 0)
            if phase != "reserved":
                assert hold["job_id"] == job["id"]
            assert await core.kill() == -signal.SIGKILL
            # The HTTP socket really dies; no successful response was delivered.
            with pytest.raises(httpx.TransportError):
                await pending
            await ws.close()
            await core.start(orphan=phase == "reserved")
            if phase == "reserved":

                async def refunded():
                    state = await snapshot(engine, account)
                    return state if state[0][0]["status"] == "settled" else None

                after = await eventually(refunded)
                assert after[1] == [] and after[3] == INITIAL
                assert after[2][-1]["reason"] == "release:failed"
                assert after[2][-1]["delta_micro"] == hold["reserved_micro"]
                assert len(after[2]) == 3  # seed, reserve, refund
                assert await queue.xlen(stream) == 0
            else:
                ws = await worker(core, worker_key, fmt)
                retry = await frame(ws, "job")
                assert retry["id"] == job["id"]
                await complete(ws, fmt, core, retry, streaming)
                ack = await frame(ws, "ack")
                assert ack["id"] == job["id"]
                assert (ack["den"] == 0) if phase == "committed" else (ack["den"] > 0)

                async def drained():
                    return (await queue.xpending(stream, "grid:workers"))["pending"] == 0

                await eventually(drained)
                after = await snapshot(engine, account)
                assert after[0][0]["status"] == "settled"
                assert len(after[1]) == 1 and after[1][0]["den"] > 0
                assert 0 < after[0][0]["actual_micro"] <= hold["reserved_micro"]
                assert after[3] == INITIAL - after[0][0]["actual_micro"]
                if fmt in MEDIA:
                    assert after[0][0]["actual_micro"] == hold["reserved_micro"]
                    recovered = await client.get(
                        core.url + "/v1/media/results", params={"job_id": job["id"]}, headers={"Authorization": "Bearer " + key},
                    )
                    assert recovered.status_code == 200, recovered.text
                    assert recovered.json()["state"] == "completed", recovered.json()
                    by_ref = await client.get(
                        core.url + "/v1/media/results",
                        params={"client_ref": body["progress_token"]},
                        headers={"Authorization": "Bearer " + key},
                    )
                    assert by_ref.status_code == 200 and by_ref.json() == recovered.json()
                    if fmt == "audio":
                        assert after[1][0]["worker_sig"]
                if phase == "committed":
                    assert after == before
            # A second restart cannot replay a refund or settled payout either.
            await core.kill()
            await core.start(orphan=True)
            assert await snapshot(engine, account) == after
    finally:
        await core.kill()
        await ws.close()
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.TransportError):
                await pending

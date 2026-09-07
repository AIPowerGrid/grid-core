# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in real PG/Core/Console/node consent, with synthetic earned allocations.

Use disposable VALIDATORS_TEST_DB_URL, VALIDATOR_NODE_SOURCE and a built,
env-file-free VALIDATOR_CONSOLE_SOURCE. No external wallet, Google, RPC, real
inference, transfer or production data is used. Each test owns one PG schema.
"""

import asyncio
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
import uvicorn
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI

from grid_api import auth, redis_client
from grid_api.ratelimit import limiter
from grid_api.routers import accounts as account_router
from grid_api.routers import validator as validator_router
from grid_api.routers import validator_compensation as compensation_router
from grid_api.services import accounts
from grid_api.services.tests import test_validator_compensation_postgres as fixtures
from grid_api.services.tests.test_validator_compensation_operator import service, setup
from grid_api.services.tests.test_validator_compensation_postgres import PG, comp, tables
from grid_api.services.tests.test_validator_compensation_postgres import db as db

NODE = os.environ.get("VALIDATOR_NODE_SOURCE", "")
CONSOLE = os.environ.get("VALIDATOR_CONSOLE_SOURCE", "")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG.startswith("postgresql") or not NODE or not CONSOLE, reason="disposable PG and reviewed node/built Console sources required",
    ),
]


class NonceStore:
    """Single-process nonce fixture, not proof of production Redis delivery."""

    def __init__(self):
        self.items = {}

    async def set(self, key, value, *, ex):
        self.items[key] = (value, time.monotonic() + ex)

    async def get(self, key):
        value, deadline = self.items.get(key, (None, 0))
        return value if deadline > time.monotonic() else None

    async def getdel(self, key):
        value = await self.get(key)
        self.items.pop(key, None)
        return value


async def test_real_wallet_login_exact_consent_and_lost_node_ack(db, monkeypatch):
    source = Path(CONSOLE).resolve()
    assert (source / ".next/BUILD_ID").is_file(), "Build the reviewed Console first"
    for name in (".env", ".env.local", ".env.production", ".env.production.local"):
        assert not (source / name).exists(), "Use an env-file-free Console worktree"
    node_exe = shutil.which("node")
    assert node_exe, "Node.js required for real Console HTTP test"
    monkeypatch.syspath_prepend(str(Path(NODE).resolve()))
    from validator.account_pairing import Identity
    from validator.compensation import CompensationController

    # Move the synthetic seven-day earning window into the recent past, then
    # use wall-clock time for real browser/node expiry checks on new consent.
    start = datetime.now(UTC) - timedelta(days=8)
    monkeypatch.setattr(fixtures, "START", start)
    monkeypatch.setattr(fixtures, "END", start + timedelta(days=7))
    members, humans, allocation, settings = await setup(db, monkeypatch)
    monkeypatch.setattr(comp, "_now", lambda: datetime.now(UTC))
    monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", secrets.token_hex(32))
    monkeypatch.setenv("GRID_SALT", secrets.token_hex(16))
    monkeypatch.setenv("GRID_SIWE_ALLOWED_DOMAINS", "console.aipowergrid.io")
    monkeypatch.setenv("GRID_LEGACY_SIWE_VERIFY_ENABLED", "0")
    monkeypatch.setenv("GRID_LEGACY_SESSION_KEYS_ENABLED", "0")
    monkeypatch.setattr(auth, "_API_KEY_SALT", None)
    nonces = NonceStore()
    monkeypatch.setattr(redis_client, "get_redis", lambda: nonces)
    monkeypatch.setattr(limiter, "enabled", False)
    human_signer, recipient = Account.create(), Account.create()
    validator_id, node_account, node_signer = members[0]
    node_key, service_key = accounts.generate_api_key(), accounts.generate_api_key()
    service_account = uuid4()
    async with db() as session:
        await session.execute(
            sa.update(tables.accounts).where(tables.accounts.c.id == humans[0]).values(wallet=human_signer.address.lower()),
        )
        await session.execute(sa.insert(tables.accounts).values(id=service_account, flags={}))
        await session.execute(
            sa.insert(tables.service_clients).values(
                id="compensation-console-test",
                account_id=service_account,
                name="Disposable consent Console",
                allowed_providers=["app"],
            ),
        )
        for key, account, kind, scopes, client in (
            (node_key, node_account, "api", accounts.VALIDATOR_SCOPES, None),
            (service_key, service_account, "service", accounts.SERVICE_SCOPES, "compensation-console-test"),
        ):
            await session.execute(
                sa.insert(tables.api_keys).values(
                    hash=auth.hash_api_key(key),
                    account_id=account,
                    key_kind=kind,
                    scopes=scopes,
                    service_id=client,
                    is_session=False,
                    revoked=False,
                ),
            )
        await session.commit()

    app = FastAPI()
    for router in (account_router.router, validator_router.router, compensation_router.router):
        app.include_router(router)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    core_origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(app, lifespan="off", access_log=False, log_level="critical", log_config=None, timeout_graceful_shutdown=5),
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    console = None
    controllers = []
    try:
        for _ in range(100):
            if server.started:
                break
            assert not server_task.done(), "Fixture Core exited before readiness"
            await asyncio.sleep(0.05)
        assert server.started
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            port = port_probe.getsockname()[1]
        origin = f"http://localhost:{port}"
        with tempfile.TemporaryDirectory(prefix="validator-payout-console-") as home, tempfile.TemporaryFile() as output:
            console = subprocess.Popen(
                [node_exe, str(source / "node_modules/next/dist/bin/next"), "start", "--hostname", "127.0.0.1", "--port", str(port)],
                cwd=source,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": home,
                    "NODE_ENV": "production",
                    "NEXT_TELEMETRY_DISABLED": "1",
                    "AUTH_URL": origin,
                    "NEXTAUTH_URL": origin,
                    "AUTH_TRUST_HOST": "true",
                    "AUTH_SECRET": secrets.token_hex(32),
                    "GRID_API_BASE": core_origin,
                    "GRID_SERVICE_API_KEY": service_key,
                },
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            async with (
                httpx.AsyncClient(base_url=origin, trust_env=False, timeout=15) as browser,
                httpx.AsyncClient(base_url=core_origin, trust_env=False, timeout=10) as core,
            ):
                for _ in range(100):
                    assert console.poll() is None, "Fixture Console exited before readiness"
                    try:
                        if (await browser.get("/api/auth/csrf")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Fixture Console did not become ready")

                loop = asyncio.get_running_loop()
                lost_ack = False
                confirm_posts = 0

                async def dispatch(request):
                    assert request.url.host == "api.aipowergrid.io"
                    assert request.headers["apikey"] == node_key
                    return await core.request(
                        request.method, request.url.raw_path.decode(), headers=request.headers, content=request.content,
                    )

                def transport(request):
                    nonlocal confirm_posts
                    response = asyncio.run_coroutine_threadsafe(dispatch(request), loop).result(timeout=15)
                    if request.url.path.endswith("/confirm"):
                        confirm_posts += 1
                        if lost_ack:
                            assert response.status_code == 200
                            raise httpx.ReadError("synthetic confirmation response loss after commit")
                    return httpx.Response(response.status_code, headers=response.headers, stream=httpx.ByteStream(response.content))

                identity = Identity(node_signer.address.lower(), node_key, node_signer.key.hex())

                def controller():
                    result = CompensationController(lambda: identity, httpx.MockTransport(transport))
                    controllers.append(result)
                    return result

                async def act(client, action, **fields):
                    status, value = await asyncio.to_thread(client.perform, {"action": action, **fields})
                    assert status == 200
                    return value

                node = controller()
                status = await act(node, "refresh", offset=0)
                assert status["status"] == "ready", status.get("error")
                assert status["validator_id"] == validator_id
                assert status["items"][0]["allocation_hash"] == allocation
                started = await act(node, "start", allocation_hash=allocation)
                request_id = started["request"]["request_id"]
                path = f"/api/validator-compensation/{request_id}"
                assert (await browser.get(path)).status_code == 401

                headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin"}
                response = await browser.post("/api/auth/nonce", headers=headers, json={"address": human_signer.address})
                assert response.status_code == 200
                challenge = response.json()
                csrf = (await browser.get("/api/auth/csrf")).json()["csrfToken"]
                response = await browser.post(
                    "/api/auth/callback/web3",
                    headers=headers,
                    data={
                        "csrfToken": csrf,
                        "callbackUrl": origin + "/dashboard",
                        "address": human_signer.address,
                        "nonce": challenge["nonce"],
                        "message": challenge["message"],
                        "signature": human_signer.sign_message(encode_defunct(text=challenge["message"])).signature.hex(),
                    },
                )
                assert response.status_code == 302 and urlsplit(response.headers["location"]).path == "/dashboard", (
                    "Real wallet callback failed"
                )
                browser_session = await browser.get("/api/auth/session")
                assert browser_session.json()["user"]["gridAccountId"] == str(humans[0])
                assert "gridAccessToken" not in browser_session.text and node_key not in browser_session.text
                response = await browser.get(path)
                assert response.status_code == 200
                assert response.json()["status"] == "awaiting_wallet"
                assert confirm_posts == 0

                denied = await browser.post(
                    path + "/prepare", headers={"Origin": "https://attacker.example"}, json={"recipient": recipient.address.lower()},
                )
                assert denied.status_code == 403
                response = await browser.post(path + "/prepare", headers=headers, json={"recipient": recipient.address.lower()})
                assert response.status_code == 200, "Real Core/Console consent contract rejected"
                prepared = response.json()
                assert prepared["consent"]["recipient"] == recipient.address.lower()
                assert prepared["consent"]["validator_id"] == validator_id
                assert prepared["payment_authorized"] is False
                assert (await act(node, "inspect", request_id=request_id))["request"]["status"] == "awaiting_wallet"
                assert confirm_posts == 0
                wallet_signature = "0x" + bytes(recipient.sign_message(encode_defunct(text=prepared["message"])).signature).hex()
                response = await browser.post(
                    path + "/approve", headers=headers, json={"review_hash": prepared["review_hash"], "signature": wallet_signature},
                )
                assert response.status_code == 200 and response.json()["status"] == "awaiting_node"

                # Reopening/login and restarting the node only read status. The
                # actual node must freshly inspect before it can sign anything.
                assert (await browser.get(path)).json()["status"] == "awaiting_node"
                reviewed = (await act(node, "inspect", request_id=request_id))["request"]
                assert reviewed["review_hash"] == prepared["review_hash"]
                fresh_node = controller()
                refused = await act(fresh_node, "confirm", request_id=request_id, review_hash=reviewed["review_hash"])
                assert refused["error"] == "changed" and confirm_posts == 0
                lost_ack = True
                uncertain = await act(node, "confirm", request_id=request_id, review_hash=reviewed["review_hash"])
                assert uncertain["status"] == "error" and confirm_posts == 1
                assert (await browser.get(path)).json()["status"] == "review_required"
                recovered = await act(fresh_node, "inspect", request_id=request_id)
                assert recovered["request"]["status"] == "review_required"
                assert confirm_posts == 1
                duplicate = await act(fresh_node, "confirm", request_id=request_id, review_hash=reviewed["review_hash"])
                assert duplicate["error"] == "changed" and confirm_posts == 1
                exported = await service.export_for_review(request_id, "approval:handoff-fixture")
                assert exported["consent"] == prepared["consent"]
                async with db() as session:
                    for table in (
                        tables.validator_compensation_recipients,
                        tables.validator_compensation_payments,
                        tables.payouts,
                        tables.credit_ledger,
                        tables.ledger,
                    ):
                        assert await session.scalar(sa.select(sa.func.count()).select_from(table)) == 0
                    assert await session.scalar(sa.select(sa.func.count()).select_from(tables.validator_compensation_requests)) == 1
    finally:
        for client in controllers:
            client.close()
        if console is not None and console.poll() is None:
            console.terminate()
            try:
                await asyncio.to_thread(console.wait, timeout=10)
            except subprocess.TimeoutExpired:
                console.kill()
                await asyncio.to_thread(console.wait, timeout=5)
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=10)
        except TimeoutError:
            server_task.cancel()
            await asyncio.gather(server_task, return_exceptions=True)
        listener.close()

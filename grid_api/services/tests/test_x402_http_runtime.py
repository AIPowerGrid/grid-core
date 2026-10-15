# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real x402 SDK/FastAPI wiring; facilitator and worker outputs are synthetic.

No network, wallet, GPU or on-chain settlement is exercised by these tests.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from x402.http import (
    decode_payment_required_header,
    decode_payment_response_header,
    encode_payment_signature_header,
)
from x402.http.middleware.fastapi import set_settlement_overrides
from x402.schemas import PaymentPayload, SettleResponse, SupportedResponse, VerifyResponse

from grid_api import database
from grid_api.routers import openai
from grid_api.services import credits, deposits, pricing, x402_payments
from grid_api.v2.schema import accounts, credit_ledger, metadata, reservations
from grid_api.v2.schema import x402_payments as payments

MODEL = "gpt-oss-120b"
PAYER = "0x1111111111111111111111111111111111111111"
USDC = "0x2222222222222222222222222222222222222222"
TREASURY = "0x3333333333333333333333333333333333333333"
FACILITATOR = "0x4444444444444444444444444444444444444444"
TX = "0x" + "ab" * 32


async def payment_rows():
    async with await database.new_session() as session:
        return (await session.execute(sa.select(payments))).mappings().all()


@pytest_asyncio.fixture
async def runtime(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    monkeypatch.setattr(
        database,
        "_session_factory",
        async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        ),
    )
    for name, value in {
        "ENABLED": True,
        "NETWORK": "eip155:84532",
        "USDC": USDC,
        "PAY_TO": TREASURY,
        "MAX_AUTH_MICRO": 50_000,
        "CDP_API_KEY_ID": "",
        "CDP_API_KEY_SECRET": "",
    }.items():
        monkeypatch.setattr(x402_payments, name, value)
    monkeypatch.setattr(openai.limiter, "enabled", False)
    observed = SimpleNamespace(verify_calls=0, settlements=[], dispatches=0, valid=True, settle_mode="success")

    class Facilitator:
        def get_supported(self):
            return SupportedResponse(
                kinds=[
                    {
                        "x402Version": 2,
                        "scheme": "upto",
                        "network": "eip155:84532",
                        "extra": {"facilitatorAddress": FACILITATOR},
                    },
                ],
                extensions=[],
                signers={},
            )

        async def verify(self, payload, requirements):
            observed.verify_calls += 1
            assert requirements.amount == "50000"
            return VerifyResponse(is_valid=observed.valid, payer=PAYER, invalid_reason=None if observed.valid else "invalid_signature")

        async def settle(self, payload, requirements):
            (row,) = await payment_rows()
            assert row["status"] == "settling"
            assert row["attempts"] == 1
            assert row["settled_micro"] == int(requirements.amount)
            assert payload.accepted.amount == "50000"
            observed.settlements.append(int(requirements.amount))
            if observed.settle_mode == "timeout":
                raise httpx.ReadTimeout("synthetic uncertain settlement")
            success = observed.settle_mode == "success"
            return SettleResponse(
                success=success,
                payer=PAYER,
                transaction=TX if success else "",
                network="eip155:84532",
                error_reason=None if success else "synthetic_settlement_failure",
            )

    import x402.http

    monkeypatch.setattr(x402.http, "HTTPFacilitatorClient", lambda _: Facilitator())

    async def synthetic_worker(body, user, *, x402_payment, x402_response):
        assert user == {"wallet": PAYER, "billing_source": "x402"}
        job = str(uuid.uuid4())
        authorized = await credits.authorize_x402_request(
            MODEL,
            100,
            500,
            job,
            payment_payload=x402_payment[0],
            payment_requirements=x402_payment[1],
        )
        if not authorized["ok"]:
            raise HTTPException(status_code=402, detail=authorized["reason"])
        observed.dispatches += 1
        actual = pricing.quote_text(MODEL, 100, 50)
        async with await database.new_session() as session:
            await session.execute(
                sa.update(reservations)
                .where(
                    reservations.c.job_id == job,
                )
                .values(status="settled", actual_micro=actual, settled=datetime.now(UTC)),
            )
            await session.commit()
        x402_response.headers["X-Grid-Job-ID"] = job
        set_settlement_overrides(x402_response, {"amount": str(actual)})
        return {"choices": [{"message": {"role": "assistant", "content": "synthetic reply"}}]}

    monkeypatch.setattr(openai, "_handle_chat_completions_for_user", synthetic_worker)
    app = FastAPI()
    app.state.limiter = openai.limiter
    app.include_router(openai.router)
    x402_payments.install_middleware(app)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, observed
    finally:
        await engine.dispose()


async def challenge(client):
    response = await client.post(x402_payments.ROUTE, json={"model": MODEL, "messages": []})
    assert response.status_code == 402
    required = decode_payment_required_header(response.headers["payment-required"])
    (requirement,) = required.accepts
    assert requirement.scheme == "upto"
    assert requirement.network == "eip155:84532"
    assert requirement.amount == "50000"
    assert requirement.pay_to == TREASURY
    assert requirement.extra["facilitatorAddress"] == FACILITATOR
    payload = PaymentPayload(x402_version=2, accepted=requirement, payload={"permit2Authorization": {"from": PAYER, "nonce": "12345"}})
    return {"payment-signature": encode_payment_signature_header(payload)}


@pytest.mark.asyncio
async def test_real_middleware_challenges_before_dispatch(runtime):
    client, observed = runtime
    await challenge(client)
    assert observed.dispatches == observed.verify_calls == 0
    assert await payment_rows() == []


@pytest.mark.asyncio
async def test_real_middleware_settles_only_actual_and_blocks_replay(runtime):
    client, observed = runtime
    headers = await challenge(client)
    body = {"model": MODEL, "messages": [{"role": "user", "content": "test"}], "stream": False}
    response = await client.post(x402_payments.ROUTE, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "synthetic reply"
    settled = decode_payment_response_header(response.headers["payment-response"])
    assert settled.success and settled.transaction == TX
    actual = pricing.quote_text(MODEL, 100, 50)
    assert observed.settlements == [actual]
    assert actual < 50_000
    (row,) = await payment_rows()
    assert row["status"] == "reported"
    assert await x402_payments.settled_amount(row["job_id"]) is None
    repeated = await client.post(x402_payments.ROUTE, json=body, headers=headers)
    assert repeated.status_code == 402
    assert observed.dispatches == 1 and observed.settlements == [actual]
    assert len(await payment_rows()) == 1
    async with await database.new_session() as session:
        assert (await session.execute(sa.select(sa.func.count()).select_from(accounts))).scalar() == 0
        assert (await session.execute(sa.select(sa.func.count()).select_from(credit_ledger))).scalar() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid_signature", "streaming"])
async def test_real_middleware_rejects_without_dispatch_or_settlement(runtime, mode):
    client, observed = runtime
    headers = await challenge(client)
    observed.valid = mode != "invalid_signature"
    response = await client.post(
        x402_payments.ROUTE,
        headers=headers,
        json={
            "model": MODEL,
            "messages": [],
            "stream": mode == "streaming",
        },
    )
    assert response.status_code == (402 if mode == "invalid_signature" else 400)
    assert observed.dispatches == 0 and observed.settlements == []
    assert await payment_rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failure", "timeout"])
async def test_real_middleware_ambiguous_settlement_stays_payout_ineligible(runtime, mode):
    client, observed = runtime
    headers = await challenge(client)
    observed.settle_mode = mode
    response = await client.post(
        x402_payments.ROUTE,
        headers=headers,
        json={
            "model": MODEL,
            "messages": [],
            "stream": False,
        },
    )
    assert response.status_code == 402
    (row,) = await payment_rows()
    assert row["status"] in {"settling", "manual_review"}
    assert await x402_payments.settled_amount(row["job_id"]) is None
    await client.post(x402_payments.ROUTE, headers=headers, json={"model": MODEL, "messages": []})
    assert observed.dispatches == len(observed.settlements) == 1
    async with await database.new_session() as session:
        await session.execute(
            sa.update(payments).values(
                last_attempt=datetime.now(UTC) - timedelta(hours=1),
            ),
        )
        await session.commit()
    await x402_payments.flag_stale_settlements()
    (row,) = await payment_rows()
    assert row["status"] == "manual_review"


@pytest.mark.asyncio
async def test_real_middleware_rejects_malformed_payment_without_dispatch(runtime):
    client, observed = runtime
    response = await client.post(
        x402_payments.ROUTE,
        json={"model": MODEL, "messages": []},
        headers={"payment-signature": "not-a-payment-header"},
    )
    assert response.status_code == 402
    assert observed.dispatches == observed.verify_calls == 0
    assert observed.settlements == [] and await payment_rows() == []


@pytest.mark.asyncio
async def test_real_middleware_does_not_settle_when_attempt_cannot_be_persisted(runtime, monkeypatch):
    client, observed = runtime
    headers = await challenge(client)

    async def unavailable_database():
        raise RuntimeError("synthetic before-settle database outage")

    monkeypatch.setattr(x402_payments, "new_session", unavailable_database)
    response = await client.post(x402_payments.ROUTE, headers=headers, json={"model": MODEL, "messages": []})
    assert response.status_code == 402
    (row,) = await payment_rows()
    assert row["status"] == "verified" and row["attempts"] == 0
    assert observed.dispatches == 1 and observed.settlements == []


@pytest.mark.asyncio
async def test_real_middleware_lost_receipt_stays_ineligible_without_secret_logging(runtime, monkeypatch, caplog):
    client, observed = runtime
    headers = await challenge(client)

    async def fail_after_transfer():
        if observed.settlements:
            raise RuntimeError("synthetic-sensitive-database-value")
        return await database.new_session()

    monkeypatch.setattr(x402_payments, "new_session", fail_after_transfer)
    response = await client.post(x402_payments.ROUTE, headers=headers, json={"model": MODEL, "messages": []})
    assert response.status_code == 402
    (row,) = await payment_rows()
    assert row["status"] == "settling" and row["attempts"] == 1
    assert row["settled_micro"] == pricing.quote_text(MODEL, 100, 50)
    assert row["tx_hash"] is None
    repeated = await client.post(x402_payments.ROUTE, headers=headers, json={"model": MODEL, "messages": []})
    assert repeated.status_code == 402
    assert observed.dispatches == len(observed.settlements) == 1
    logs = [record for record in caplog.records if record.name == "grid_api.x402"]
    assert logs and any("receipt could not be persisted" in record.getMessage() for record in logs)
    assert all(record.exc_info is None for record in logs)
    assert all(row["job_id"] not in record.getMessage() for record in logs)
    assert all("synthetic-sensitive-database-value" not in record.getMessage() for record in logs)
    monkeypatch.setattr(x402_payments, "new_session", database.new_session)
    assert await x402_payments.settled_amount(row["job_id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "rpc_chain", "verifier_chain", "asset", "unsupported"])
async def test_preflight_is_read_only_and_rejects_network_mismatches(runtime, monkeypatch, failure):
    _, observed = runtime
    monkeypatch.setattr(x402_payments, "ENABLED", False)
    canonical = "0x036cbd53842c5426634e7929541ec2318f3dcf7e"
    monkeypatch.setattr(x402_payments, "USDC", canonical)
    monkeypatch.setattr(deposits, "USDC", USDC if failure == "asset" else canonical)
    monkeypatch.setattr(deposits, "CHAIN_ID", 8453 if failure == "verifier_chain" else 84532)

    async def rpc(method, params):
        assert method == "eth_chainId" and params == []
        return hex(8453 if failure == "rpc_chain" else 84532)

    async def forbidden_database():
        raise AssertionError("preflight must never connect to the database")

    monkeypatch.setattr(deposits, "_rpc", rpc)
    monkeypatch.setattr(x402_payments, "new_session", forbidden_database)
    if failure == "unsupported":

        class Unsupported:
            def get_supported(self):
                return SupportedResponse(kinds=[], extensions=[], signers={})

        import x402.http

        monkeypatch.setattr(x402.http, "HTTPFacilitatorClient", lambda _: Unsupported())
    if failure:
        with pytest.raises(RuntimeError):
            await x402_payments.preflight()
    else:
        report = await x402_payments.preflight()
        assert report == {
            "status": "preflight_passed",
            "enabled": False,
            "network": "eip155:84532",
            "scheme": "upto",
            "asset": "USDC",
            "max_authorized_micro": 50_000,
            "receipt_rpc_chain": 84532,
            "facilitator_support": True,
            "payments_sent": 0,
            "inference_dispatched": 0,
        }
    assert not x402_payments.ENABLED
    assert observed.dispatches == observed.verify_calls == 0
    assert observed.settlements == []


def test_preflight_checks_configuration_even_while_disabled(monkeypatch):
    monkeypatch.setattr(x402_payments, "ENABLED", False)
    monkeypatch.setattr(x402_payments, "NETWORK", "eip155:8453")
    monkeypatch.setattr(x402_payments, "PAY_TO", TREASURY)
    monkeypatch.setattr(x402_payments, "USDC", USDC)
    monkeypatch.setattr(x402_payments, "CDP_API_KEY_ID", "")
    monkeypatch.setattr(x402_payments, "CDP_API_KEY_SECRET", "")
    x402_payments.validate_config()
    with pytest.raises(RuntimeError, match="CDP_API_KEY_ID"):
        x402_payments.validate_config(force=True)


def test_preflight_cli_failure_does_not_expose_exception(monkeypatch, capsys):
    async def failing_preflight():
        raise RuntimeError("synthetic-sensitive-facilitator-response")

    monkeypatch.setattr(x402_payments, "preflight", failing_preflight)
    assert x402_payments._main(["--preflight"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "preflight_failed", "error_type": "RuntimeError"}
    assert output.err == ""


def test_preflight_cli_success_and_reconcile_remain_separate(monkeypatch, capsys):
    calls = []

    async def successful_preflight():
        calls.append("preflight")
        return {"status": "preflight_passed", "payments_sent": 0}

    async def reconcile(job, transaction):
        calls.append((job, transaction))

    monkeypatch.setattr(x402_payments, "preflight", successful_preflight)
    monkeypatch.setattr(x402_payments, "_run_reconcile_cli", reconcile)
    assert x402_payments._main(["--preflight"]) == 0
    assert json.loads(capsys.readouterr().out)["payments_sent"] == 0
    assert x402_payments._main(["--reconcile-job", "test-job", "--tx", TX]) == 0
    assert calls == ["preflight", ("test-job", TX)]


@pytest.mark.parametrize(
    "args",
    [[], ["--reconcile-job", "test-job"], ["--preflight", "--tx", TX], ["--preflight", "--reconcile-job", "test-job", "--tx", TX]],
)
def test_preflight_cli_rejects_ambiguous_or_incomplete_actions(args):
    with pytest.raises(SystemExit) as error:
        x402_payments._main(args)
    assert error.value.code == 2

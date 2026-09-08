# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from grid_api.ratelimit import limiter
from grid_api.routers import validator_compensation as router
from grid_api.services import validator_compensation_operator as service
from grid_api.services import validator_compensation_recipients as recipients
from grid_api.services.tests.test_validator_compensation_postgres import PG, close_time, comp, create, members, report, tables
from grid_api.services.tests.test_validator_compensation_postgres import db as db

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not PG.startswith("postgresql"), reason="disposable PostgreSQL required")]


async def setup(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    for node in group:
        await report(db, node)
    close_time(monkeypatch)
    result = await comp.finalize_campaign("pilot-fixture")
    await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=result["digest"])
    settings = SimpleNamespace(validator_compensation_operator_enabled=True, validator_pairing_enabled=True)
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(service.pairing, "get_settings", lambda: settings)
    humans = []
    async with db() as session:
        for node in group:
            human = uuid4()
            humans.append(human)
            await session.execute(sa.insert(tables.accounts).values(id=human))
            await session.execute(
                sa.insert(tables.validator_account_links).values(
                    validator_id=node[0],
                    operator_account_id=human,
                    node_account_id=node[1],
                    signing_wallet=node[2].address.lower(),
                    pairing_id="vpa_" + uuid4().hex * 2,
                    payload={},
                    signature="fixture",
                    linked_at=comp._now(),
                ),
            )
        await session.commit()
    status = await service.status(group[0][1], group[0][2].address.lower())
    return group, humans, status["items"][0]["allocation_hash"], settings


def sign(wallet, message):
    return "0x" + wallet.sign_message(encode_defunct(text=message)).signature.hex()


async def prepared(db, monkeypatch):
    group, humans, allocation, settings = await setup(db, monkeypatch)
    node, human, recipient = group[0], humans[0], Account.create()
    slot = await service.start(node[1], node[2].address.lower(), allocation)
    view = await service.prepare(human, slot["request_id"], recipient.address.lower())
    return node, human, recipient, view, group, humans, settings


async def approve_wallet(human, recipient, view):
    return await service.approve(human, None, view["request_id"], view["review_hash"], sign(recipient, view["message"]), human=True)


async def test_both_signatures_only_collect_review_not_money(db, monkeypatch):
    node, human, recipient, view, _, _, _ = await prepared(db, monkeypatch)
    assert (await approve_wallet(human, recipient, view))["status"] == "awaiting_node"
    proof = sign(node[2], view["message"])
    confirmed = await service.approve(node[1], node[2].address.lower(), view["request_id"], view["review_hash"], proof)
    assert confirmed["status"] == "review_required" and confirmed["payment_authorized"] is False
    assert await service.approve(node[1], node[2].address.lower(), view["request_id"], view["review_hash"], proof) == confirmed
    async with db() as session:
        for table in (
            tables.validator_compensation_recipients,
            tables.validator_compensation_payments,
            tables.payouts,
            tables.credit_ledger,
            tables.ledger,
        ):
            assert await session.scalar(sa.select(sa.func.count()).select_from(table)) == 0
    exported = await service.export_for_review(view["request_id"], "approval:fixture-review")
    preview = await recipients.bind_recipient(exported)
    await recipients.bind_recipient(exported, apply=True, expected_digest=preview["digest"])
    assert (await service.inspect(human, None, view["request_id"], human=True))["status"] == "recipient_bound"
    item = (await service.status(node[1], node[2].address.lower()))["items"][0]
    assert item["status"] == "ready_for_payment" and item["transaction_hash"] is None
    assert item["recipient"] == recipient.address.lower()


async def test_cross_account_reads_and_writes_are_rejected(db, monkeypatch):
    node, human, recipient, view, group, humans, _ = await prepared(db, monkeypatch)
    for identity, wallet, is_human in ((humans[1], None, True), (group[1][1], group[1][2].address.lower(), False)):
        with pytest.raises(service.OperatorError, match="request_not_found"):
            await service.inspect(identity, wallet, view["request_id"], human=is_human)
        with pytest.raises(service.OperatorError, match="request_not_found"):
            await service.approve(
                identity,
                wallet,
                view["request_id"],
                view["review_hash"],
                sign(recipient, view["message"]),
                human=is_human,
            )
    with pytest.raises(service.OperatorError, match="allocation_not_found"):
        await service.start(group[1][1], group[1][2].address.lower(), view["allocation_hash"])


async def test_node_cannot_confirm_before_wallet_or_sign_for_another_destination(db, monkeypatch):
    node, human, recipient, view, _, _, _ = await prepared(db, monkeypatch)
    with pytest.raises(service.OperatorError, match="consent_already_signed"):
        await service.approve(node[1], node[2].address.lower(), view["request_id"], view["review_hash"], sign(node[2], view["message"]))
    with pytest.raises(service.OperatorError, match="invalid_signature"):
        await service.approve(node[1], node[2].address.lower(), view["request_id"], view["review_hash"], sign(recipient, view["message"]))


async def test_prepare_change_invalidates_old_review_and_freezes_after_signature(db, monkeypatch):
    node, human, recipient, view, _, _, _ = await prepared(db, monkeypatch)
    other = Account.create()
    current = await service.prepare(human, view["request_id"], other.address.lower())
    with pytest.raises(service.OperatorError, match="consent_changed"):
        await approve_wallet(human, recipient, view)
    await approve_wallet(human, other, current)
    with pytest.raises(service.OperatorError, match="consent_already_signed"):
        await service.prepare(human, view["request_id"], recipient.address.lower())


@pytest.mark.parametrize("change", ["unlink", "new_link", "retire_human", "rotate_node", "disable"])
async def test_link_identity_and_gate_changes_prevent_approval(db, monkeypatch, change):
    node, human, recipient, view, _, humans, settings = await prepared(db, monkeypatch)
    async with db() as session:
        if change == "unlink":
            await session.execute(
                sa.update(tables.validator_account_links)
                .where(tables.validator_account_links.c.validator_id == node[0])
                .values(revoked_at=comp._now()),
            )
        elif change == "new_link":
            await session.execute(
                sa.update(tables.validator_account_links)
                .where(tables.validator_account_links.c.validator_id == node[0])
                .values(pairing_id="vpa_" + "aa" * 32),
            )
        elif change == "retire_human":
            await session.execute(
                sa.insert(tables.account_aliases).values(
                    source_account_id=human,
                    canonical_account_id=humans[1],
                    reason="fixture",
                    merge_ref="fixture-merge",
                ),
            )
        elif change == "rotate_node":
            await session.execute(
                sa.update(tables.validators)
                .where(tables.validators.c.id == node[0])
                .values(signing_wallet=Account.create().address.lower()),
            )
        else:
            settings.validator_compensation_operator_enabled = False
        await session.commit()
    with pytest.raises((service.OperatorError, service.pairing.PairingError, comp.CompensationError)):
        await approve_wallet(human, recipient, view)


async def test_expiry_during_wallet_rpc_cannot_commit_and_old_request_cannot_revive(db, monkeypatch):
    node, human, _, view, _, _, _ = await prepared(db, monkeypatch)

    async def rpc(method, args):
        return "0x2105"

    async def verify(**kwargs):
        monkeypatch.setattr(comp, "_now", lambda: comp._time(view["expires_at"]))
        return True

    monkeypatch.setattr(service.wallet_proofs, "_rpc", rpc)
    monkeypatch.setattr(service.wallet_proofs, "verify_personal_signature", verify)
    with pytest.raises(service.OperatorError, match="expired"):
        await service.approve(human, None, view["request_id"], view["review_hash"], "0x1234", human=True)
    restarted = await service.start(node[1], node[2].address.lower(), view["allocation_hash"])
    assert restarted["request_id"] != view["request_id"]
    with pytest.raises(service.OperatorError, match="request_not_found"):
        await service.inspect(node[1], node[2].address.lower(), view["request_id"])


async def test_cancel_before_node_signature_and_keep_reviewed_proof(db, monkeypatch):
    node, human, recipient, view, _, _, _ = await prepared(db, monkeypatch)
    await approve_wallet(human, recipient, view)
    assert (await service.cancel(node[1], node[2].address.lower(), view["request_id"]))["status"] == "cancelled"
    with pytest.raises(service.OperatorError, match="cancelled"):
        await service.approve(node[1], node[2].address.lower(), view["request_id"], view["review_hash"], sign(node[2], view["message"]))
    replacement = await service.start(node[1], node[2].address.lower(), view["allocation_hash"])
    current = await service.prepare(human, replacement["request_id"], recipient.address.lower())
    await approve_wallet(human, recipient, current)
    await service.approve(
        node[1],
        node[2].address.lower(),
        current["request_id"],
        current["review_hash"],
        sign(node[2], current["message"]),
    )
    with pytest.raises(service.OperatorError, match="maintainer_review_required"):
        await service.cancel(node[1], node[2].address.lower(), current["request_id"])


async def test_repeated_start_and_concurrent_confirm_share_one_request(db, monkeypatch):
    node, human, recipient, view, _, _, _ = await prepared(db, monkeypatch)
    starts = await asyncio.gather(*(service.start(node[1], node[2].address.lower(), view["allocation_hash"]) for _ in range(10)))
    assert {result["request_id"] for result in starts} == {view["request_id"]}
    approved = await asyncio.gather(*(approve_wallet(human, recipient, view) for _ in range(10)))
    assert all(result["status"] == "awaiting_node" for result in approved)
    confirmed = await asyncio.gather(
        *(
            service.approve(node[1], node[2].address.lower(), view["request_id"], view["review_hash"], sign(node[2], view["message"]))
            for _ in range(10)
        ),
    )
    assert all(result["status"] == "review_required" for result in confirmed)
    async with db() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(service.requests)) == 1


async def test_http_auth_body_bounds_privacy_and_complete_flow(db, monkeypatch):
    node, human, recipient, view, _, _, settings = await prepared(db, monkeypatch)
    seen = []

    async def auth(key, required_scope):
        seen.append(required_scope)
        if key == "node":
            return {"source": "v2", "account_id": node[1], "wallet": node[2].address.lower(), "key_kind": "api_key"}
        if key in {"human", "service"}:
            return {"source": "v2", "account_id": human, "key_kind": "user_token" if key == "human" else "api_key", "token_claims": {}}
        raise HTTPException(401, detail="unauthorized")

    monkeypatch.setattr(router.accounts, "authenticate", auth)
    monkeypatch.setattr(router.user_tokens, "require_recent_step_up", lambda claims: None)
    monkeypatch.setattr(limiter, "enabled", False)
    app = FastAPI()
    app.include_router(router.router)
    base = "/v1/account/validator-compensation/requests/" + view["request_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(base)).status_code == 401
        assert (await client.get(base, headers={"apikey": "service"})).status_code == 403
        response = await client.get("/v1/validator/compensation", headers={"apikey": "node"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert not any(
            key in json.dumps(response.json()) for key in ("operator_group_id", "node_signature", "raw_transaction", "account_id")
        )
        oversized = await client.post(base + "/approve", headers={"apikey": "human"}, content=b"x" * 20001)
        assert oversized.status_code == 413 and oversized.headers["cache-control"] == "no-store"
        duplicate = await client.post(base + "/prepare", headers={"apikey": "human"}, content='{"recipient":"a","recipient":"b"}')
        assert duplicate.status_code == 400
        approved = await client.post(
            base + "/approve",
            headers={"apikey": "human"},
            json={"review_hash": view["review_hash"], "signature": sign(recipient, view["message"])},
        )
        assert approved.json()["status"] == "awaiting_node"
        confirm = await client.post(
            "/v1/validator/compensation/requests/" + view["request_id"] + "/confirm",
            headers={"apikey": "node"},
            json={"review_hash": view["review_hash"], "signature": sign(node[2], view["message"])},
        )
        assert confirm.json()["status"] == "review_required"
        settings.validator_compensation_operator_enabled = False
        assert (await client.get(base, headers={"apikey": "human"})).status_code == 503
    assert {"validator.read", "validator.attest", "account.read", "account.manage"}.issubset(seen)


async def test_slow_wallet_rpc_holds_no_node_lock_and_rechecks_changed_link(db, monkeypatch):
    node, human, _, view, _, _, _ = await prepared(db, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def rpc(method, args):
        return "0x2105"

    async def verify(**kwargs):
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(service.wallet_proofs, "_rpc", rpc)
    monkeypatch.setattr(service.wallet_proofs, "verify_personal_signature", verify)
    task = asyncio.create_task(service.approve(human, None, view["request_id"], view["review_hash"], "0x1234", human=True))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            async with db() as session:
                await session.execute(
                    sa.select(tables.validators.c.id).where(tables.validators.c.id == node[0]).with_for_update(nowait=True),
                )
                await session.execute(
                    sa.update(tables.validator_account_links)
                    .where(tables.validator_account_links.c.validator_id == node[0])
                    .values(pairing_id="vpa_" + "cd" * 32),
                )
                await session.commit()
            release.set()
            with pytest.raises(service.OperatorError, match="account_link_changed"):
                await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with db() as session:
        assert await session.scalar(sa.select(service.requests.c.recipient_signature)) is None


@pytest.mark.parametrize("chain", ["0x1", "0x2105"])
async def test_contract_wallet_proof_requires_base_chain(db, monkeypatch, chain):
    _, human, _, view, _, _, _ = await prepared(db, monkeypatch)
    calls = []

    async def rpc(method, args):
        return chain

    async def verify(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(service.wallet_proofs, "_rpc", rpc)
    monkeypatch.setattr(service.wallet_proofs, "verify_personal_signature", verify)
    if chain == "0x1":
        with pytest.raises(service.OperatorError, match="invalid_signature"):
            await service.approve(human, None, view["request_id"], view["review_hash"], "0x1234", human=True)
        assert not calls
    else:
        assert (await service.approve(human, None, view["request_id"], view["review_hash"], "0x1234", human=True))[
            "status"
        ] == "awaiting_node"
        assert len(calls) == 1


async def test_migration_roundtrip_and_pending_proof_preservation(db, monkeypatch):
    group, _, allocation, _ = await setup(db, monkeypatch)
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0039_validator_compensation_requests.py"
    spec = importlib.util.spec_from_file_location("request_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    engine = db.kw["bind"]
    namespace = engine.get_execution_options()["schema_translate_map"][None]

    def roundtrip(connection):
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{namespace}"')
        service.requests.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            columns = sa.inspect(connection).get_columns(service.requests.name, schema=namespace)
            assert {column["name"]: column["nullable"] for column in columns} == {
                column.name: column.nullable for column in service.requests.c
            }
            assert len(sa.inspect(connection).get_check_constraints(service.requests.name, schema=namespace)) == 5
            revision.downgrade()
            revision.upgrade()

    async with engine.begin() as connection:
        await connection.run_sync(roundtrip)
    node = group[0]
    await service.start(node[1], node[2].address.lower(), allocation)

    def refuse(connection):
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{namespace}"')
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="refuse to discard"):
                revision.downgrade()

    async with engine.begin() as connection:
        await connection.run_sync(refuse)


async def test_open_pilot_shows_earning_window_not_fake_earned_balance(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    settings = SimpleNamespace(validator_compensation_operator_enabled=True)
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    node = group[0]
    scheduled = await service.status(node[1], node[2].address.lower())
    assert scheduled["items"] == [] and scheduled["campaigns"][0]["status"] == "scheduled"
    starts = comp._time(scheduled["campaigns"][0]["starts_at"])
    monkeypatch.setattr(comp, "_now", lambda: starts + timedelta(days=1))
    earning = await service.status(node[1], node[2].address.lower())
    assert earning["campaigns"][0]["status"] == "earning" and earning["items"] == []
    monkeypatch.setattr(comp, "_now", lambda: starts + timedelta(days=8))
    awaiting = await service.status(node[1], node[2].address.lower())
    assert awaiting["campaigns"][0]["status"] == "awaiting_finalization" and awaiting["items"] == []


@pytest.mark.parametrize("change", ["expire", "disable", "cancel"])
async def test_blocked_approval_rechecks_state_after_actual_pg_lock(db, monkeypatch, change):
    node, human, recipient, view, _, _, settings = await prepared(db, monkeypatch)
    task = None
    try:
        async with db() as holder:
            await holder.execute(sa.select(tables.validators.c.id).where(tables.validators.c.id == node[0]).with_for_update())
            holder_pid = await holder.scalar(sa.text("SELECT pg_backend_pid()"))
            async with asyncio.timeout(10):
                async with db() as observer:
                    # Force the observer's activity snapshot to precede the contender.
                    await observer.scalar(sa.text("SELECT count(*) FROM pg_stat_activity"))
                    task = asyncio.create_task(approve_wallet(human, recipient, view))
                    while True:
                        # Activity snapshots persist for the observer transaction.
                        await observer.execute(sa.text("SELECT pg_stat_clear_snapshot()"))
                        if await observer.scalar(
                            sa.text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))",
                            ),
                            {"pid": holder_pid},
                        ):
                            break
                        if task.done():
                            await task
                            pytest.fail("approval did not wait on the held node lock")
                        await asyncio.sleep(0.02)
            if change == "expire":
                monkeypatch.setattr(comp, "_now", lambda: comp._time(view["expires_at"]))
            elif change == "disable":
                settings.validator_compensation_operator_enabled = False
            else:
                await holder.execute(
                    sa.update(service.requests).where(service.requests.c.id == view["request_id"]).values(status="cancelled"),
                )
            await holder.commit()
        with pytest.raises(service.OperatorError, match={"expire": "expired", "disable": "unavailable", "cancel": "cancelled"}[change]):
            await asyncio.wait_for(task, 10)
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with db() as session:
        assert await session.scalar(sa.select(service.requests.c.recipient_signature)) is None


async def test_http_step_up_and_validation_storage_errors_are_private(db, monkeypatch):
    _, human, recipient, view, _, _, _ = await prepared(db, monkeypatch)

    async def auth(key, required_scope):
        return {"source": "v2", "account_id": human, "key_kind": "user_token", "token_claims": {}}

    monkeypatch.setattr(router.accounts, "authenticate", auth)
    monkeypatch.setattr(limiter, "enabled", False)
    app = FastAPI()
    app.include_router(router.router)
    base = "/v1/account/validator-compensation/requests/" + view["request_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(base + "/prepare", headers={"apikey": "human"}, json={"recipient": recipient.address.lower()})
        assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
        bad_query = await client.get("/v1/validator/compensation?offset=private-fixture-input")
        assert bad_query.status_code == 400 and "private-fixture-input" not in bad_query.text
        assert bad_query.headers["cache-control"] == "no-store"

        async def unavailable(**kwargs):
            raise SQLAlchemyError("private database parameters must not escape")

        monkeypatch.setattr(service, "inspect", unavailable)
        response = await client.get(base, headers={"apikey": "human"})
        assert response.status_code == 503 and response.json() == {"detail": "compensation_storage_unavailable"}
        assert response.headers["referrer-policy"] == "no-referrer"


async def test_private_cli_export_uses_readonly_connection(db, monkeypatch, tmp_path):
    from scripts import manage_validator_recipient as cli

    node, human, recipient, view, _, _, _ = await prepared(db, monkeypatch)
    await approve_wallet(human, recipient, view)
    await service.approve(node[1], node[2].address.lower(), view["request_id"], view["review_hash"], sign(node[2], view["message"]))
    namespace = db.kw["bind"].get_execution_options()["schema_translate_map"][None]
    engines = []

    def engine(url, *, connect_args):
        assert connect_args["server_settings"]["default_transaction_read_only"] == "on"
        connect_args["server_settings"]["search_path"] = namespace
        result = create_async_engine(url, connect_args=connect_args)
        engines.append(result)
        return result

    original_export = cli.export_for_review

    async def checked_export(**kwargs):
        async with cli.database._session_factory() as session:
            assert await session.scalar(sa.text("SHOW transaction_read_only")) == "on"
        return await original_export(**kwargs)

    monkeypatch.setattr(cli, "create_async_engine", engine)
    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(async_database_url=PG))
    monkeypatch.setattr(cli, "export_for_review", checked_export)

    # The service test fixture redirects all new_session calls; point this
    # command's reads at its own independently readonly factory instead.
    async def command_session():
        return cli.database._session_factory()

    monkeypatch.setattr(service, "new_session", command_session)
    monkeypatch.setattr(recipients, "new_session", command_session)
    path = tmp_path / "request.json"
    cli.write_private(path, {"request_id": view["request_id"], "approval_ref": "approval:fixture-export"})
    exported = await cli.run(SimpleNamespace(input=path, action="export", apply=False, expect_digest=None))
    assert len(engines) == 1 and exported["consent"] == view["consent"]
    assert set(exported) == {"consent", "node_signature", "recipient_signature", "approval_ref"}
    async with db() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(tables.validator_compensation_recipients)) == 0


@pytest.mark.parametrize("state", ["pending", "sent", "manual_review"])
async def test_status_reads_actual_sender_state_without_exposing_signed_transaction(db, monkeypatch, state):
    from grid_api.services.tests import test_validator_payments as sender

    request, eth, _, group = await sender.setup(db, monkeypatch)
    if state == "pending":
        eth.auto_mine = False
    elif state == "manual_review":
        eth.mutate_receipt = lambda receipt: {**receipt, "logs": []}
    result = await sender.execute(request)
    assert result["status"] == state
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(validator_compensation_operator_enabled=True))
    monkeypatch.setattr(sender.pay, "_context", lambda plan: pytest.fail("status must not call Base"))
    node = group[0]
    response = await service.status(node[1], node[2].address.lower())
    assert response["items"][0]["status"] == state
    stored = (await sender.rows(db))[0]
    assert response["items"][0]["transaction_hash"] == stored["tx_hash"]
    serialized = json.dumps(response)
    assert stored["raw_transaction"].hex() not in serialized
    assert not any(key in serialized for key in ("operator_group_id", "node_signature", "raw_transaction", "account_id"))

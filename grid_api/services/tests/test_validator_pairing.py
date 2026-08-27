# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pairing lifecycle and real PostgreSQL race proofs; never use a live DB."""

import asyncio
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import auth, database
from grid_api.ratelimit import limiter
from grid_api.routers import validator_pairing as pairing_router
from grid_api.services import accounts as accounts_svc
from grid_api.services import user_tokens
from grid_api.services import validator_pairing as pairing
from grid_api.v2 import schema as db

NOW = datetime(2026, 8, 27, 17, tzinfo=UTC)
PG = os.environ.get("VALIDATORS_TEST_DB_URL", "")


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def state(request, monkeypatch):
    if request.param == "postgresql" and not PG.startswith("postgresql"):
        pytest.skip("set VALIDATORS_TEST_DB_URL to a disposable PostgreSQL database")
    url = PG if request.param == "postgresql" else "sqlite+aiosqlite://"
    engine = create_async_engine(url, **({"poolclass": StaticPool} if request.param == "sqlite" else {}))
    if request.param == "sqlite":

        @sa.event.listens_for(engine.sync_engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(db.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def new_session():
        return factory()

    async def clock(_session):
        return NOW

    original_clock = pairing._now
    monkeypatch.setattr(pairing, "new_session", new_session)
    monkeypatch.setattr(pairing, "_now", clock)
    settings = SimpleNamespace(
        validator_pairing_audience="https://api.example.test",
        validator_pairing_console_url="https://console.example.test/dashboard/connect-validator",
    )
    monkeypatch.setattr(pairing, "get_settings", lambda: settings)
    signer, other = Account.create(), Account.create()
    aid, operator, attacker, other_aid = (uuid4() for _ in range(4))
    node, other_node = "val_" + uuid4().hex, "val_" + uuid4().hex
    async with factory() as session:
        for account_id, wallet in [(aid, signer.address.lower()), (other_aid, other.address.lower()), (operator, None), (attacker, None)]:
            await session.execute(sa.insert(db.accounts).values(id=account_id, wallet=wallet, payout_wallet="0x" + "a" * 40, flags={}))
            await session.execute(sa.insert(db.credits).values(account_id=account_id, balance_micro=12345))
        for account_id, wallet, vid in [(aid, signer.address.lower(), node), (other_aid, other.address.lower(), other_node)]:
            await session.execute(
                sa.insert(db.validators).values(
                    id=vid,
                    account_id=account_id,
                    signing_wallet=wallet,
                    status="active",
                    registration_signature="fixture",
                    software_version="pairing-test",
                    capabilities=["text.basic.v1"],
                ),
            )
        await session.commit()
    fixture = SimpleNamespace(
        factory=factory,
        engine=engine,
        signer=signer,
        other=other,
        aid=aid,
        operator=operator,
        attacker=attacker,
        node=node,
        other_node=other_node,
        other_aid=other_aid,
        wallet=signer.address.lower(),
        settings=settings,
        original_clock=original_clock,
        dialect=request.param,
    )
    try:
        yield fixture
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(db.metadata.drop_all)
        await engine.dispose()


def _sign(signer, payload):
    return signer.sign_message(encode_defunct(text=json.dumps(payload, sort_keys=True, separators=(",", ":")))).signature.hex()


async def _approved(s):
    created = await pairing.create(account_id=s.aid, wallet=s.wallet)
    approved = await pairing.approve(pairing_id=created["pairing_id"], operator_account_id=s.operator)
    return approved


async def _confirmed(s):
    approved = await _approved(s)
    await pairing.confirm(
        pairing_id=approved["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=_sign(s.signer, approved["payload"]),
    )
    return approved


async def _snapshot(s):
    tables = [db.accounts, db.account_aliases, db.account_identities, db.api_keys, db.credits, db.credit_ledger, db.ledger, db.validators]
    async with s.factory() as session:
        return {t.name: [dict(row) for row in (await session.execute(sa.select(t))).mappings()] for t in tables}


@pytest.mark.asyncio
async def test_two_party_lifecycle_preserves_identity_credit_and_ownership(state):
    s = state
    before = await _snapshot(s)
    created = await pairing.create(account_id=s.aid, wallet=s.wallet)
    assert created["status"] == "pending"
    assert created["expires_at"] == int(NOW.timestamp()) + 600
    assert "payload" not in created and "comparison_code" not in created
    assert created == await pairing.create(account_id=s.aid, wallet=s.wallet)
    assert (await pairing.list_for_account(operator_account_id=s.operator))["nodes"] == []
    approved = await pairing.approve(pairing_id=created["pairing_id"], operator_account_id=s.operator)
    assert approved["status"] == "approved"
    assert approved == await pairing.approve(pairing_id=created["pairing_id"], operator_account_id=s.operator)
    assert (await pairing.list_for_account(operator_account_id=s.operator))["nodes"] == []
    polled = await pairing.poll(account_id=s.aid, wallet=s.wallet)
    assert polled["payload"] == approved["payload"]
    assert polled["payload"]["permissions"] == ["validator.account_visibility"]
    params = dict(pairing_id=created["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=_sign(s.signer, polled["payload"]))
    first = await pairing.confirm(**params)
    assert first["status"] == "linked"
    assert await pairing.confirm(**params) == first
    mine = (await pairing.list_for_account(operator_account_id=s.operator))["nodes"]
    assert len(mine) == 1 and mine[0]["validator_id"] == s.node
    assert "signature" not in mine[0] and "node_account_id" not in mine[0]
    assert (await pairing.list_for_account(operator_account_id=s.attacker))["nodes"] == []
    assert before == await _snapshot(s)


@pytest.mark.asyncio
async def test_account_list_timestamps_are_timezone_aware(state):
    s = state
    await _confirmed(s)
    async with s.factory() as session:
        await session.execute(sa.update(db.validators).where(db.validators.c.id == s.node).values(last_heartbeat=NOW))
        await session.commit()
    row = (await pairing.list_for_account(operator_account_id=s.operator))["nodes"][0]
    assert row["linked_at"] == NOW
    assert row["last_heartbeat"] == NOW


@pytest.mark.asyncio
async def test_approval_is_immutable_and_never_creates_association_alone(state):
    s = state
    approved = await _approved(s)
    for action in [pairing.approve, pairing.inspect]:
        with pytest.raises(pairing.PairingConflict, match="Another account"):
            await action(pairing_id=approved["pairing_id"], operator_account_id=s.attacker)
    assert (await pairing.list_for_account(operator_account_id=s.operator))["nodes"] == []
    with pytest.raises(pairing.PairingNotFound):
        await pairing.confirm(
            pairing_id=approved["pairing_id"],
            account_id=s.other_aid,
            wallet=s.other.address.lower(),
            signature=_sign(s.other, approved["payload"]),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("purpose", "other-purpose"),
        ("audience", "https://evil.example.test"),
        ("operator_account_id", "00000000-0000-0000-0000-000000000001"),
        ("node_account_id", "00000000-0000-0000-0000-000000000001"),
        ("validator_id", "val_other"),
        ("pairing_id", "vpa_" + "0" * 64),
        ("permissions", ["account.manage"]),
        ("expires_at", 9999999999),
        ("comparison_code", "00000000"),
        ("signing_wallet", "0x" + "0" * 40),
    ],
)
async def test_signature_binds_every_field(state, field, value):
    s = state
    approved = await _approved(s)
    signature = _sign(s.signer, {**approved["payload"], field: value})
    with pytest.raises(pairing.PairingForbidden, match="signature"):
        await pairing.confirm(pairing_id=approved["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=signature)
    assert (await pairing.poll(account_id=s.aid, wallet=s.wallet))["status"] == "approved"


@pytest.mark.asyncio
async def test_wrong_signer_and_malformed_signature_fail_closed(state):
    s = state
    approved = await _approved(s)
    for signature in ["bad", "0" * 130, _sign(s.other, approved["payload"])]:
        with pytest.raises(pairing.PairingForbidden):
            await pairing.confirm(pairing_id=approved["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=signature)


@pytest.mark.asyncio
async def test_cancel_expiry_and_fresh_attempt_prevent_replay(state):
    s = state
    old = await _approved(s)
    params = dict(pairing_id=old["pairing_id"], account_id=s.aid, wallet=s.wallet)
    assert (await pairing.cancel(**params))["status"] == "cancelled"
    with pytest.raises(pairing.PairingConflict):
        await pairing.confirm(**params, signature=_sign(s.signer, old["payload"]))
    fresh = await _approved(s)
    assert fresh["pairing_id"] != old["pairing_id"]
    with pytest.raises(pairing.PairingNotFound):
        await pairing.confirm(**params, signature=_sign(s.signer, old["payload"]))
    async with s.factory() as session:
        await session.execute(sa.update(db.validator_pairings).values(created=NOW - timedelta(hours=1), expires_at=NOW))
        await session.commit()
    assert (await pairing.poll(account_id=s.aid, wallet=s.wallet))["status"] == "expired"
    for action in [pairing.approve, pairing.inspect]:
        with pytest.raises(pairing.PairingConflict, match="expired"):
            await action(pairing_id=fresh["pairing_id"], operator_account_id=s.operator)
    with pytest.raises(pairing.PairingConflict, match="expired"):
        await pairing.confirm(
            pairing_id=fresh["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=_sign(s.signer, fresh["payload"]),
        )
    async with s.factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(db.validator_pairings)) == 1


@pytest.mark.asyncio
async def test_unlink_is_owner_bound_and_old_requests_cannot_remove_a_new_link(state):
    s = state
    old = await _confirmed(s)
    with pytest.raises(pairing.PairingConflict, match="already associated"):
        await pairing.create(account_id=s.aid, wallet=s.wallet)
    with pytest.raises(pairing.PairingNotFound):
        await pairing.unlink(validator_id=s.node, pairing_id=old["pairing_id"], operator_account_id=s.attacker)
    params = dict(validator_id=s.node, pairing_id=old["pairing_id"], operator_account_id=s.operator)
    assert (await pairing.unlink(**params))["status"] == "unlinked"
    assert (await pairing.unlink(**params))["status"] == "unlinked"
    assert (await pairing.list_for_account(operator_account_id=s.operator))["nodes"] == []
    await _confirmed(s)
    with pytest.raises(pairing.PairingNotFound):
        await pairing.unlink(**params)


@pytest.mark.asyncio
async def test_revocation_signer_drift_and_account_retirement_fail_closed(state):
    s = state
    approved = await _approved(s)
    async with s.factory() as session:
        await session.execute(sa.update(db.validators).where(db.validators.c.id == s.node).values(status="revoked"))
        await session.commit()
    with pytest.raises(pairing.PairingForbidden):
        await pairing.confirm(
            pairing_id=approved["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=_sign(s.signer, approved["payload"]),
        )
    replacement = Account.create().address.lower()
    async with s.factory() as session:
        await session.execute(
            sa.update(db.validators).where(db.validators.c.id == s.node).values(status="active", signing_wallet=replacement),
        )
        await session.commit()
    with pytest.raises(pairing.PairingConflict, match="identity changed"):
        await pairing.confirm(
            pairing_id=approved["pairing_id"], account_id=s.aid, wallet=replacement, signature=_sign(s.signer, approved["payload"]),
        )
    async with s.factory() as session:
        await session.execute(sa.update(db.validators).where(db.validators.c.id == s.node).values(signing_wallet=s.wallet))
        await session.execute(
            sa.insert(db.account_aliases).values(
                source_account_id=s.operator, canonical_account_id=s.attacker, merge_ref="fixture:retired",
            ),
        )
        await session.commit()
    with pytest.raises(pairing.PairingForbidden, match="Account changed"):
        await pairing.confirm(
            pairing_id=approved["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=_sign(s.signer, approved["payload"]),
        )


@pytest.mark.asyncio
async def test_pending_pairing_cannot_be_confirmed_or_link_to_its_node_account(state):
    s = state
    created = await pairing.create(account_id=s.aid, wallet=s.wallet)
    with pytest.raises(pairing.PairingConflict, match="separate personal account"):
        await pairing.approve(pairing_id=created["pairing_id"], operator_account_id=s.aid)
    with pytest.raises(pairing.PairingConflict, match="Approve in your account"):
        await pairing.confirm(pairing_id=created["pairing_id"], account_id=s.aid, wallet=s.wallet, signature="0" * 130)


@pytest.mark.asyncio
async def test_postgres_concurrent_approvals_have_one_account_and_confirm_is_idempotent(state):
    s = state
    if s.dialect != "postgresql":
        pytest.skip("requires real row locks")
    created = await pairing.create(account_id=s.aid, wallet=s.wallet)

    async def approve(aid):
        try:
            return await pairing.approve(pairing_id=created["pairing_id"], operator_account_id=aid)
        except pairing.PairingConflict:
            return None

    results = await asyncio.gather(*(approve(aid) for aid in [s.operator, s.attacker] * 10))
    winners = [r for r in results if r is not None]
    assert len(winners) == 10
    assert len({r["payload"]["operator_account_id"] for r in winners}) == 1
    signature = _sign(s.signer, winners[0]["payload"])
    confirmations = await asyncio.gather(
        *(pairing.confirm(pairing_id=created["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=signature) for _ in range(20)),
    )
    assert all(r["status"] == "linked" for r in confirmations)
    async with s.factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(db.validator_account_links)) == 1


@pytest.mark.asyncio
async def test_postgres_cancel_confirm_race_has_one_terminal_result(state):
    s = state
    if s.dialect != "postgresql":
        pytest.skip("requires real row locks")
    approved = await _approved(s)
    params = dict(pairing_id=approved["pairing_id"], account_id=s.aid, wallet=s.wallet)

    async def attempt(action, **kwargs):
        try:
            return (await action(**params, **kwargs))["status"]
        except pairing.PairingConflict:
            return "conflict"

    results = await asyncio.gather(attempt(pairing.confirm, signature=_sign(s.signer, approved["payload"])), attempt(pairing.cancel))
    assert results in [["linked", "conflict"], ["conflict", "cancelled"]]
    async with s.factory() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(db.validator_account_links))
    assert count == int(results[0] == "linked")


@pytest.mark.asyncio
async def test_postgres_expiry_is_checked_after_lock_wait(state, monkeypatch):
    s = state
    if s.dialect != "postgresql":
        pytest.skip("requires real row locks")
    monkeypatch.setattr(pairing, "_now", s.original_clock)
    approved = await _approved(s)
    signature = _sign(s.signer, approved["payload"])
    async with s.factory() as blocker:
        await blocker.execute(sa.select(db.validators).where(db.validators.c.id == s.node).with_for_update())
        await blocker.execute(
            sa.update(db.validator_pairings)
            .where(db.validator_pairings.c.validator_id == s.node)
            .values(
                expires_at=datetime.now(UTC) + timedelta(milliseconds=150),
            ),
        )
        task = asyncio.create_task(
            pairing.confirm(pairing_id=approved["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=signature),
        )
        await asyncio.sleep(0.3)
        await blocker.commit()
    with pytest.raises(pairing.PairingConflict, match="expired"):
        await task


@pytest.mark.asyncio
async def test_node_can_remove_association_without_access_to_human_account(state):
    s = state
    approved = await _confirmed(s)
    current = await pairing.node_link(account_id=s.aid, wallet=s.wallet)
    payload = current["unlink_payload"]
    params = dict(account_id=s.aid, wallet=s.wallet, pairing_id=approved["pairing_id"], issued_at=payload["issued_at"])
    with pytest.raises(pairing.PairingForbidden):
        await pairing.unlink_from_node(**params, signature=_sign(s.other, payload))
    with pytest.raises(pairing.PairingConflict, match="expired"):
        await pairing.unlink_from_node(**{**params, "issued_at": payload["issued_at"] - 601}, signature=_sign(s.signer, payload))
    signed = _sign(s.signer, payload)
    assert (await pairing.unlink_from_node(**params, signature=signed))["status"] == "unlinked"
    assert (await pairing.unlink_from_node(**params, signature=signed))["status"] == "unlinked"
    assert (await pairing.node_link(account_id=s.aid, wallet=s.wallet))["status"] == "none"
    await _confirmed(s)
    with pytest.raises(pairing.PairingNotFound):
        await pairing.unlink_from_node(**params, signature=signed)


@pytest.mark.asyncio
async def test_link_insert_failure_rolls_back_terminal_state(state):
    s = state
    approved = await _approved(s)

    def refuse_link(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("INSERT INTO grid_validator_account_links"):
            raise RuntimeError("simulated database failure")

    sa.event.listen(s.engine.sync_engine, "before_cursor_execute", refuse_link)
    try:
        with pytest.raises(RuntimeError, match="simulated database failure"):
            await pairing.confirm(
                pairing_id=approved["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=_sign(s.signer, approved["payload"]),
            )
    finally:
        sa.event.remove(s.engine.sync_engine, "before_cursor_execute", refuse_link)
    assert (await pairing.poll(account_id=s.aid, wallet=s.wallet))["status"] == "approved"
    assert (await pairing.list_for_account(operator_account_id=s.operator))["nodes"] == []
    assert (
        await pairing.confirm(
            pairing_id=approved["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=_sign(s.signer, approved["payload"]),
        )
    )["status"] == "linked"


@pytest.mark.asyncio
async def test_signer_rotation_and_account_merge_do_not_transfer_association(state):
    s = state
    await _confirmed(s)
    async with s.factory() as session:
        await session.execute(
            sa.insert(db.account_aliases).values(
                source_account_id=s.operator, canonical_account_id=s.attacker, merge_ref="fixture:retire-after-pair",
            ),
        )
        await session.commit()
    assert (await pairing.list_for_account(operator_account_id=s.attacker))["nodes"] == []
    assert (await pairing.node_link(account_id=s.aid, wallet=s.wallet))["status"] == "none"
    # An explicit new two-party flow is needed, never following account aliases.
    fresh = await pairing.create(account_id=s.aid, wallet=s.wallet)
    approved = await pairing.approve(pairing_id=fresh["pairing_id"], operator_account_id=s.attacker)
    await pairing.confirm(pairing_id=fresh["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=_sign(s.signer, approved["payload"]))
    replacement = Account.create().address.lower()
    async with s.factory() as session:
        await session.execute(sa.update(db.validators).where(db.validators.c.id == s.node).values(signing_wallet=replacement))
        await session.commit()
    assert (await pairing.list_for_account(operator_account_id=s.attacker))["nodes"] == []
    assert (await pairing.node_link(account_id=s.aid, wallet=replacement))["status"] == "none"


@pytest.mark.asyncio
async def test_http_flow_uses_real_scoped_keys_and_fresh_user_tokens(state, monkeypatch):
    s = state
    monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", secrets.token_hex(32))
    monkeypatch.setenv("GRID_SALT", "pairing-test-only")
    monkeypatch.setattr(auth, "_API_KEY_SALT", None)
    monkeypatch.setattr(database, "_session_factory", s.factory)
    monkeypatch.setattr(limiter, "enabled", False)
    s.settings.validator_pairing_enabled = True
    monkeypatch.setattr(pairing_router, "get_settings", lambda: s.settings)
    node_key, ordinary_key, other_key = (accounts_svc.generate_api_key() for _ in range(3))
    async with s.factory() as session:
        for key, aid, scopes in [
            (node_key, s.aid, accounts_svc.VALIDATOR_SCOPES),
            (ordinary_key, s.operator, accounts_svc.INFERENCE_SCOPES),
            (other_key, s.other_aid, accounts_svc.VALIDATOR_SCOPES),
        ]:
            await session.execute(
                sa.insert(db.api_keys).values(hash=auth.hash_api_key(key), account_id=aid, scopes=scopes, is_session=False, revoked=False),
            )
        await session.commit()
    user_token = user_tokens.issue(s.operator, audience="grid-console", scopes=accounts_svc.SESSION_SCOPES, auth_method="google")
    stale_token = user_tokens.issue(
        s.operator, audience="grid-console", scopes=accounts_svc.SESSION_SCOPES, auth_method="google", now=int(time.time()) - 601,
    )
    unproved = user_tokens.issue(s.operator, audience="grid-console", scopes=accounts_svc.SESSION_SCOPES, auth_method="app")
    app = FastAPI()
    app.include_router(pairing_router.router)
    def headers(key):
        return {"Authorization": f"Bearer {key}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://core.test") as client:
        start = "/v1/validator/account-pairings"
        assert (await client.post(start, headers=headers(ordinary_key))).status_code == 403
        assert (await client.post(start)).status_code == 401
        created = await client.post(start, headers=headers(node_key))
        assert created.status_code == 200 and created.headers["cache-control"] == "no-store"
        pid = created.json()["pairing_id"]
        account_path = f"/v1/account/validator-pairings/{pid}"
        assert (await client.get(account_path)).status_code == 401
        for credential in [node_key, ordinary_key, stale_token, unproved]:
            assert (await client.post(account_path + "/approve", headers=headers(credential))).status_code == 403
        approved = await client.post(account_path + "/approve", headers=headers(user_token))
        assert approved.status_code == 200
        payload = approved.json()["payload"]
        confirm_path = f"{start}/{pid}/confirm"
        signature = _sign(s.signer, payload)
        assert (await client.post(confirm_path, headers=headers(other_key), json={"signature": signature})).status_code == 404
        assert (
            await client.post(
                confirm_path, headers=headers(node_key), json={"signature": signature, "operator_account_id": str(s.attacker)},
            )
        ).status_code == 422
        assert (await client.post(confirm_path, headers=headers(node_key), json={"signature": signature})).json()["status"] == "linked"
        mine = await client.get("/v1/account/validators", headers=headers(user_token))
        assert mine.status_code == 200 and len(mine.json()["nodes"]) == 1
        assert (await client.get("/v1/account/validators", headers=headers(node_key))).status_code == 403
        s.settings.validator_pairing_enabled = False
        assert (await client.post(start, headers=headers(node_key))).status_code == 503


@pytest.mark.asyncio
async def test_postgres_concurrent_create_reuses_one_attempt(state):
    s = state
    if s.dialect != "postgresql":
        pytest.skip("requires real row locks")
    results = await asyncio.gather(*(pairing.create(account_id=s.aid, wallet=s.wallet) for _ in range(20)))
    assert len({r["pairing_id"] for r in results}) == 1
    async with s.factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(db.validator_pairings)) == 1


@pytest.mark.asyncio
async def test_configuration_drift_invalidates_attempt_and_allows_explicit_restart(state):
    s = state
    old = await _approved(s)
    s.settings.validator_pairing_audience = "https://replacement.example.test"
    with pytest.raises(pairing.PairingConflict, match="audience changed"):
        await pairing.confirm(pairing_id=old["pairing_id"], account_id=s.aid, wallet=s.wallet, signature=_sign(s.signer, old["payload"]))
    fresh = await pairing.create(account_id=s.aid, wallet=s.wallet)
    assert fresh["pairing_id"] != old["pairing_id"]
    for url in ["http://console.example.test", "https://user:password@console.example.test", "https://console.example.test/?next=evil"]:
        s.settings.validator_pairing_console_url = url
        with pytest.raises(pairing.PairingError, match="HTTPS URLs"):
            await pairing.create(account_id=s.aid, wallet=s.wallet)


@pytest.mark.asyncio
async def test_router_database_failure_does_not_expose_driver_parameters():
    async def failed():
        raise SQLAlchemyError("sensitive SQL parameters must not reach HTTP")

    with pytest.raises(HTTPException) as caught:
        await pairing_router._call(failed)
    assert caught.value.status_code == 503
    assert "sensitive" not in caught.value.detail

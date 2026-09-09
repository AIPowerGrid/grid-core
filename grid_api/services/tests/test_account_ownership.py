# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ownership handoff is read-only, authenticated and based on recorded merges."""

import os
import secrets
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from grid_api import database
from grid_api.routers import accounts as router
from grid_api.services import accounts, identities, user_tokens
from grid_api.v2.schema import account_aliases, accounts as accounts_t, credit_ledger, metadata


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def db(request, monkeypatch):
    pg = os.getenv("VALIDATORS_TEST_DB_URL", "")
    if request.param == "postgres" and not pg.startswith("postgresql"):
        pytest.skip("set VALIDATORS_TEST_DB_URL to a disposable PostgreSQL database")
    namespace = "ownership_test_" + uuid4().hex
    engine = create_async_engine(
        pg if request.param == "postgres" else "sqlite+aiosqlite:///:memory:",
        **({"execution_options": {"schema_translate_map": {None: namespace}}}
           if request.param == "postgres" else {}),
    )
    try:
        async with engine.begin() as conn:
            if request.param == "postgres":
                await conn.execute(sa.schema.CreateSchema(namespace))
            await conn.run_sync(metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(database, "_session_factory", factory)
        monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", secrets.token_hex(32))
        yield factory
    finally:
        if request.param == "postgres":
            async with engine.begin() as conn:
                await conn.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


async def seed(db, count=3):
    ids = [uuid4() for _ in range(count)]
    async with db() as session:
        await session.execute(sa.insert(accounts_t), [{"id": aid, "flags": {}} for aid in ids])
        await session.commit()
    return ids


@pytest.mark.asyncio
async def test_actual_merges_expose_only_proved_aliases_without_new_money_rows(db):
    first, second, canonical, stranger = await seed(db, 4)
    await identities.merge_accounts(second, first, merge_ref="ownership-first")
    await identities.merge_accounts(canonical, second, merge_ref="ownership-second")
    async with db() as session:
        before = (await session.execute(sa.select(credit_ledger))).all()
    expected = {"account_id": str(canonical), "account_aliases": sorted(map(str, [first, second]))}
    for aid in [first, second, canonical]:
        assert await identities.account_ownership(aid) == expected
    assert await identities.account_ownership(stranger) == {"account_id": str(stranger), "account_aliases": []}
    async with db() as session:
        assert (await session.execute(sa.select(credit_ledger))).all() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [128, 129])
async def test_family_size_limit_rejects_instead_of_truncating(db, size):
    canonical, *aliases = await seed(db, size)
    async with db() as session:
        await session.execute(sa.insert(account_aliases), [
            {"source_account_id": aid, "canonical_account_id": canonical,
             "merge_ref": f"bound:{aid}"} for aid in aliases])
        await session.commit()
    if size == 129:
        with pytest.raises(RuntimeError, match="member limit"):
            await identities.account_ownership(canonical)
    else:
        assert len((await identities.account_ownership(canonical))["account_aliases"]) == 127


@pytest.mark.asyncio
async def test_root_change_during_read_is_not_a_partial_handoff(db, monkeypatch):
    canonical, replacement, _ = await seed(db)
    calls = 0

    async def resolve(_aid, *, session=None):
        nonlocal calls
        calls += 1
        return canonical if calls < 3 else replacement

    monkeypatch.setattr(identities, "canonical_account_id", resolve)
    with pytest.raises(RuntimeError, match="changed during read"):
        await identities.account_ownership(canonical)


@pytest.mark.asyncio
async def test_missing_account_is_not_an_ownership_proof(db):
    with pytest.raises(RuntimeError, match="missing"):
        await identities.account_ownership(uuid4())


@pytest.mark.asyncio
async def test_cycle_fails_closed(db):
    first, second, _ = await seed(db)
    async with db() as session:
        await session.execute(sa.insert(account_aliases), [
            {"source_account_id": first, "canonical_account_id": second, "merge_ref": "cycle-one"},
            {"source_account_id": second, "canonical_account_id": first, "merge_ref": "cycle-two"}])
        await session.commit()
    with pytest.raises(RuntimeError, match="cycle"):
        await identities.account_ownership(first)


@pytest.mark.asyncio
async def test_http_signed_delegation_scope_isolation_and_no_store(db, monkeypatch):
    old, canonical, stranger = await seed(db)
    await identities.merge_accounts(canonical, old, merge_ref="http-ownership")

    async def resolve(key):
        if key in {"service", "limited"}:
            return {"source": "v2", "key_kind": "service", "service_id": "gallery",
                    "account_id": stranger, "scopes": ["account.read"] if key == "service" else []}
        return None

    monkeypatch.setattr(accounts, "resolve_api_key", resolve)
    monkeypatch.setattr(router.limiter, "enabled", False)
    app = FastAPI()
    app.include_router(router.router)

    def token(aid=old, scope="account.read", service="gallery"):
        return user_tokens.issue(aid, audience=service, service_id=service,
                                 scopes=[scope], auth_method="google")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for headers, status in [
            ({}, 401), ({"apikey": "service"}, 401),
            ({"apikey": "service", "X-Grid-User-Token": "invalid"}, 401),
            ({"apikey": "service", "X-Grid-User-Token": token(service="foreign")}, 401),
            ({"apikey": "limited", "X-Grid-User-Token": token()}, 403),
            ({"apikey": "service", "X-Grid-User-Token": token(scope="inference.submit")}, 403),
        ]:
            response = await client.get("/v1/account/ownership", headers=headers)
            assert response.status_code == status, response.text
            assert response.headers["cache-control"] == "no-store"
            assert str(old) not in response.text
        response = await client.get("/v1/account/ownership", headers={
            "apikey": "service", "X-Grid-User-Token": token()})
        assert response.status_code == 200 and response.json() == {
            "account_id": str(canonical), "account_aliases": [str(old)]}
        assert response.headers["cache-control"] == "no-store"
        # A target parameter is not an authority: only the signed subject wins.
        response = await client.get(f"/v1/account/ownership?account_id={canonical}", headers={
            "apikey": "service", "X-Grid-User-Token": token(stranger)})
        assert response.json() == {"account_id": str(stranger), "account_aliases": []}

        async def broken(_):
            raise RuntimeError("sensitive database detail")
        monkeypatch.setattr(identities, "account_ownership", broken)
        response = await client.get("/v1/account/ownership", headers={
            "apikey": "service", "X-Grid-User-Token": token()})
        assert response.status_code == 503
        assert "sensitive" not in response.text
        assert response.headers["cache-control"] == "no-store"

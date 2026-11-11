# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-PostgreSQL concurrency proofs for refresh-token rotation and revocation.

SQLite serializes writers, so it cannot show the READ COMMITTED hazards these
tests pin: a revocation that waits on a rotation must still end the successor
that rotation inserted. Skipped unless CREDITS_TEST_DB_URL points at Postgres.
"""

import asyncio
import base64
import hashlib
import os
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from grid_api import database
from grid_api.services import accounts, identities, oauth_server
from grid_api.v2.schema import metadata, oauth_refresh_tokens

_PG = os.environ.get("CREDITS_TEST_DB_URL", "")
RESOURCE = "https://api.example.test"

pytestmark = pytest.mark.skipif(
    not _PG.startswith("postgresql"),
    reason="set CREDITS_TEST_DB_URL=postgresql+asyncpg://… for the real-PG refresh-token tests",
)


@pytest_asyncio.fixture
async def pg(monkeypatch):
    engine = create_async_engine(_PG, pool_size=10)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setenv("GRID_MCP_OAUTH_ENABLED", "1")
    monkeypatch.setenv("GRID_OAUTH_REFRESH_TOKENS_ENABLED", "1")
    monkeypatch.setenv("GRID_OAUTH_ISSUER", RESOURCE)
    monkeypatch.setenv("GRID_OAUTH_RESOURCE", RESOURCE)
    monkeypatch.setenv("GRID_OAUTH_CONSOLE_AUTHORIZE_URL", "https://console.example.test/oauth/authorize")
    monkeypatch.setenv("GRID_USER_TOKEN_SIGNING_KEY", "unit-test-" * 4)
    try:
        yield engine
    finally:
        database._session_factory = old
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


def _query(url: str, name: str) -> str:
    return parse_qs(urlsplit(url).query)[name][0]


async def _account() -> str:
    account, _ = await accounts.create_account(username="pg user", issue_initial_key=False, grant_verified_welcome=False)
    return account["id"]


async def _authorize(account_id) -> tuple[str, dict]:
    client = await oauth_server.register_client(
        {"client_name": "Desktop", "application_type": "native", "redirect_uris": ["http://127.0.0.1/callback"],
         "grant_types": ["authorization_code", "refresh_token"]},
    )
    redirect, verifier = "http://127.0.0.1:50000/callback", "r" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    consent = await oauth_server.create_authorization_request(
        client_id=client["client_id"], redirect_uri=redirect, response_type="code",
        scope="inference.submit account.read offline_access", state="s", code_challenge=challenge,
        code_challenge_method="S256", requested_resource=RESOURCE,
    )
    callback = await oauth_server.decide_authorization_request(
        _query(consent, "request"), approve=True, account_id=account_id, auth_method="google",
    )
    tokens = await oauth_server.exchange_authorization_code(
        grant_type="authorization_code", code=_query(callback, "code"), client_id=client["client_id"],
        redirect_uri=redirect, code_verifier=verifier, requested_resource=RESOURCE,
    )
    return client["client_id"], tokens


async def _refresh(token: str, client_id: str):
    try:
        return await oauth_server.refresh_access_token(refresh_token=token, client_id=client_id)
    except oauth_server.OAuthProtocolError as exc:
        return exc.error


async def _rows():
    async with await database.new_session() as session:
        return (await session.execute(sa.select(oauth_refresh_tokens))).mappings().all()


async def _live_tokens() -> int:
    return sum(row["used_at"] is None and row["revoked_at"] is None for row in await _rows())


class _Paused:
    """Hold one rotation open after it inserted its successor."""

    def __init__(self, monkeypatch):
        self.inserted, self.release = asyncio.Event(), asyncio.Event()
        real = oauth_server._insert_refresh_token
        first = True

        async def paused(session, **kwargs):
            nonlocal first
            token = await real(session, **kwargs)
            if first:
                first = False
                await session.flush()
                self.inserted.set()
                await self.release.wait()
            return token

        monkeypatch.setattr(oauth_server, "_insert_refresh_token", paused)


async def _revocation_racing_a_rotation(monkeypatch, revoker):
    account = await _account()
    client_id, first = await _authorize(account)
    family = (await _rows())[0]["family_id"]
    paused = _Paused(monkeypatch)
    rotation = asyncio.create_task(_refresh(first["refresh_token"], client_id))
    await asyncio.wait_for(paused.inserted.wait(), 10)
    revocation = asyncio.create_task(revoker(account, family, first, client_id))
    await asyncio.sleep(0.4)
    assert not revocation.done()  # it waits for the rotation rather than passing it
    paused.release.set()
    rotated = await asyncio.wait_for(rotation, 10)
    await asyncio.wait_for(revocation, 10)
    assert isinstance(rotated, dict)  # the rotation itself was legitimate and finished first
    # Checked before the successor is ever presented: the revocation itself
    # must have reached it, not a later use-time check.
    assert await _live_tokens() == 0
    assert await oauth_server.list_sessions(account) == []
    assert await _refresh(rotated["refresh_token"], client_id) == "invalid_grant"


@pytest.mark.asyncio
async def test_owner_revocation_ends_a_successor_inserted_by_an_in_flight_rotation(pg, monkeypatch):
    async def revoker(account, family, first, client_id):
        assert await oauth_server.revoke_session(account, family) is True

    await _revocation_racing_a_rotation(monkeypatch, revoker)


@pytest.mark.asyncio
async def test_client_revocation_ends_a_successor_inserted_by_an_in_flight_rotation(pg, monkeypatch):
    async def revoker(account, family, first, client_id):
        await oauth_server.revoke_refresh_token(token=first["refresh_token"], client_id=client_id)

    await _revocation_racing_a_rotation(monkeypatch, revoker)


@pytest.mark.asyncio
async def test_replay_revocation_ends_a_successor_inserted_by_an_in_flight_rotation(pg, monkeypatch):
    account = await _account()
    client_id, first = await _authorize(account)
    second = await oauth_server.refresh_access_token(refresh_token=first["refresh_token"], client_id=client_id)
    paused = _Paused(monkeypatch)
    rotation = asyncio.create_task(_refresh(second["refresh_token"], client_id))
    await asyncio.wait_for(paused.inserted.wait(), 10)
    replay = asyncio.create_task(_refresh(first["refresh_token"], client_id))
    await asyncio.sleep(0.4)
    assert not replay.done()
    paused.release.set()
    third = await asyncio.wait_for(rotation, 10)
    assert await asyncio.wait_for(replay, 10) == "invalid_grant"
    assert isinstance(third, dict)
    assert await _live_tokens() == 0
    assert await _refresh(third["refresh_token"], client_id) == "invalid_grant"
    assert {row["revoke_reason"] for row in await _rows()} == {"reuse"}


@pytest.mark.asyncio
async def test_one_token_presented_concurrently_has_exactly_one_winner(pg):
    account = await _account()
    client_id, first = await _authorize(account)
    results = await asyncio.gather(*[_refresh(first["refresh_token"], client_id) for _ in range(6)])
    assert sum(isinstance(result, dict) for result in results) == 1
    assert [result for result in results if not isinstance(result, dict)] == ["invalid_grant"] * 5
    # The duplicates count as a replay, so the session ends: clients must serialize refreshes.
    assert await _live_tokens() == 0


@pytest.mark.asyncio
async def test_merge_racing_a_rotation_neither_deadlocks_nor_leaves_a_session(pg, monkeypatch):
    survivor, retired = await _account(), await _account()
    client_id, first = await _authorize(retired)
    paused = _Paused(monkeypatch)
    rotation = asyncio.create_task(_refresh(first["refresh_token"], client_id))
    await asyncio.wait_for(paused.inserted.wait(), 10)
    merge = asyncio.create_task(identities.merge_accounts(survivor, retired, merge_ref="pg-merge-race"))
    await asyncio.sleep(0.4)
    assert not merge.done()  # the merge waits for the rotation's hold on the account
    paused.release.set()
    rotated = await asyncio.wait_for(rotation, 10)
    await asyncio.wait_for(merge, 10)
    assert isinstance(rotated, dict)
    assert await _live_tokens() == 0
    assert {row["revoke_reason"] for row in await _rows()} == {"merge"}
    assert await _refresh(rotated["refresh_token"], client_id) == "invalid_grant"


@pytest.mark.asyncio
async def test_rotation_started_during_a_merge_waits_and_is_refused(pg, monkeypatch):
    survivor, retired = await _account(), await _account()
    client_id, first = await _authorize(retired)
    entered, release = asyncio.Event(), asyncio.Event()
    real = oauth_server.revoke_account_sessions

    async def held(session, account_id, *, now):
        entered.set()
        await release.wait()
        return await real(session, account_id, now=now)

    monkeypatch.setattr(oauth_server, "revoke_account_sessions", held)
    merge = asyncio.create_task(identities.merge_accounts(survivor, retired, merge_ref="pg-merge-first"))
    await asyncio.wait_for(entered.wait(), 10)
    rotation = asyncio.create_task(_refresh(first["refresh_token"], client_id))
    await asyncio.sleep(0.4)
    assert not rotation.done()
    release.set()
    await asyncio.wait_for(merge, 10)
    assert await asyncio.wait_for(rotation, 10) == "invalid_grant"
    assert await _live_tokens() == 0

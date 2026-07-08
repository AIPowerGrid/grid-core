# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Identity-bridge P0 regression: /v1/accounts/session must resolve on exactly
ONE authoritative identity, so an unverified OAuth-asserted email can't join
into a *different* account (confused-deputy / account takeover)."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import sqlalchemy as sa

from grid_api import database
from grid_api.services import accounts as accounts_svc
from grid_api.routers.accounts import SessionForm, _session_match
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata as v2_metadata


# ── the pure match-key selector (the security-relevant logic) ────────────────

def _f(**kw):
    return SessionForm(**kw)


def test_oauth_email_supplement_never_matches_on_email():
    # THE takeover shape: OAuth login asserting a victim's email → resolve on
    # oauth_sub, the email is ignored as a match key.
    assert _session_match(_f(oauth_sub="osA", email="victim@x.com")) == ("oauth_sub", "osA")


def test_wallet_beats_email_and_lowercases():
    assert _session_match(_f(wallet="0xABC", email="v@x.com")) == ("wallet", "0xabc")


def test_oauth_beats_wallet():
    assert _session_match(_f(oauth_sub="osA", wallet="0xabc")) == ("oauth_sub", "osA")


def test_verified_sole_email_is_authoritative():
    assert _session_match(_f(email="me@x.com", email_verified=True)) == ("email", "me@x.com")


def test_unverified_sole_email_is_refused():
    assert _session_match(_f(email="me@x.com")) is None
    assert _session_match(_f(email="me@x.com", email_verified=False)) is None


def test_nothing_is_refused():
    assert _session_match(_f()) is None


# ── the DB takeover scenario end-to-end on the resolver query ────────────────

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(v2_metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield
    finally:
        database._session_factory = old
        await engine.dispose()


async def _resolve(form: SessionForm):
    """Mirror the router's resolver: single authoritative match key → row."""
    match = _session_match(form)
    if match is None:
        return None
    field, val = match
    col = getattr(accounts_t.c, field)
    async with await database.new_session() as s:
        return (await s.execute(sa.select(accounts_t.c.id).where(col == val))).first()


@pytest.mark.asyncio
async def test_oauth_login_with_victims_email_resolves_to_own_account(db):
    victim, _ = await accounts_svc.create_account(email="victim@x.com")           # account B (email)
    attacker, _ = await accounts_svc.create_account(oauth_sub="github_attacker")  # account A (oauth)

    # Attacker logs in via OAuth, asserting the VICTIM's email as a supplement.
    row = await _resolve(SessionForm(oauth_sub="github_attacker", email="victim@x.com"))
    assert row is not None
    assert str(row[0]) == str(attacker["id"])   # resolves to the attacker's OWN account…
    assert str(row[0]) != str(victim["id"])     # …never the victim's. Takeover closed.


@pytest.mark.asyncio
async def test_new_oauth_user_with_colliding_email_does_not_hijack_or_crash(db):
    victim, _ = await accounts_svc.create_account(email="taken@x.com")
    # A brand-new oauth_sub not yet in the DB → resolver returns None → router
    # creates a NEW account, dropping the colliding email instead of merging.
    row = await _resolve(SessionForm(oauth_sub="github_new", email="taken@x.com"))
    assert row is None  # no existing account matched → create path (email dropped if taken)
    # victim's account is untouched and still owns the email
    async with await database.new_session() as s:
        owner = (await s.execute(
            sa.select(accounts_t.c.id).where(accounts_t.c.email == "taken@x.com"))).first()
    assert str(owner[0]) == str(victim["id"])

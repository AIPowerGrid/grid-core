# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import promotions, wallet_trial
from grid_api.v2.schema import accounts, metadata, promo_campaigns, promo_grants, wallet_trial_claims

CAMPAIGN = "wallet-trial-test"


@pytest_asyncio.fixture
async def trial_db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(promotions, "PROMO_ENABLED", True)
    monkeypatch.setattr(promotions, "PROMO_SPENDABLE_LIVE", True)
    monkeypatch.setattr(promotions, "PROMO_SPENDABLE_CAMPAIGNS", frozenset({CAMPAIGN}))
    monkeypatch.setattr(wallet_trial, "ENABLED", True)
    monkeypatch.setattr(wallet_trial, "CAMPAIGN_ID", CAMPAIGN)
    monkeypatch.setattr(wallet_trial, "GRANT_MICRO", 20_000)
    monkeypatch.setattr(wallet_trial, "BUDGET_MICRO", 100_000)
    monkeypatch.setattr(wallet_trial, "HOURLY_MICRO", 40_000)
    monkeypatch.setattr(wallet_trial, "EXPIRES_DAYS", 3)
    monkeypatch.setattr(wallet_trial, "_campaign_ready", False)
    monkeypatch.setattr(wallet_trial, "_get_api_key_salt", lambda: "test-salt-" + "x" * 90)

    async def new_account():
        aid = uuid4()
        async with database._session_factory() as session:
            await session.execute(sa.insert(accounts).values(id=aid, flags={}, username=f"w-{aid.hex[:6]}"))
            await session.commit()
        return aid

    try:
        yield new_account
    finally:
        database._session_factory = old
        await engine.dispose()


async def _count(table):
    async with await database.new_session() as session:
        return await session.scalar(sa.select(sa.func.count()).select_from(table))


async def _campaign_granted():
    async with await database.new_session() as session:
        return await session.scalar(
            sa.select(promo_campaigns.c.granted_micro).where(promo_campaigns.c.id == CAMPAIGN),
        )


@pytest.mark.asyncio
async def test_first_sign_in_gets_the_credit_once(trial_db):
    account = await trial_db()
    first = await wallet_trial.grant(account, "8.8.4.7")
    again = await wallet_trial.grant(account, "1.1.1.1")
    assert first["status"] == "granted" and first["granted_micro"] == 20_000
    assert again["status"] == "already"
    assert await _count(promo_grants) == 1
    assert await _count(wallet_trial_claims) == 1
    assert await _campaign_granted() == 20_000
    assert await promotions.available_micro(account, spendable_only=True) == 20_000


@pytest.mark.asyncio
async def test_second_wallet_from_the_same_address_gets_nothing(trial_db):
    await wallet_trial.grant(await trial_db(), "8.8.4.7")
    second = await trial_db()
    refused = await wallet_trial.grant(second, "8.8.4.7")
    assert refused == {"status": "address_used", "granted_micro": 0}
    assert await _count(promo_grants) == 1
    assert await _campaign_granted() == 20_000
    assert await promotions.available_micro(second) == 0


@pytest.mark.asyncio
async def test_ipv6_addresses_in_one_block_count_as_one(trial_db):
    await wallet_trial.grant(await trial_db(), "2001:4860:4860:1::8888")
    refused = await wallet_trial.grant(await trial_db(), "2001:4860:4860:1:ffff::1")
    other_block = await wallet_trial.grant(await trial_db(), "2001:4860:4861::8888")
    assert refused["status"] == "address_used"
    assert other_block["status"] == "granted"


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "::1", "", None, "not-an-ip"])
async def test_hidden_or_unusable_address_gets_nothing(trial_db, address):
    result = await wallet_trial.grant(await trial_db(), address)
    assert result == {"status": "address_unknown", "granted_micro": 0}
    assert await _count(promo_grants) == 0


@pytest.mark.asyncio
async def test_hourly_release_stops_the_third_grant_and_leaves_its_address_free(trial_db):
    for last in (1, 2):
        assert (await wallet_trial.grant(await trial_db(), f"8.8.4.{last}"))["status"] == "granted"
    late = await trial_db()
    assert (await wallet_trial.grant(late, "8.8.4.3"))["status"] == "hour_exhausted"
    assert await _count(wallet_trial_claims) == 2
    assert await _campaign_granted() == 40_000


@pytest.mark.asyncio
async def test_total_budget_is_never_passed(trial_db, monkeypatch):
    monkeypatch.setattr(wallet_trial, "HOURLY_MICRO", 10_000_000)
    statuses = [
        (await wallet_trial.grant(await trial_db(), f"8.8.4.{last}"))["status"] for last in range(1, 7)
    ]
    assert statuses == ["granted"] * 5 + ["budget_exhausted"]
    assert await _campaign_granted() == 100_000


@pytest.mark.asyncio
async def test_switched_off_grants_nothing(trial_db, monkeypatch):
    monkeypatch.setattr(wallet_trial, "ENABLED", False)
    result = await wallet_trial.grant(await trial_db(), "8.8.4.7")
    assert result == {"status": "disabled", "granted_micro": 0}
    assert await _count(promo_campaigns) == 0


@pytest.mark.asyncio
async def test_credit_pays_for_text_but_not_media(trial_db):
    from grid_api.services import credits

    account = await trial_db()
    await wallet_trial.grant(account, "8.8.4.7")
    assert await credits._promo_first(account, 5_000, "media-job", media=True) == 0
    assert await credits._promo_first(account, 5_000, "text-job") == 5_000
    assert await promotions.available_micro(account, spendable_only=True) == 15_000


def test_address_digest_is_keyed_and_not_the_address(monkeypatch):
    monkeypatch.setattr(wallet_trial, "_get_api_key_salt", lambda: "a" * 40)
    first = wallet_trial.address_hash("8.8.4.7")
    monkeypatch.setattr(wallet_trial, "_get_api_key_salt", lambda: "b" * 40)
    assert len(first) == 64 and "8.8.4" not in first
    assert wallet_trial.address_hash("8.8.4.7") != first
    assert wallet_trial.address_hash("::ffff:8.8.4.7") == wallet_trial.address_hash("8.8.4.7")


def test_public_result_hides_internal_reasons():
    assert wallet_trial.public({"status": "address_unknown", "granted_micro": 0}) == {"status": "unavailable"}
    shown = wallet_trial.public({"status": "granted", "granted_micro": 20_000, "remaining_micro": 20_000,
                                 "expires": "2026-11-20T00:00:00+00:00"})
    assert shown == {"status": "granted", "granted_usd": 0.02, "remaining_usd": 0.02,
                     "expires": "2026-11-20T00:00:00+00:00", "models": "text"}
    assert "retry" in wallet_trial.public({"status": "hour_exhausted", "granted_micro": 0})


class _Request:
    def __init__(self, peer, headers=None):
        self.client = type("Client", (), {"host": peer})()
        self.headers = headers or {}


def test_behind_the_cdn_the_caller_is_the_one_the_cdn_names():
    edge = "104.16.1.1"
    assert wallet_trial.caller_address(_Request(edge, {"cf-connecting-ip": "8.8.4.4"})) == "8.8.4.4"
    assert wallet_trial.caller_address(_Request(edge)) is None
    assert wallet_trial.caller_address(_Request(edge, {"cf-connecting-ip": "junk"})) is None


def test_a_direct_caller_cannot_name_another_address():
    assert wallet_trial.caller_address(_Request("8.8.4.4", {"cf-connecting-ip": "1.1.1.1"})) == "8.8.4.4"
    assert wallet_trial.address_hash(wallet_trial.caller_address(_Request(None))) is None


def test_a_salt_longer_than_the_hash_key_limit_still_works(monkeypatch):
    monkeypatch.setattr(wallet_trial, "_get_api_key_salt", lambda: "s" * 200)
    assert len(wallet_trial.address_hash("8.8.4.4")) == 64


def test_earlier_trial_campaigns_stay_text_only():
    listed = frozenset({"wallet-trial-v0", "builder-x", CAMPAIGN})
    assert wallet_trial.text_only_campaigns(listed) >= {"wallet-trial-v0", CAMPAIGN}
    assert "builder-x" not in wallet_trial.text_only_campaigns(listed)


def test_a_malformed_amount_switches_the_trial_off_instead_of_raising(monkeypatch):
    import importlib

    monkeypatch.setenv("GRID_WALLET_TRIAL_ENABLED", "1")
    monkeypatch.setenv("GRID_WALLET_TRIAL_GRANT_MICRO", "0.02")
    try:
        reloaded = importlib.reload(wallet_trial)
        assert reloaded.ENABLED is False and reloaded.GRANT_MICRO == 20_000
    finally:
        monkeypatch.delenv("GRID_WALLET_TRIAL_ENABLED")
        monkeypatch.delenv("GRID_WALLET_TRIAL_GRANT_MICRO")
        importlib.reload(wallet_trial)


@pytest.mark.asyncio
async def test_media_quotes_do_not_count_the_trial_credit(trial_db):
    account = await trial_db()
    await wallet_trial.grant(account, "8.8.4.7")
    spendable = await promotions.available_micro(account, spendable_only=True)
    for_media = await promotions.available_micro(
        account, spendable_only=True, exclude_campaigns=wallet_trial.text_only_campaigns(),
    )
    assert (spendable, for_media) == (20_000, 0)


@pytest.mark.asyncio
async def test_returning_wallet_is_answered_without_touching_the_address(trial_db):
    account = await trial_db()
    await wallet_trial.grant(account, "8.8.4.7")
    assert (await wallet_trial.grant(account, None))["status"] == "already"

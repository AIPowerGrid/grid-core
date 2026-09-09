# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Global billing through persisted credentials and real PostgreSQL holds.

No HTTP, OAuth provider, GPU, or concurrency claim: other suites own those
boundaries. This matrix pins the legacy-key/quota-exemption activation contract.
Free/promo availability and holder discounts are explicitly zero fixtures.
"""

import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from grid_api import database
from grid_api.services import accounts, credits
from grid_api.v2 import schema as tables

PG = os.environ.get("CREDITS_TEST_DB_URL", "")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not PG.startswith("postgresql"), reason="requires disposable PostgreSQL"),
]


@pytest_asyncio.fixture
async def pg(monkeypatch):
    name = f"consumer_activation_{uuid4().hex}"
    admin = create_async_engine(PG)
    engine = create_async_engine(PG, connect_args={"server_settings": {"search_path": name}})
    try:
        async with admin.begin() as conn:
            await conn.execute(sa.schema.CreateSchema(name))
        async with engine.begin() as conn:
            await conn.run_sync(tables.metadata.create_all)
        monkeypatch.setattr(database, "_session_factory", async_sessionmaker(engine, expire_on_commit=False))
        monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "on")
        monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
        for setting in ("CHARGING_ALLOW_ACCOUNTS", "CHARGING_ALLOW_SERVICES", "CHARGING_ALLOW_MODELS"):
            monkeypatch.setattr(credits, setting, frozenset({"deliberately-unmatched"}))
        monkeypatch.setattr(credits, "_promo_first", AsyncMock(return_value=0))
        monkeypatch.setattr(credits, "_free_first", AsyncMock(return_value=0))
        monkeypatch.setattr(credits, "holder_discount_bps", AsyncMock(return_value=0))
        monkeypatch.setattr(credits, "_economic_alert", lambda *args, **kwargs: None)
        yield
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(sa.schema.DropSchema(name, cascade=True, if_exists=True))
        await admin.dispose()


async def credential(kind):
    aid = uuid4()
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(tables.accounts).values(
                id=aid,
                flags={"quota_exempt": True, "paid": True},
            ),
        )
        await session.commit()
    key = await accounts.issue_key(aid, is_session=kind == "legacy-session")
    if kind.startswith("legacy"):
        async with await database.new_session() as session:
            await session.execute(
                sa.update(tables.api_keys)
                .where(
                    tables.api_keys.c.account_id == aid,
                )
                .values(scopes=[]),
            )
            await session.commit()
    return aid, key


@pytest.mark.parametrize("kind", ["legacy-key", "legacy-session", "scoped-key"])
@pytest.mark.parametrize("funded", [False, True])
@pytest.mark.parametrize(
    "modality,model",
    [
        ("text", "gpt-oss-120b"),
        ("image", "z-image-turbo"),
        ("video", "ltx-2.3"),
        ("audio", "ace-step-v1.5-xl-turbo"),
    ],
)
async def test_global_charging_has_no_legacy_key_or_quota_exemption(pg, kind, funded, modality, model):
    aid, key = await credential(kind)
    user = await accounts.authenticate(key, required_scope="inference.submit")
    assert user["account_id"] == aid
    assert user["quota_exempt"] is True
    assert credits.charging_enabled_for(user, model) is True
    initial = 1_000_000 if funded else 0
    if funded:
        assert await credits.credit(aid, initial, "test_funding", f"seed:{aid}")
    job = str(uuid4())
    if modality == "text":
        result = await credits.authorize_request(user, model, 100, 128, job, record_reservation=True)
    else:
        result = await credits.authorize_media(
            aid,
            model,
            modality,
            1,
            2,
            job,
            user=user,
            record_reservation=True,
        )
    async with await database.new_session() as session:
        hold = (
            (
                await session.execute(
                    sa.select(tables.reservations).where(
                        tables.reservations.c.job_id == job,
                    ),
                )
            )
            .mappings()
            .one_or_none()
        )
        assert await session.scalar(sa.select(sa.func.count()).select_from(tables.ledger)) == 0
    if not funded:
        assert result["ok"] is False and result["status"] == "insufficient"
        assert hold is None
        assert await credits.get_balance(aid) == 0
        return
    assert result["ok"] is True and result["reserved"] > 0
    assert hold["account_id"] == aid and hold["status"] == "held"
    assert hold["free_micro"] == hold["promo_micro"] == 0
    assert hold["reserved_micro"] == result["reserved"]
    assert await credits.get_balance(aid) == initial - result["reserved"]
    await credits.release_job(job)
    await credits.release_job(job)
    assert await credits.get_balance(aid) == initial
    async with await database.new_session() as session:
        assert (
            await session.scalar(
                sa.select(tables.reservations.c.status).where(
                    tables.reservations.c.job_id == job,
                ),
            )
            == "settled"
        )
        refund = (
            (
                await session.execute(
                    sa.select(tables.credit_ledger).where(
                        tables.credit_ledger.c.ref == f"{job}:refund",
                    ),
                )
            )
            .mappings()
            .one()
        )
        assert refund["reason"] == "release:failed"
        assert refund["delta_micro"] == result["reserved"]
        assert await session.scalar(sa.select(sa.func.count()).select_from(tables.ledger)) == 0


@pytest.mark.parametrize("scope", ["worker.connect", "validator.submit", "account.read"])
async def test_non_inference_credentials_cannot_enter_billing(pg, scope):
    aid, _ = await credential("scoped-key")
    key = await accounts.issue_key(aid, scopes=[scope])
    with pytest.raises(HTTPException) as error:
        await accounts.authenticate(key, required_scope="inference.submit")
    assert error.value.status_code == 403
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(tables.reservations)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(tables.ledger)) == 0

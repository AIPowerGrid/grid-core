# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-Postgres proof that active qualification cannot be reset accidentally."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from grid_api.services import validator_operators
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validators as validators_t

PG_URL = os.environ.get("VALIDATORS_TEST_DB_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL.startswith("postgresql"),
    reason="set VALIDATORS_TEST_DB_URL to a disposable PostgreSQL database",
)


@pytest_asyncio.fixture
async def pg(monkeypatch):
    namespace = "validator_operator_review_" + uuid4().hex
    engine = create_async_engine(
        PG_URL,
        execution_options={"schema_translate_map": {None: namespace}},
    )
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(namespace))
            await connection.run_sync(metadata.create_all)
        created = True
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def new_session():
            return factory()

        monkeypatch.setattr(validator_operators, "new_session", new_session)
        monkeypatch.setattr(
            validator_operators,
            "get_settings",
            lambda: SimpleNamespace(
                validator_cohort_baseline_version="v0.1.0-preview.13",
            ),
        )
        yield factory
    finally:
        if created:
            async with engine.begin() as connection:
                await connection.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


@pytest.mark.asyncio
async def test_active_candidate_requires_explicit_restart_on_postgres(pg):
    now = datetime(2026, 9, 1, 19, tzinfo=UTC)
    started = now - timedelta(hours=24)
    account_id = uuid4()
    validator_id = "val_" + uuid4().hex
    async with pg() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
        await session.execute(
            sa.insert(validators_t).values(
                id=validator_id,
                account_id=account_id,
                signing_wallet="0x" + "1" * 40,
                software_version="v0.1.0-preview.13",
                capabilities=["text.generated.v8"],
                registration_signature="fixture",
                status="active",
                last_heartbeat=now,
                operator_group_id="opg_postgres_restart_01",
                independence_status="candidate",
                qualification_started_at=started,
                heartbeat_sample_count=100,
                last_heartbeat_sampled_at=now - timedelta(minutes=5),
                created=started,
                updated=now,
            ),
        )
        await session.commit()

    async def protected_state():
        async with pg() as session:
            row = (
                await session.execute(
                    sa.select(validators_t).where(validators_t.c.id == validator_id),
                )
            ).mappings().one()
        return {
            key: row[key]
            for key in (
                "operator_group_id",
                "independence_status",
                "qualification_started_at",
                "heartbeat_sample_count",
                "last_heartbeat_sampled_at",
                "independence_reviewed_at",
                "independence_expires_at",
                "independence_review_ref",
                "updated",
            )
        }

    original = await protected_state()

    blocked = await validator_operators.review_operator(
        validator_id,
        action="candidate",
        operator_group_id="opg_postgres_restart_01",
        review_ref="review:postgres-accidental",
        now=now,
    )
    assert blocked["eligible_to_apply"] is False
    with pytest.raises(
        validator_operators.OperatorReviewError,
        match="qualification is already active",
    ):
        await validator_operators.review_operator(
            validator_id,
            action="candidate",
            operator_group_id="opg_postgres_restart_01",
            review_ref="review:postgres-accidental",
            expected_digest=blocked["current_digest"],
            apply=True,
            now=now,
        )

    assert await protected_state() == original

    preview = await validator_operators.review_operator(
        validator_id,
        action="candidate",
        operator_group_id="opg_postgres_restart_01",
        review_ref="review:postgres-deliberate",
        restart_qualification=True,
        now=now,
    )
    applied = await validator_operators.review_operator(
        validator_id,
        action="candidate",
        operator_group_id="opg_postgres_restart_01",
        review_ref="review:postgres-deliberate",
        restart_qualification=True,
        expected_digest=preview["current_digest"],
        apply=True,
        now=now,
    )
    assert applied["eligible_to_apply"] is True

    async with pg() as session:
        restarted = (
            await session.execute(
                sa.select(validators_t).where(validators_t.c.id == validator_id),
            )
        ).mappings().one()
    assert restarted["qualification_started_at"] == now
    assert restarted["heartbeat_sample_count"] == 0
    assert restarted["last_heartbeat_sampled_at"] is None
    assert restarted["independence_review_ref"] == "review:postgres-deliberate"
    assert restarted["updated"] == now


@pytest.mark.asyncio
async def test_restart_flag_is_candidate_only_on_postgres(pg):
    with pytest.raises(
        validator_operators.OperatorReviewError,
        match="valid only for a candidate transition",
    ):
        await validator_operators.review_operator(
            "val_missing",
            action="verify",
            review_ref="review:postgres-invalid-restart",
            restart_qualification=True,
        )

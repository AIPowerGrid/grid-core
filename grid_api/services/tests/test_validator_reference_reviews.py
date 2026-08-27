# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api.config import GridSettings
from grid_api.services import validator_reference_reviews
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validator_reference_workers as references_t
from grid_api.v2.schema import workers as workers_t

NOW = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
MODEL = "krea-2-turbo"
WALLET = "0x" + "1" * 40
VERIFIER = "worker-registry-v2-957685a"
RUNTIME_HASH = "0x10cb9fb1b441747142df35545d69e705e81543516937c7a7b08c3df2ccbb5db2"


def _settings():
    return GridSettings(
        validator_media_bond_chain_id=8453,
        validator_media_bond_contract="0x" + "2" * 40,
        validator_media_bond_verifier_version=VERIFIER,
        validator_media_minimum_bond_raw=10**18,
        validator_media_minimum_quality_pass_rate=0.95,
    )


@pytest_asyncio.fixture
async def database(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def open_session():
        return factory()

    monkeypatch.setattr(validator_reference_reviews, "new_session", open_session)
    yield factory
    await engine.dispose()


async def _worker(factory):
    account_id = uuid4()
    worker_id = uuid4()
    async with factory() as session:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
        await session.execute(
            sa.insert(workers_t).values(
                id=worker_id,
                account_id=account_id,
                name="reviewed-image-rig",
                type="image",
                wallet=WALLET,
                models=[MODEL],
                capabilities={},
                maintenance=False,
                first_seen=NOW - timedelta(days=10),
                last_seen=NOW,
                jobs_completed=100,
                den_earned=0,
            ),
        )
        await session.commit()
    return worker_id, account_id


def _review_kwargs(worker_id):
    return {
        "worker_id": worker_id,
        "model": MODEL,
        "modality": "image",
        "action": "review",
        "review_ref": "review:reference-001",
        "quality_window_start": NOW - timedelta(days=1),
        "quality_window_end": NOW,
        "quality_pass_rate": 0.99,
        "now": NOW,
        "settings": _settings(),
    }


@pytest.mark.asyncio
async def test_review_is_preview_first_and_creates_only_a_paused_row(database):
    worker_id, account_id = await _worker(database)

    preview = await validator_reference_reviews.review_reference(
        **_review_kwargs(worker_id),
    )
    assert preview["current_status"] == "missing"
    assert preview["proposed_status"] == "paused"
    assert preview["bond_evidence_invalidated"] is False

    async with database() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(references_t)) == 0

    applied = await validator_reference_reviews.review_reference(
        **_review_kwargs(worker_id),
        expected_digest=preview["current_digest"],
        apply=True,
    )
    assert applied["proposed_status"] == "paused"

    async with database() as session:
        row = (await session.execute(sa.select(references_t))).mappings().one()
    assert row.account_id == account_id
    assert row.payout_wallet == WALLET
    assert row.status == "paused"
    assert row.quality_pass_rate == pytest.approx(0.99)
    assert row.bond_active is False
    assert row.bond_contract is None
    assert row.bond_verified_at is None


@pytest.mark.asyncio
async def test_activate_requires_fresh_complete_chain_and_quality_proof(database):
    worker_id, _ = await _worker(database)
    review_preview = await validator_reference_reviews.review_reference(
        **_review_kwargs(worker_id),
    )
    await validator_reference_reviews.review_reference(
        **_review_kwargs(worker_id),
        expected_digest=review_preview["current_digest"],
        apply=True,
    )

    with pytest.raises(
        validator_reference_reviews.ReferenceReviewError,
        match="not activation-ready",
    ):
        await validator_reference_reviews.review_reference(
            worker_id,
            model=MODEL,
            modality="image",
            action="activate",
            review_ref="review:activate-001",
            now=NOW,
            settings=_settings(),
        )

    async with database() as session:
        await session.execute(
            sa.update(references_t).values(
                bond_contract="0x" + "2" * 40,
                bond_chain_id=8453,
                bond_finalized_block=123_456,
                bond_finalized_block_hash="0x" + "3" * 64,
                bond_facet_address="0x" + "4" * 40,
                bond_facet_runtime_hash=RUNTIME_HASH,
                bond_amount_raw=Decimal(10**18),
                bond_active=True,
                bond_slashed=False,
                bond_verifier_version=VERIFIER,
                bond_status_reason="active",
                bond_verified_at=NOW,
                updated=NOW,
            ),
        )
        await session.commit()

    preview = await validator_reference_reviews.review_reference(
        worker_id,
        model=MODEL,
        modality="image",
        action="activate",
        review_ref="review:activate-001",
        now=NOW,
        settings=_settings(),
    )
    assert preview["activation_ready"] is True
    applied = await validator_reference_reviews.review_reference(
        worker_id,
        model=MODEL,
        modality="image",
        action="activate",
        review_ref="review:activate-001",
        expected_digest=preview["current_digest"],
        apply=True,
        now=NOW,
        settings=_settings(),
    )
    assert applied["proposed_status"] == "active"

    async with database() as session:
        row = (await session.execute(sa.select(references_t))).mappings().one()
    assert row.status == "active"
    assert row.bond_contract == "0x" + "2" * 40
    assert row.bond_active is True

    async with database() as session:
        await session.execute(
            sa.update(workers_t)
            .where(workers_t.c.id == worker_id)
            .values(wallet="0x" + "7" * 40),
        )
        await session.commit()
    identity_preview = await validator_reference_reviews.review_reference(
        **_review_kwargs(worker_id),
    )
    assert identity_preview["proposed_status"] == "paused"
    assert identity_preview["bond_evidence_invalidated"] is True
    await validator_reference_reviews.review_reference(
        **_review_kwargs(worker_id),
        expected_digest=identity_preview["current_digest"],
        apply=True,
    )
    async with database() as session:
        identity_row = (await session.execute(sa.select(references_t))).mappings().one()
    assert identity_row.status == "paused"
    assert identity_row.payout_wallet == "0x" + "7" * 40
    assert identity_row.bond_contract is None
    assert identity_row.bond_active is False
    assert identity_row.bond_status_reason == "identity_changed"


@pytest.mark.asyncio
async def test_apply_rejects_stale_digest_and_identity_drift(database):
    worker_id, _ = await _worker(database)
    preview = await validator_reference_reviews.review_reference(
        **_review_kwargs(worker_id),
    )
    async with database() as session:
        await session.execute(
            sa.update(workers_t)
            .where(workers_t.c.id == worker_id)
            .values(wallet="0x" + "6" * 40),
        )
        await session.commit()

    with pytest.raises(
        validator_reference_reviews.ReferenceReviewError,
        match="state changed",
    ):
        await validator_reference_reviews.review_reference(
            **_review_kwargs(worker_id),
            expected_digest=preview["current_digest"],
            apply=True,
        )


@pytest.mark.asyncio
async def test_pause_and_revoke_are_explicit_and_revocation_is_terminal(database):
    worker_id, _ = await _worker(database)
    review_preview = await validator_reference_reviews.review_reference(
        **_review_kwargs(worker_id),
    )
    await validator_reference_reviews.review_reference(
        **_review_kwargs(worker_id),
        expected_digest=review_preview["current_digest"],
        apply=True,
    )
    revoke_preview = await validator_reference_reviews.review_reference(
        worker_id,
        model=MODEL,
        modality="image",
        action="revoke",
        review_ref="review:revoke-001",
        now=NOW,
        settings=_settings(),
    )
    await validator_reference_reviews.review_reference(
        worker_id,
        model=MODEL,
        modality="image",
        action="revoke",
        review_ref="review:revoke-001",
        expected_digest=revoke_preview["current_digest"],
        apply=True,
        now=NOW,
        settings=_settings(),
    )

    with pytest.raises(
        validator_reference_reviews.ReferenceReviewError,
        match="revoked reference",
    ):
        await validator_reference_reviews.review_reference(
            **_review_kwargs(worker_id),
        )

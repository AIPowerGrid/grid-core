# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api.services import worker_control_reviews
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import worker_control_reviews as controls_t
from grid_api.v2.schema import workers as workers_t
from scripts import review_worker_control as review_worker_control_cli

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
WALLET = "0x" + "1" * 40
GROUP = "opg_worker_control_0001"


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

    monkeypatch.setattr(worker_control_reviews, "new_session", open_session)
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
                name="worker-control-review-test",
                type="image",
                wallet=WALLET,
                models=["krea-2-turbo"],
                capabilities={},
                maintenance=False,
                first_seen=NOW - timedelta(days=1),
                last_seen=NOW,
                jobs_completed=0,
                den_earned=0,
            ),
        )
        await session.commit()
    return worker_id, account_id


@pytest.mark.asyncio
async def test_verify_is_preview_first_identity_bound_and_expiring(database):
    worker_id, account_id = await _worker(database)
    kwargs = {
        "worker_id": worker_id,
        "action": "verify",
        "operator_group_id": GROUP,
        "review_ref": "review:worker-control-001",
        "review_days": 30,
        "now": NOW,
    }

    preview = await worker_control_reviews.review_worker_control(**kwargs)
    assert preview["current_status"] == "missing"
    assert preview["proposed_status"] == "verified"
    assert preview["economic_effect"] == "none"
    async with database() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(controls_t)) == 0

    applied = await worker_control_reviews.review_worker_control(
        **kwargs,
        expected_digest=preview["current_digest"],
        apply=True,
    )
    assert applied["expires_at"] == (NOW + timedelta(days=30)).isoformat()
    async with database() as session:
        row = dict((await session.execute(sa.select(controls_t))).mappings().one())
    assert row["account_id"] == account_id
    assert row["payout_wallet"] == WALLET
    assert row["operator_group_id"] == GROUP
    assert worker_control_reviews.fresh_review_reasons(
        row,
        worker_account_id=account_id,
        worker_wallet=WALLET,
        now=NOW,
    ) == []


@pytest.mark.asyncio
async def test_apply_rejects_stale_digest_after_worker_identity_change(database):
    worker_id, _account_id = await _worker(database)
    preview = await worker_control_reviews.review_worker_control(
        worker_id,
        action="verify",
        operator_group_id=GROUP,
        review_ref="review:worker-control-002",
        now=NOW,
    )
    async with database() as session:
        await session.execute(
            sa.update(workers_t)
            .where(workers_t.c.id == worker_id)
            .values(wallet="0x" + "2" * 40),
        )
        await session.commit()

    with pytest.raises(
        worker_control_reviews.WorkerControlReviewError,
        match="state changed",
    ):
        await worker_control_reviews.review_worker_control(
            worker_id,
            action="verify",
            operator_group_id=GROUP,
            review_ref="review:worker-control-002",
            expected_digest=preview["current_digest"],
            apply=True,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_rejected_review_can_be_verified_but_revocation_is_terminal(database):
    worker_id, _account_id = await _worker(database)
    reject = await worker_control_reviews.review_worker_control(
        worker_id,
        action="reject",
        review_ref="review:worker-control-reject",
        now=NOW,
    )
    await worker_control_reviews.review_worker_control(
        worker_id,
        action="reject",
        review_ref="review:worker-control-reject",
        expected_digest=reject["current_digest"],
        apply=True,
        now=NOW,
    )
    verify = await worker_control_reviews.review_worker_control(
        worker_id,
        action="verify",
        operator_group_id=GROUP,
        review_ref="review:worker-control-verify",
        now=NOW,
    )
    await worker_control_reviews.review_worker_control(
        worker_id,
        action="verify",
        operator_group_id=GROUP,
        review_ref="review:worker-control-verify",
        expected_digest=verify["current_digest"],
        apply=True,
        now=NOW,
    )
    revoke = await worker_control_reviews.review_worker_control(
        worker_id,
        action="revoke",
        review_ref="review:worker-control-revoke",
        now=NOW,
    )
    await worker_control_reviews.review_worker_control(
        worker_id,
        action="revoke",
        review_ref="review:worker-control-revoke",
        expected_digest=revoke["current_digest"],
        apply=True,
        now=NOW,
    )

    with pytest.raises(
        worker_control_reviews.WorkerControlReviewError,
        match="revoked worker control review",
    ):
        await worker_control_reviews.review_worker_control(
            worker_id,
            action="verify",
            operator_group_id=GROUP,
            review_ref="review:worker-control-impossible",
            now=NOW,
        )


def test_fresh_review_rejects_expiry_and_identity_drift():
    account_id = uuid4()
    row = {
        "account_id": account_id,
        "payout_wallet": WALLET,
        "operator_group_id": GROUP,
        "status": "verified",
        "reviewed_at": NOW - timedelta(days=31),
        "expires_at": NOW - timedelta(days=1),
    }
    reasons = worker_control_reviews.fresh_review_reasons(
        row,
        worker_account_id=account_id,
        worker_wallet="0x" + "3" * 40,
        now=NOW,
    )
    assert "worker payout wallet differs from control review" in reasons
    assert "worker control review is expired" in reasons


@pytest.mark.parametrize(
    "group",
    ["operator-one", "opg_short", "opg_private email@example.com"],
)
@pytest.mark.asyncio
async def test_verify_rejects_nonopaque_group_ids(database, group):
    worker_id, _account_id = await _worker(database)
    with pytest.raises(
        worker_control_reviews.WorkerControlReviewError,
        match="opaque opg",
    ):
        await worker_control_reviews.review_worker_control(
            worker_id,
            action="verify",
            operator_group_id=group,
            review_ref="review:worker-control-invalid",
            now=NOW,
        )


@pytest.mark.parametrize(
    "arguments, expected",
    [
        (
            [
                "review_worker_control.py",
                "--worker-id",
                "00000000-0000-0000-0000-000000000001",
                "--action",
                "verify",
                "--review-ref",
                "review:cli-missing-group",
            ],
            "verify requires --operator-group",
        ),
        (
            [
                "review_worker_control.py",
                "--worker-id",
                "00000000-0000-0000-0000-000000000001",
                "--action",
                "reject",
                "--review-ref",
                "review:cli-missing-digest",
                "--apply",
            ],
            "--apply requires --expect-digest",
        ),
    ],
)
def test_cli_enforces_preview_first_contract(monkeypatch, capsys, arguments, expected):
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit, match="2"):
        review_worker_control_cli.main()
    assert expected in capsys.readouterr().err

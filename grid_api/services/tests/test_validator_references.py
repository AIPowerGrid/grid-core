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

from grid_api.services import validator_references
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validator_reference_workers as references_t
from grid_api.v2.schema import worker_control_reviews as controls_t
from grid_api.v2.schema import workers as workers_t

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
MODEL = "krea-2-turbo"
BOND_CONTRACT = "0x" + "a" * 40
BOND_RUNTIME_HASH = "0x10cb9fb1b441747142df35545d69e705e81543516937c7a7b08c3df2ccbb5db2"
VERIFIER = "worker-registry-v2-957685a"
MINIMUM_BOND_RAW = 1_000_000
BOND_POLICY = {
    "expected_chain_id": 8453,
    "expected_bond_contract": BOND_CONTRACT,
    "expected_verifier_version": VERIFIER,
    "expected_facet_runtime_hash": BOND_RUNTIME_HASH,
    # SQLite cannot represent a one-wei boundary near 1e18 exactly. The same
    # Numeric(78, 0) policy is proved at token-scale in the Postgres suite.
    "minimum_bond_raw": MINIMUM_BOND_RAW,
    "minimum_quality_pass_rate": 0.95,
}


class _FakeMappingsResult:
    def __init__(self, *, one=None, rows=None):
        self._one = one
        self._rows = rows

    def mappings(self):
        return self

    def one_or_none(self):
        return self._one

    def all(self):
        return self._rows or []


class _SequencedSession:
    bind = None

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _statement):
        assert self._results, "unexpected selector query"
        return self._results.pop(0)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _worker(
    session,
    index,
    *,
    account_id=None,
    wallet=None,
    last_seen=NOW,
    control=True,
    operator_group_id=None,
):
    account_id = account_id or uuid4()
    wallet = wallet or f"0x{index:040x}"
    worker_id = uuid4()
    account_exists = await session.scalar(
        sa.select(accounts_t.c.id).where(accounts_t.c.id == account_id),
    )
    if account_exists is None:
        await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
    await session.execute(
        sa.insert(workers_t).values(
            id=worker_id,
            account_id=account_id,
            name=f"media-rig-{index}",
            type="image",
            wallet=wallet,
            models=[MODEL],
            capabilities={},
            maintenance=False,
            first_seen=NOW - timedelta(days=10),
            last_seen=last_seen,
            jobs_completed=100,
            den_earned=0,
        ),
    )
    if control:
        await session.execute(
            sa.insert(controls_t).values(
                worker_id=worker_id,
                account_id=account_id,
                payout_wallet=wallet,
                operator_group_id=operator_group_id or f"opg_worker_{index:08d}",
                status="verified",
                reviewed_at=NOW - timedelta(days=1),
                expires_at=NOW + timedelta(days=29),
                review_ref=f"review:worker-{index}",
                created=NOW - timedelta(days=1),
                updated=NOW - timedelta(days=1),
            ),
        )
    return worker_id, account_id, wallet


async def _reference(
    session,
    worker,
    *,
    status="active",
    bond_age=timedelta(minutes=1),
    quality_age=timedelta(hours=1),
    slashed=False,
    chain_id=8453,
    bond_contract=BOND_CONTRACT,
    bond_amount_raw=MINIMUM_BOND_RAW,
    verifier_version=VERIFIER,
    finalized_block_hash="0x" + "d" * 64,
    facet_address="0x" + "e" * 40,
    facet_runtime_hash=BOND_RUNTIME_HASH,
    bond_status_reason="active",
    quality_pass_rate=0.99,
    quality_window_end=NOW,
    last_selected=None,
    selection_count=0,
):
    worker_id, account_id, wallet = worker
    await session.execute(
        sa.insert(references_t).values(
            worker_id=worker_id,
            model=MODEL,
            modality="image",
            account_id=account_id,
            payout_wallet=wallet,
            status=status,
            status_reason="test fixture",
            bond_contract=bond_contract,
            bond_chain_id=chain_id,
            bond_finalized_block=123456,
            bond_finalized_block_hash=finalized_block_hash,
            bond_facet_address=facet_address,
            bond_facet_runtime_hash=facet_runtime_hash,
            bond_amount_raw=Decimal(bond_amount_raw),
            bond_active=True,
            bond_slashed=slashed,
            bond_verifier_version=verifier_version,
            bond_status_reason=bond_status_reason,
            bond_verified_at=NOW - bond_age,
            quality_window_start=NOW - timedelta(days=1),
            quality_window_end=quality_window_end,
            quality_pass_rate=quality_pass_rate,
            quality_reviewed_at=NOW - quality_age,
            last_selected=last_selected,
            selection_count=selection_count,
            created=NOW,
            updated=NOW,
        ),
    )


@pytest.mark.asyncio
async def test_selects_two_fresh_independent_online_references_and_updates_usage(session):
    candidate = await _worker(session, 1)
    refs = [await _worker(session, index) for index in range(2, 5)]
    for ref in refs:
        await _reference(session, ref)
    await session.commit()

    selected = await validator_references.select_reference_workers(
        session,
        model=MODEL,
        modality="image",
        candidate_worker_id=candidate[0],
        online_model_worker_ids=[candidate[0], *(ref[0] for ref in refs)],
        now=NOW,
        **BOND_POLICY,
    )

    assert len(selected) == 2
    assert len({item.account_id for item in selected}) == 2
    assert candidate[1] not in {item.account_id for item in selected}
    usage = (
        await session.execute(
            sa.select(references_t.c.worker_id, references_t.c.selection_count).where(
                references_t.c.worker_id.in_([item.worker_id for item in selected]),
            ),
        )
    ).all()
    assert {count for _, count in usage} == {1}


@pytest.mark.asyncio
async def test_preview_applies_same_rules_without_updating_usage(session):
    candidate = await _worker(session, 5)
    refs = [await _worker(session, index) for index in range(6, 9)]
    for ref in refs:
        await _reference(session, ref)
    await session.commit()

    selected = await validator_references.preview_reference_workers(
        session,
        model=MODEL,
        modality="image",
        candidate_worker_id=candidate[0],
        online_model_worker_ids=[candidate[0], *(ref[0] for ref in refs)],
        now=NOW,
        **BOND_POLICY,
    )

    assert len(selected) == 2
    assert candidate[1] not in {item.account_id for item in selected}
    usage = (
        (
            await session.execute(
                sa.select(references_t.c.selection_count).where(
                    references_t.c.worker_id.in_([item.worker_id for item in selected]),
                ),
            )
        )
        .scalars()
        .all()
    )
    assert usage == [0, 0]


@pytest.mark.asyncio
async def test_preview_fails_closed_when_independence_is_insufficient(session):
    candidate = await _worker(session, 9)
    shared_group = "opg_preview_shared_control"
    refs = [await _worker(session, index, operator_group_id=shared_group) for index in (901, 902)]
    for ref in refs:
        await _reference(session, ref)
    await session.commit()

    with pytest.raises(validator_references.ReferencePoolUnavailable, match="found 1"):
        await validator_references.preview_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[candidate[0], *(ref[0] for ref in refs)],
            now=NOW,
            **BOND_POLICY,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"status": "paused"},
        {"bond_age": timedelta(hours=1)},
        {"quality_age": timedelta(days=8)},
        {"slashed": True},
        {"chain_id": 1},
        {"bond_contract": "0x" + "c" * 40},
        {"bond_amount_raw": MINIMUM_BOND_RAW - 1},
        {"verifier_version": "unknown-registry"},
        {"finalized_block_hash": None},
        {"facet_address": None},
        {"facet_runtime_hash": "0x" + "f" * 64},
        {"bond_status_reason": "sync_faulted"},
        {"quality_pass_rate": 0.94},
        {"quality_window_end": NOW + timedelta(minutes=1)},
    ],
)
async def test_ineligible_bond_or_quality_snapshot_fails_closed(session, override):
    candidate = await _worker(session, 10)
    good = await _worker(session, 11)
    bad = await _worker(session, 12)
    await _reference(session, good)
    await _reference(session, bad, **override)
    await session.commit()

    with pytest.raises(validator_references.ReferencePoolUnavailable, match="found 1"):
        await validator_references.select_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[candidate[0], good[0], bad[0]],
            now=NOW,
            **BOND_POLICY,
        )


@pytest.mark.asyncio
async def test_same_account_cannot_fill_reference_quorum(session):
    candidate = await _worker(session, 20)
    shared_account = uuid4()
    first = await _worker(session, 21, account_id=shared_account)
    second = await _worker(session, 22, account_id=shared_account)
    await _reference(session, first)
    await _reference(session, second)
    await session.commit()

    with pytest.raises(validator_references.ReferencePoolUnavailable, match="found 1"):
        await validator_references.select_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[candidate[0], first[0], second[0]],
            now=NOW,
            **BOND_POLICY,
        )


@pytest.mark.asyncio
async def test_same_payout_wallet_cannot_fill_reference_quorum(session):
    candidate = await _worker(session, 23)
    shared_wallet = "0x" + "b" * 40
    first = await _worker(session, 24, wallet=shared_wallet)
    second = await _worker(session, 25, wallet=shared_wallet.upper().replace("0X", "0x"))
    await _reference(session, first)
    await _reference(session, second)
    await session.commit()

    with pytest.raises(validator_references.ReferencePoolUnavailable, match="found 1"):
        await validator_references.select_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[candidate[0], first[0], second[0]],
            now=NOW,
            **BOND_POLICY,
        )


@pytest.mark.asyncio
async def test_same_operator_cannot_fill_reference_quorum_with_distinct_identities(session):
    candidate = await _worker(session, 26)
    first = await _worker(
        session,
        27,
        operator_group_id="opg_shared_reference_control",
    )
    second = await _worker(
        session,
        28,
        operator_group_id="opg_shared_reference_control",
    )
    await _reference(session, first)
    await _reference(session, second)
    await session.commit()

    with pytest.raises(validator_references.ReferencePoolUnavailable, match="found 1"):
        await validator_references.select_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[candidate[0], first[0], second[0]],
            now=NOW,
            **BOND_POLICY,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["account_id", "payout_wallet", "operator_group_id"])
async def test_post_lock_independence_rejects_stale_initial_identity(collision, monkeypatch):
    candidate_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    candidate_account = uuid4()
    first_account = uuid4()
    second_account = uuid4()
    first_wallet = "0x" + "1" * 40
    second_wallet = "0x" + "2" * 40
    first_group = "opg_reference_alpha"
    second_group = "opg_reference_beta"

    initial_first = {
        "worker_id": first_id,
        "account_id": first_account,
        "payout_wallet": first_wallet,
        "operator_group_id": first_group,
        "last_selected": None,
        "selection_count": 0,
    }
    initial_second = {
        "worker_id": second_id,
        "account_id": second_account,
        "payout_wallet": second_wallet,
        "operator_group_id": second_group,
        "last_selected": None,
        "selection_count": 0,
    }
    locked_second = dict(initial_second)
    locked_second[collision] = initial_first[collision]

    session = _SequencedSession(
        [
            _FakeMappingsResult(
                one={"account_id": candidate_account, "wallet": "0x" + "3" * 40},
            ),
            _FakeMappingsResult(
                one={
                    "account_id": candidate_account,
                    "payout_wallet": "0x" + "3" * 40,
                    "operator_group_id": "opg_candidate_control",
                    "status": "verified",
                    "reviewed_at": NOW - timedelta(days=1),
                    "expires_at": NOW + timedelta(days=1),
                },
            ),
            _FakeMappingsResult(rows=[initial_first, initial_second]),
            _FakeMappingsResult(one=initial_first),
            _FakeMappingsResult(one=locked_second),
        ],
    )
    monkeypatch.setattr(
        validator_references,
        "_weighted_choice",
        lambda rows, *, now: rows[0],
    )

    with pytest.raises(validator_references.ReferencePoolUnavailable, match="found 1"):
        await validator_references.select_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate_id,
            online_model_worker_ids=[candidate_id, first_id, second_id],
            now=NOW,
            **BOND_POLICY,
        )

    assert session._results == []


@pytest.mark.asyncio
async def test_candidate_and_references_require_fresh_distinct_control_reviews(session):
    candidate_group = "opg_candidate_common_control"
    candidate = await _worker(session, 29, operator_group_id=candidate_group)
    same_operator = await _worker(session, 291, operator_group_id=candidate_group)
    independent = await _worker(session, 292)
    await _reference(session, same_operator)
    await _reference(session, independent)
    await session.commit()

    with pytest.raises(validator_references.ReferencePoolUnavailable, match="found 1"):
        await validator_references.select_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[candidate[0], same_operator[0], independent[0]],
            now=NOW,
            **BOND_POLICY,
        )

    await session.rollback()
    await session.execute(
        sa.delete(controls_t).where(controls_t.c.worker_id == candidate[0]),
    )
    await session.commit()
    with pytest.raises(
        validator_references.ReferencePoolUnavailable,
        match="candidate worker control review is unavailable",
    ):
        await validator_references.select_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[candidate[0], same_operator[0], independent[0]],
            now=NOW,
            **BOND_POLICY,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control_values",
    [
        {"operator_group_id": "malformed-control-group"},
        {"expires_at": NOW - timedelta(seconds=1)},
    ],
)
async def test_malformed_or_expired_reference_control_fails_closed(session, control_values):
    candidate = await _worker(session, 293)
    good = await _worker(session, 294)
    bad = await _worker(session, 295)
    await _reference(session, good)
    await _reference(session, bad)
    await session.execute(
        sa.update(controls_t).where(controls_t.c.worker_id == bad[0]).values(**control_values),
    )
    await session.commit()

    with pytest.raises(validator_references.ReferencePoolUnavailable, match="found 1"):
        await validator_references.select_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[candidate[0], good[0], bad[0]],
            now=NOW,
            **BOND_POLICY,
        )


@pytest.mark.asyncio
async def test_offline_candidate_or_reference_fails_closed(session):
    candidate = await _worker(session, 30)
    first = await _worker(session, 31)
    second = await _worker(session, 32, last_seen=NOW - timedelta(hours=1))
    await _reference(session, first)
    await _reference(session, second)
    await session.commit()

    with pytest.raises(validator_references.ReferencePoolUnavailable, match="found 1"):
        await validator_references.select_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[candidate[0], first[0], second[0]],
            now=NOW,
            **BOND_POLICY,
        )
    with pytest.raises(validator_references.ReferencePoolUnavailable, match="not online"):
        await validator_references.select_reference_workers(
            session,
            model=MODEL,
            modality="image",
            candidate_worker_id=candidate[0],
            online_model_worker_ids=[first[0], second[0]],
            now=NOW,
            **BOND_POLICY,
        )


@pytest.mark.asyncio
async def test_recently_used_pair_is_penalized(monkeypatch, session):
    candidate = await _worker(session, 40)
    old_refs = [await _worker(session, index) for index in (41, 42)]
    recent_refs = [await _worker(session, index) for index in (43, 44)]
    for ref in old_refs:
        await _reference(session, ref, last_selected=None, selection_count=0)
    for ref in recent_refs:
        await _reference(
            session,
            ref,
            last_selected=NOW - timedelta(minutes=1),
            selection_count=100,
        )
    await session.commit()

    class PickFirst:
        @staticmethod
        def uniform(start, end):
            return start

    monkeypatch.setattr(validator_references.secrets, "SystemRandom", PickFirst)
    selected = await validator_references.select_reference_workers(
        session,
        model=MODEL,
        modality="image",
        candidate_worker_id=candidate[0],
        online_model_worker_ids=[candidate[0], *(ref[0] for ref in old_refs + recent_refs)],
        now=NOW,
        **BOND_POLICY,
    )

    assert {item.worker_id for item in selected} == {ref[0] for ref in old_refs}

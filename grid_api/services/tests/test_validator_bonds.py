# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from web3 import Web3

from grid_api.config import GridSettings
from grid_api.services import validator_bonds
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validator_bond_sync_state as sync_state_t
from grid_api.v2.schema import validator_reference_workers as references_t
from grid_api.v2.schema import workers as workers_t

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
DIAMOND = "0x" + "1" * 40
FACET = "0x" + "2" * 40
OTHER_FACET = "0x" + "3" * 40
RUNTIME = b"reviewed-worker-registry-runtime"
RUNTIME_HASH = Web3.keccak(RUNTIME).hex()
BLOCK_HASH = "0x" + "4" * 64
MODEL = "krea-2-turbo"
VERIFIER = "worker-registry-v2-957685a"
REVIEWED_RUNTIME_HASH = validator_bonds.reviewed_runtime_hash(VERIFIER)
CURRENT_VERIFIER = "worker-registry-v2-7d7a2e8"
CURRENT_RUNTIME_HASH = (
    "0x359fb8372a292a77fe76d156bbda39b35c3170f1ff0edaa1874ea8b87ee3af78"
)


def test_reviewed_verifier_labels_pin_distinct_exact_runtimes():
    assert validator_bonds.REVIEWED_WORKER_REGISTRY_RUNTIMES == {
        VERIFIER: REVIEWED_RUNTIME_HASH,
        CURRENT_VERIFIER: CURRENT_RUNTIME_HASH,
    }
    assert validator_bonds.reviewed_runtime_hash(CURRENT_VERIFIER) == CURRENT_RUNTIME_HASH
    assert validator_bonds.reviewed_runtime_hash(f" {CURRENT_VERIFIER} ") == CURRENT_RUNTIME_HASH
    assert validator_bonds.reviewed_runtime_hash("worker-registry-v2-unreviewed") is None
    assert CURRENT_RUNTIME_HASH != REVIEWED_RUNTIME_HASH


class _Call:
    def __init__(self, value, calls, name):
        self.value = value
        self.calls = calls
        self.name = name

    def call(self, *, block_identifier):
        self.calls.append((self.name, block_identifier))
        return self.value


class _Functions:
    def __init__(self, *, routes, workers, calls):
        self.routes = routes
        self.workers = workers
        self.calls = calls

    def moduleAddress(self, selector):
        key = "0x" + bytes(selector).hex()
        return _Call(self.routes[key], self.calls, f"route:{key}")

    def getWorkerCount(self):
        return _Call(len(self.workers), self.calls, "count")

    def getWorkerAt(self, index):
        return _Call(list(self.workers)[index], self.calls, f"at:{index}")

    def getWorker(self, wallet):
        normalized = str(wallet).lower()
        value = self.workers.get(
            normalized,
            ("0x" + "0" * 40, 0, 0, 0, 0, False, False, 0),
        )
        return _Call(value, self.calls, f"worker:{normalized}")


class _Contract:
    def __init__(self, functions):
        self.functions = functions


class _Eth:
    def __init__(self, *, routes, workers, runtime=RUNTIME, chain_id=8453):
        self.chain_id = chain_id
        self.calls = []
        self.runtime = runtime
        self.contract_value = _Contract(
            _Functions(routes=routes, workers=workers, calls=self.calls),
        )

    def get_block(self, tag):
        number = 123_456 if tag == "finalized" else int(tag)
        return {"number": number, "hash": bytes.fromhex(BLOCK_HASH[2:])}

    def get_code(self, address, *, block_identifier):
        self.calls.append((f"code:{str(address).lower()}", block_identifier))
        return self.runtime

    def contract(self, *, address, abi):
        assert str(address).lower() == DIAMOND
        assert abi
        return self.contract_value


class _Web3:
    def __init__(self, eth, *, connected=True):
        self.eth = eth
        self.connected = connected

    def is_connected(self):
        return self.connected


def _routes(*, one_override=None):
    result = {selector: FACET for selector in validator_bonds.WORKER_REGISTRY_SELECTORS}
    if one_override:
        result[validator_bonds.WORKER_REGISTRY_SELECTORS[0]] = one_override
    return result


def _worker_tuple(wallet, *, amount=1_000, active=True, slashed=False, unbonding_at=0):
    return (wallet, amount, 10, 20, 1_700_000_000, active, slashed, unbonding_at)


def _read_with(
    fake,
    *,
    reference_wallets=None,
    max_workers=100,
    finalized_block=None,
    anchor_block=None,
    anchor_block_hash=None,
):
    wallets = reference_wallets
    if wallets is None:
        wallets = tuple(fake.eth.contract_value.functions.workers)
    return validator_bonds.read_finalized_bond_snapshot(
        rpc_url="https://rpc.invalid",
        expected_chain_id=8453,
        diamond_address=DIAMOND,
        expected_facet_runtime_hash=RUNTIME_HASH,
        reference_wallets=wallets,
        max_workers=max_workers,
        rpc_timeout_seconds=10,
        finalized_block=finalized_block,
        anchor_block=anchor_block,
        anchor_block_hash=anchor_block_hash,
        web3_factory=lambda _url, _timeout: fake,
    )


def test_reads_every_selector_and_worker_at_one_finalized_block():
    wallet = "0x" + "a" * 40
    eth = _Eth(
        routes=_routes(),
        workers={wallet: _worker_tuple(wallet)},
    )

    snapshot = _read_with(_Web3(eth))

    assert snapshot.finalized_block == 123_456
    assert snapshot.finalized_block_hash == BLOCK_HASH
    assert snapshot.facet_address == FACET
    assert snapshot.workers[wallet].amount_raw == 1_000
    assert len([name for name, _ in eth.calls if name.startswith("route:")]) == 16
    assert {block for _, block in eth.calls} == {123_456}


def test_pins_every_read_to_requested_shared_finalized_block():
    wallet = "0x" + "a" * 40
    eth = _Eth(routes=_routes(), workers={wallet: _worker_tuple(wallet)})

    snapshot = _read_with(_Web3(eth), finalized_block=123_455)

    assert snapshot.finalized_block == 123_455
    assert {block for _, block in eth.calls} == {123_455}


def test_rejects_changed_prior_finalized_hash():
    eth = _Eth(routes=_routes(), workers={})
    with pytest.raises(validator_bonds.BondSyncError, match="prior finalized block hash changed"):
        _read_with(
            _Web3(eth),
            anchor_block=123_400,
            anchor_block_hash="0x" + "9" * 64,
        )


@pytest.mark.parametrize(
    ("eth", "match"),
    [
        (
            _Eth(routes=_routes(one_override=OTHER_FACET), workers={}),
            "do not route to one facet",
        ),
        (
            _Eth(routes=_routes(), workers={}, runtime=b"unreviewed"),
            "runtime hash is not reviewed",
        ),
        (
            _Eth(routes=_routes(), workers={}, chain_id=1),
            "chain id does not match",
        ),
    ],
)
def test_rejects_wrong_route_runtime_or_chain(eth, match):
    with pytest.raises(validator_bonds.BondSyncError, match=match):
        _read_with(_Web3(eth))


def test_rejects_inconsistent_worker_state():
    wallet = "0x" + "b" * 40
    inconsistent = _Eth(
        routes=_routes(),
        workers={wallet: _worker_tuple(wallet, active=True, slashed=True)},
    )
    with pytest.raises(validator_bonds.BondSyncError, match="inconsistent active"):
        _read_with(_Web3(inconsistent))


def test_rejects_duplicate_reviewed_reference_wallets():
    wallet = "0x" + "b" * 40
    eth = _Eth(routes=_routes(), workers={wallet: _worker_tuple(wallet)})

    with pytest.raises(validator_bonds.BondSyncError, match="contain a duplicate"):
        _read_with(_Web3(eth), reference_wallets=(wallet, wallet))


def test_rejects_reviewed_reference_count_above_configured_bound():
    first = "0x" + "b" * 40
    second = "0x" + "c" * 40
    eth = _Eth(
        routes=_routes(),
        workers={
            first: _worker_tuple(first),
            second: _worker_tuple(second),
        },
    )

    with pytest.raises(validator_bonds.BondSyncError, match="configured bound"):
        _read_with(_Web3(eth), max_workers=1)


def test_missing_reviewed_wallet_is_inactive_without_failing_snapshot():
    missing = "0x" + "d" * 40
    eth = _Eth(routes=_routes(), workers={})

    snapshot = _read_with(_Web3(eth), reference_wallets=(missing,))

    assert snapshot.workers == {}
    assert (f"worker:{missing}", 123_456) in eth.calls


def test_quorum_requires_distinct_sources_and_exact_snapshot_agreement():
    worker = validator_bonds.WorkerBond(
        wallet="0x" + "d" * 40,
        amount_raw=1_000,
        active=True,
        slashed=False,
        unbonding_at=0,
    )
    expected = _snapshot(worker)
    calls = []

    def agreeing_reader(**kwargs):
        calls.append(kwargs["rpc_url"])
        return expected

    result = validator_bonds.read_quorum_bond_snapshot(
        rpc_url="https://primary.invalid",
        confirmation_rpc_url="https://confirmation.invalid",
        expected_chain_id=8453,
        diamond_address=DIAMOND,
        expected_facet_runtime_hash=RUNTIME_HASH,
        reference_wallets=(worker.wallet,),
        max_workers=100,
        rpc_timeout_seconds=10,
        single_reader=agreeing_reader,
    )
    assert result == expected
    assert calls == ["https://primary.invalid", "https://confirmation.invalid"]

    with pytest.raises(validator_bonds.BondSyncError, match="distinct HTTP"):
        validator_bonds.read_quorum_bond_snapshot(
            rpc_url="https://same.invalid",
            confirmation_rpc_url="https://same.invalid/another-path",
            expected_chain_id=8453,
            diamond_address=DIAMOND,
            expected_facet_runtime_hash=RUNTIME_HASH,
            reference_wallets=(worker.wallet,),
            max_workers=100,
            rpc_timeout_seconds=10,
            single_reader=agreeing_reader,
        )


def test_quorum_uses_shared_finalized_history_when_provider_tips_lag():
    worker = validator_bonds.WorkerBond(
        wallet="0x" + "e" * 40,
        amount_raw=1_000,
        active=True,
        slashed=False,
        unbonding_at=0,
    )
    calls = []

    def lagging_reader(**kwargs):
        requested = kwargs.get("finalized_block")
        calls.append((kwargs["rpc_url"], requested))
        if requested is not None:
            return _snapshot(worker, block=requested)
        tip = 123_456 if kwargs["rpc_url"] == "https://primary.invalid" else 123_455
        return _snapshot(worker, block=tip)

    result = validator_bonds.read_quorum_bond_snapshot(
        rpc_url="https://primary.invalid",
        confirmation_rpc_url="https://confirmation.invalid",
        expected_chain_id=8453,
        diamond_address=DIAMOND,
        expected_facet_runtime_hash=RUNTIME_HASH,
        reference_wallets=(worker.wallet,),
        max_workers=100,
        rpc_timeout_seconds=10,
        single_reader=lagging_reader,
    )

    assert result.finalized_block == 123_455
    assert calls == [
        ("https://primary.invalid", None),
        ("https://confirmation.invalid", None),
        ("https://primary.invalid", 123_455),
        ("https://confirmation.invalid", 123_455),
    ]


def test_quorum_rejects_disagreement_on_shared_finalized_history():
    worker = validator_bonds.WorkerBond(
        wallet="0x" + "f" * 40,
        amount_raw=1_000,
        active=True,
        slashed=False,
        unbonding_at=0,
    )
    expected = _snapshot(worker)

    def disagreeing_reader(**kwargs):
        if kwargs["rpc_url"] == "https://primary.invalid":
            return expected
        return _snapshot(worker, block=expected.finalized_block - 1)

    with pytest.raises(validator_bonds.BondSyncError, match="do not agree"):
        validator_bonds.read_quorum_bond_snapshot(
            rpc_url="https://primary.invalid",
            confirmation_rpc_url="https://confirmation.invalid",
            expected_chain_id=8453,
            diamond_address=DIAMOND,
            expected_facet_runtime_hash=RUNTIME_HASH,
            reference_wallets=(worker.wallet,),
            max_workers=100,
            rpc_timeout_seconds=10,
            single_reader=disagreeing_reader,
        )


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


async def _reference(session, index, *, prior_block=None):
    account_id = uuid4()
    worker_id = uuid4()
    wallet = f"0x{index:040x}"
    await session.execute(sa.insert(accounts_t).values(id=account_id, flags={}))
    await session.execute(
        sa.insert(workers_t).values(
            id=worker_id,
            account_id=account_id,
            name=f"bond-sync-rig-{index}",
            type="image",
            wallet=wallet,
            models=[MODEL],
            capabilities={},
            maintenance=False,
            first_seen=NOW - timedelta(days=7),
            last_seen=NOW,
            jobs_completed=100,
            den_earned=0,
        ),
    )
    await session.execute(
        sa.insert(references_t).values(
            worker_id=worker_id,
            model=MODEL,
            modality="image",
            account_id=account_id,
            payout_wallet=wallet,
            status="active",
            status_reason="independent quality review",
            bond_contract=DIAMOND if prior_block is not None else None,
            bond_chain_id=8453 if prior_block is not None else None,
            bond_finalized_block=prior_block,
            bond_finalized_block_hash=BLOCK_HASH if prior_block is not None else None,
            bond_facet_address=FACET if prior_block is not None else None,
            bond_facet_runtime_hash=REVIEWED_RUNTIME_HASH if prior_block is not None else None,
            bond_amount_raw=Decimal(500 if prior_block is not None else 0),
            bond_active=prior_block is not None,
            bond_slashed=False,
            bond_verifier_version=VERIFIER if prior_block is not None else None,
            bond_status_reason="active" if prior_block is not None else None,
            bond_verified_at=NOW - timedelta(hours=1) if prior_block is not None else None,
            quality_window_start=NOW - timedelta(days=1),
            quality_window_end=NOW,
            quality_pass_rate=0.99,
            quality_reviewed_at=NOW,
            selection_count=0,
            created=NOW,
            updated=NOW,
        ),
    )
    return worker_id, wallet


def _snapshot(*workers, block=123_456):
    return validator_bonds.FinalizedBondSnapshot(
        chain_id=8453,
        diamond_address=DIAMOND,
        facet_address=FACET,
        facet_runtime_hash=REVIEWED_RUNTIME_HASH,
        finalized_block=block,
        finalized_block_hash=BLOCK_HASH,
        workers={worker.wallet: worker for worker in workers},
    )


@pytest.mark.asyncio
async def test_apply_updates_reviewed_rows_and_fails_missing_wallet_closed(session):
    present_id, present_wallet = await _reference(session, 1)
    missing_id, _ = await _reference(session, 2)
    await session.commit()
    bond = validator_bonds.WorkerBond(
        wallet=present_wallet,
        amount_raw=2_000,
        active=True,
        slashed=False,
        unbonding_at=0,
    )

    result = await validator_bonds.apply_reference_bond_snapshot(
        session,
        snapshot=_snapshot(bond),
        verifier_version=VERIFIER,
        verified_at=NOW,
    )
    await session.commit()

    rows = {
        row.worker_id: row
        for row in (
            await session.execute(sa.select(references_t))
        ).mappings()
    }
    assert result == {
        "reference_rows": 2,
        "updated": 2,
        "inactive": 1,
        "matched_workers": 1,
        "finalized_block": 123_456,
    }
    assert rows[present_id].bond_active is True
    assert int(rows[present_id].bond_amount_raw) == 2_000
    assert rows[present_id].bond_finalized_block_hash == BLOCK_HASH
    assert rows[present_id].bond_facet_address == FACET
    assert rows[present_id].bond_facet_runtime_hash == REVIEWED_RUNTIME_HASH
    assert rows[present_id].bond_status_reason == "active"
    assert rows[missing_id].bond_active is False
    assert int(rows[missing_id].bond_amount_raw) == 0
    assert rows[missing_id].status == "active"
    assert rows[missing_id].quality_pass_rate == pytest.approx(0.99)
    assert rows[missing_id].status_reason == "independent quality review"


@pytest.mark.asyncio
async def test_apply_fails_identity_drift_closed(session):
    worker_id, wallet = await _reference(session, 20)
    await session.execute(
        sa.update(workers_t)
        .where(workers_t.c.id == worker_id)
        .values(wallet="0x" + "9" * 40),
    )
    await session.commit()

    result = await validator_bonds.apply_reference_bond_snapshot(
        session,
        snapshot=_snapshot(
            validator_bonds.WorkerBond(
                wallet=wallet,
                amount_raw=2_000,
                active=True,
                slashed=False,
                unbonding_at=0,
            ),
        ),
        verifier_version=VERIFIER,
        verified_at=NOW,
    )
    await session.commit()

    row = (
        await session.execute(
            sa.select(references_t).where(references_t.c.worker_id == worker_id),
        )
    ).mappings().one()
    assert result["inactive"] == 1
    assert row.bond_active is False
    assert row.bond_status_reason == "identity_mismatch"


@pytest.mark.asyncio
async def test_apply_rejects_unreviewed_verifier_or_runtime(session):
    await _reference(session, 21)
    await session.commit()

    with pytest.raises(validator_bonds.BondSyncError, match="not reviewed"):
        await validator_bonds.apply_reference_bond_snapshot(
            session,
            snapshot=_snapshot(),
            verifier_version="operator-chosen-label",
            verified_at=NOW,
        )
    with pytest.raises(validator_bonds.BondSyncError, match="runtime"):
        await validator_bonds.apply_reference_bond_snapshot(
            session,
            snapshot=validator_bonds.FinalizedBondSnapshot(
                chain_id=8453,
                diamond_address=DIAMOND,
                facet_address=FACET,
                facet_runtime_hash="0x" + "f" * 64,
                finalized_block=123_456,
                finalized_block_hash=BLOCK_HASH,
                workers={},
            ),
            verifier_version=VERIFIER,
            verified_at=NOW,
        )


@pytest.mark.asyncio
async def test_apply_cannot_overwrite_a_newer_finalized_snapshot(session):
    worker_id, wallet = await _reference(session, 3, prior_block=200)
    await session.commit()
    older = validator_bonds.WorkerBond(
        wallet=wallet,
        amount_raw=999,
        active=False,
        slashed=True,
        unbonding_at=0,
    )

    with pytest.raises(validator_bonds.BondSyncError, match="moved backwards"):
        await validator_bonds.apply_reference_bond_snapshot(
            session,
            snapshot=_snapshot(older, block=199),
            verifier_version=VERIFIER,
            verified_at=NOW,
        )
    await session.rollback()

    row = (
        await session.execute(
            sa.select(references_t).where(references_t.c.worker_id == worker_id),
        )
    ).mappings().one()
    assert row.bond_finalized_block == 200
    assert row.bond_active is True
    assert int(row.bond_amount_raw) == 500
    assert row.bond_verifier_version == VERIFIER


@pytest.mark.asyncio
async def test_disabled_sync_does_not_read_rpc():
    called = False

    def reader(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("disabled sync called RPC")

    result = await validator_bonds.sync_reference_bonds_once(
        settings=GridSettings(validator_media_bond_sync_enabled=False),
        snapshot_reader=reader,
    )

    assert result == {"status": "disabled"}
    assert called is False


@pytest.mark.asyncio
async def test_enabled_sync_reads_config_and_commits_atomically(session, monkeypatch):
    worker_id, wallet = await _reference(session, 4)
    await session.commit()
    bond = validator_bonds.WorkerBond(
        wallet=wallet,
        amount_raw=3_000,
        active=True,
        slashed=False,
        unbonding_at=0,
    )
    expected_snapshot = _snapshot(bond)
    observed = {}

    def reader(**kwargs):
        observed.update(kwargs)
        return expected_snapshot

    factory = async_sessionmaker(
        session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def open_session():
        return factory()

    monkeypatch.setattr(validator_bonds, "new_session", open_session)
    result = await validator_bonds.sync_reference_bonds_once(
        settings=GridSettings(
            base_rpc_url=SecretStr("https://rpc.invalid/private-token"),
            validator_media_bond_confirmation_rpc_url=SecretStr(
                "https://rpc-two.invalid/private-token",
            ),
            validator_media_bond_sync_enabled=True,
            validator_media_bond_chain_id=8453,
            validator_media_bond_contract=DIAMOND,
            validator_media_bond_verifier_version=VERIFIER,
            validator_media_bond_max_workers=77,
            validator_media_bond_rpc_timeout_seconds=11,
        ),
        snapshot_reader=reader,
    )

    row = (
        await session.execute(
            sa.select(references_t).where(references_t.c.worker_id == worker_id),
        )
    ).mappings().one()
    assert result == {
        "status": "synced",
        "reference_rows": 1,
        "updated": 1,
        "inactive": 0,
        "matched_workers": 1,
        "finalized_block": 123_456,
    }
    assert observed == {
        "rpc_url": "https://rpc.invalid/private-token",
        "confirmation_rpc_url": "https://rpc-two.invalid/private-token",
        "expected_chain_id": 8453,
        "diamond_address": DIAMOND,
        "expected_facet_runtime_hash": REVIEWED_RUNTIME_HASH,
        "reference_wallets": (wallet,),
        "max_workers": 77,
        "rpc_timeout_seconds": 11,
        "anchor_block": None,
        "anchor_block_hash": None,
    }
    assert row.bond_active is True
    assert int(row.bond_amount_raw) == 3_000
    assert row.bond_verifier_version == VERIFIER
    assert row.status == "active"
    assert row.quality_pass_rate == pytest.approx(0.99)
    state = (await session.execute(sa.select(sync_state_t))).mappings().one()
    assert state.status == "healthy"
    assert state.finalized_block == 123_456
    assert state.finalized_block_hash == BLOCK_HASH
    assert state.facet_runtime_hash == REVIEWED_RUNTIME_HASH


@pytest.mark.asyncio
async def test_sync_skips_when_another_process_holds_the_lock(session, monkeypatch):
    called = False
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)

    async def open_session():
        return factory()

    async def lock_unavailable(_session):
        return False

    def reader(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("contended sync read RPC")

    monkeypatch.setattr(validator_bonds, "new_session", open_session)
    monkeypatch.setattr(validator_bonds, "_try_sync_lock", lock_unavailable)
    result = await validator_bonds.sync_reference_bonds_once(
        settings=GridSettings(
            base_rpc_url=SecretStr("https://rpc.invalid/private-token"),
            validator_media_bond_confirmation_rpc_url=SecretStr(
                "https://rpc-two.invalid/private-token",
            ),
            validator_media_bond_sync_enabled=True,
            validator_media_bond_chain_id=8453,
            validator_media_bond_contract=DIAMOND,
            validator_media_bond_verifier_version=VERIFIER,
        ),
        snapshot_reader=reader,
    )

    assert result == {"status": "skipped", "reason": "another sync is running"}
    assert called is False


@pytest.mark.asyncio
async def test_sync_fault_invalidates_prior_eligibility_and_records_health(session, monkeypatch):
    worker_id, wallet = await _reference(session, 5, prior_block=123_400)
    other_worker_id, _ = await _reference(session, 6, prior_block=123_400)
    await session.execute(
        sa.update(references_t)
        .where(references_t.c.worker_id == other_worker_id)
        .values(bond_contract="0x" + "8" * 40),
    )
    await session.execute(
        sa.insert(sync_state_t).values(
            chain_id=8453,
            bond_contract=DIAMOND,
            verifier_version=VERIFIER,
            facet_address=FACET,
            facet_runtime_hash=REVIEWED_RUNTIME_HASH,
            finalized_block=123_400,
            finalized_block_hash=BLOCK_HASH,
            status="healthy",
            status_reason="quorum_verified",
            consecutive_failures=0,
            last_attempt_at=NOW,
            last_success_at=NOW,
            created=NOW,
            updated=NOW,
        ),
    )
    await session.commit()
    factory = async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)

    async def open_session():
        return factory()

    def failing_reader(**kwargs):
        assert kwargs["anchor_block"] == 123_400
        assert kwargs["anchor_block_hash"] == BLOCK_HASH
        raise RuntimeError("provider detail must not escape")

    monkeypatch.setattr(validator_bonds, "new_session", open_session)
    result = await validator_bonds.sync_reference_bonds_once(
        settings=GridSettings(
            base_rpc_url=SecretStr("https://rpc.invalid/private-token"),
            validator_media_bond_confirmation_rpc_url=SecretStr("https://rpc-two.invalid/private-token"),
            validator_media_bond_sync_enabled=True,
            validator_media_bond_chain_id=8453,
            validator_media_bond_contract=DIAMOND,
            validator_media_bond_verifier_version=VERIFIER,
        ),
        snapshot_reader=failing_reader,
    )

    assert result["status"] == "faulted"
    assert result["reason"] == "bond snapshot read failed"
    assert result["invalidated"] == 1
    row = (
        await session.execute(sa.select(references_t).where(references_t.c.worker_id == worker_id))
    ).mappings().one()
    other_row = (
        await session.execute(
            sa.select(references_t).where(references_t.c.worker_id == other_worker_id),
        )
    ).mappings().one()
    state = (await session.execute(sa.select(sync_state_t))).mappings().one()
    assert row.payout_wallet == wallet
    assert row.bond_active is False
    assert row.bond_verified_at is None
    assert row.bond_status_reason == "sync_faulted"
    assert other_row.bond_active is True
    assert state.status == "faulted"
    assert state.consecutive_failures == 1
    assert state.finalized_block == 123_400

    recovered_snapshot = _snapshot(
        validator_bonds.WorkerBond(
            wallet=wallet,
            amount_raw=4_000,
            active=True,
            slashed=False,
            unbonding_at=0,
        ),
        block=123_500,
    )

    def recovered_reader(**kwargs):
        assert kwargs["anchor_block"] == 123_400
        assert kwargs["anchor_block_hash"] == BLOCK_HASH
        assert kwargs["reference_wallets"] == (wallet,)
        return recovered_snapshot

    recovered = await validator_bonds.sync_reference_bonds_once(
        settings=GridSettings(
            base_rpc_url=SecretStr("https://rpc.invalid/private-token"),
            validator_media_bond_confirmation_rpc_url=SecretStr(
                "https://rpc-two.invalid/private-token",
            ),
            validator_media_bond_sync_enabled=True,
            validator_media_bond_chain_id=8453,
            validator_media_bond_contract=DIAMOND,
            validator_media_bond_verifier_version=VERIFIER,
        ),
        snapshot_reader=recovered_reader,
    )

    recovered_row = (
        await session.execute(
            sa.select(references_t).where(references_t.c.worker_id == worker_id),
        )
    ).mappings().one()
    recovered_state = (await session.execute(sa.select(sync_state_t))).mappings().one()
    assert recovered["status"] == "synced"
    assert recovered_row.bond_active is True
    assert recovered_row.bond_status_reason == "active"
    assert recovered_state.status == "healthy"
    assert recovered_state.consecutive_failures == 0
    assert recovered_state.finalized_block == 123_500

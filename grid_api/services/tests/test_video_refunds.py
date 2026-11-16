# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real SQL refund closure; synthetic Safe/token receipts, no real funds."""

import asyncio
import copy
import importlib.util
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from grid_api import database
from grid_api.services import credits, payment_receipt_claims, video_prepaid, video_refunds
from grid_api.services import video_payment_proofs as rpc
from grid_api.services.tests import test_video_prepaid as support
from grid_api.services.tests import test_video_refund_proofs as proof_support
from grid_api.v2.schema import account_aliases, accounts, credit_ledger, ledger, video_quotes
from grid_api.v2.schema import payment_receipt_claims as claims
from grid_api.v2.schema import video_refunds as refunds

db = support.db
admission = support.admission
registry = support.registry
synthetic_dependencies = support.synthetic_dependencies
source_store = support.source_store
pytestmark = pytest.mark.asyncio
POLICY = {"safe_nonce": 7, "confirmation_tag": "safe", "min_confirmations": 12}


async def due(*, mode="unadmitted", tx=None):
    owner, quote, account = await support.proven(tx=tx)
    payment = await video_prepaid.admit_proven(owner, quote["id"], **support.policy(allow_execution=mode != "unadmitted"))
    if mode != "unadmitted":
        kwargs = {"only_if_unstarted_media": True}
        if mode == "started":
            job, _hold = await support.state(payment["job_id"])
            claimed, _worker, token = await support.claim(job)
            assert claimed == "claimed"
            kwargs = {"media_execution_token": token}
        await credits.release_job(payment["job_id"], **kwargs)
        payment = await video_prepaid.lookup(owner, quote["id"])
    assert payment["refund_due_at"] is not None
    return owner, quote, account, payment


async def settle(owner, quote, prepared, *, one=None, two=None):
    one = one or proof_support.RefundRPC(prepared["intent"])
    two = two or proof_support.RefundRPC(prepared["intent"])
    async with proof_support.support.sources(one, two) as sources:
        return await video_refunds.reconcile(owner, quote["id"], transaction_hash=proof_support._TX, sources=sources)


async def unchanged_economics(owner):
    assert await credits.get_balance(owner) == 0
    async with await database.new_session() as session:
        for table in (credit_ledger, ledger):
            assert await session.scalar(sa.select(sa.func.count()).select_from(table)) == 0


@pytest.mark.parametrize("mode", ["unadmitted", "unstarted", "started"])
async def test_original_payer_exact_nonce_and_receipt_commit_once_without_credit_or_payout(db, admission, mode, monkeypatch):
    owner, quote, account, payment = await due(mode=mode)
    before = copy.deepcopy(payment)
    prepared = await video_refunds.prepare(owner, quote["id"], **POLICY)
    assert prepared["status"] == "prepared" and prepared["transaction_hash"] is None
    assert prepared["intent"]["payer"] == account.address.lower()
    assert prepared["intent"]["amount_micro"] == payment["amount_micro"]
    assert await video_refunds.prepare(owner, quote["id"], **POLICY) == prepared
    monkeypatch.setattr(support.attempt_tests.quotes, "_now", lambda: quote["expires"] + timedelta(days=1))
    result = await settle(owner, quote, prepared)
    assert result["status"] == "refunded" and result["transaction_hash"] == proof_support._TX
    assert await video_prepaid.lookup(owner, quote["id"]) == before
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(refunds)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(claims)) == 2
    one, two = proof_support.RefundRPC(prepared["intent"]), proof_support.RefundRPC(prepared["intent"])
    assert await settle(owner, quote, prepared, one=one, two=two) == result
    assert not one.calls and not two.calls
    assert await video_refunds.prepare(owner, quote["id"], **POLICY) == result
    if mode != "unadmitted":
        assert (await support.state(payment["job_id"]))[1]["actual_micro"] == 0
    await unchanged_economics(owner)


async def test_foreign_missing_held_and_successful_jobs_cannot_prepare_refunds(db, admission):
    owner, quote, _account, _payment = await due()
    foreign = await support.attempt_tests.quote_tests.owner(0)
    for principal, identity in ((foreign, quote["id"]), (owner, uuid4())):
        with pytest.raises(video_refunds.VideoRefundError):
            await video_refunds.prepare(principal, identity, **POLICY)
    held_owner, held_quote, _account = await support.proven(tx="0x" + "b" * 64)
    held = await video_prepaid.admit_proven(held_owner, held_quote["id"], **support.policy())
    with pytest.raises(video_refunds.VideoRefundError):
        await video_refunds.prepare(held_owner, held_quote["id"], **POLICY)
    job, _hold = await support.state(held["job_id"])
    _status, worker, token = await support.claim(job)
    values, media = support.terminal(job, worker)
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_result=media, media_execution_token=token) == "settled"
    with pytest.raises(video_refunds.VideoRefundError):
        await video_refunds.prepare(held_owner, held_quote["id"], **POLICY)
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(refunds)) == 0


@pytest.mark.parametrize("change", [{"safe_nonce": 8}, {"confirmation_tag": "finalized"}, {"min_confirmations": 13}])
async def test_prepared_nonce_and_policy_cannot_change(db, admission, change):
    owner, quote, _account, _payment = await due()
    original = await video_refunds.prepare(owner, quote["id"], **POLICY)
    with pytest.raises(video_refunds.VideoRefundConflict):
        await video_refunds.prepare(owner, quote["id"], **{**POLICY, **change})
    assert await video_refunds.lookup(owner, quote["id"]) == original
    await unchanged_economics(owner)


@pytest.mark.parametrize("failure", ["missing", "wrong-receipt", "unavailable", "claim-conflict"])
async def test_uncertainty_and_receipt_ownership_never_discharge_or_replace_liability(db, admission, failure):
    owner, quote, _account, payment = await due()
    prepared = await video_refunds.prepare(owner, quote["id"], **POLICY)
    one, two = proof_support.RefundRPC(prepared["intent"]), proof_support.RefundRPC(prepared["intent"])
    if failure == "missing":
        one.tx = None
    elif failure == "wrong-receipt":
        one.receipt["logs"].pop(0)
    elif failure == "unavailable":
        one.fault = TimeoutError("private provider detail")
    else:
        async with await database.new_session() as session:
            await payment_receipt_claims.claim_in_session(session, chain_id=prepared["chain_id"], asset=prepared["intent"]["asset"],
                transaction_hash=proof_support._TX, owner_kind="deposit", owner_ref="another-reference", purpose={"schema": "test"})
            await session.commit()
    with pytest.raises((video_refunds.VideoRefundError, rpc.VideoPaymentProofError)):
        await settle(owner, quote, prepared, one=one, two=two)
    assert await video_refunds.lookup(owner, quote["id"]) == prepared
    assert await video_prepaid.lookup(owner, quote["id"]) == payment
    await unchanged_economics(owner)


async def test_real_sql_network_reads_hold_no_account_or_quote_locks(db, admission):
    owner, quote, _account, _payment = await due()
    prepared = await video_refunds.prepare(owner, quote["id"], **POLICY)
    one, two = proof_support.RefundRPC(prepared["intent"]), proof_support.RefundRPC(prepared["intent"])
    async def unlocked(_method, _params):
        async with await database.new_session() as session:
            await session.execute(sa.select(accounts.c.id).where(accounts.c.id == owner).with_for_update(nowait=True))
            await session.execute(sa.select(video_quotes.c.id).where(video_quotes.c.id == quote["id"]).with_for_update(nowait=True))
    one.before_read = two.before_read = unlocked
    assert (await settle(owner, quote, prepared, one=one, two=two))["status"] == "refunded"
    await unchanged_economics(owner)


async def test_account_alias_and_changed_wallet_do_not_redirect_the_original_refund(db, admission):
    owner, quote, payer, _payment = await due()
    prepared = await video_refunds.prepare(owner, quote["id"], **POLICY)
    target = await support.attempt_tests.quote_tests.owner(0)
    async with await database.new_session() as session:
        await session.execute(account_aliases.insert().values(source_account_id=owner,
            canonical_account_id=target, merge_ref="synthetic verified merge"))
        await session.execute(accounts.update().where(accounts.c.id == target).values(payout_wallet="0x" + "3" * 40))
        await session.commit()
    assert await video_refunds.lookup(target, quote["id"]) == prepared
    assert prepared["intent"]["payer"] == payer.address.lower()
    assert (await settle(target, quote, prepared))["status"] == "refunded"
    await unchanged_economics(target)


@pytest.mark.parametrize("damage", ["payer", "amount", "original_proof_hash", "original_claim_key"])
async def test_self_consistent_mutated_intent_cannot_override_owned_payment(db, admission, damage):
    owner, quote, _payer, _payment = await due()
    prepared = await video_refunds.prepare(owner, quote["id"], **POLICY)
    altered = copy.deepcopy(prepared["intent"])
    if damage == "payer":
        altered["payer"] = "0x" + "3" * 40
    elif damage == "amount":
        altered["amount_micro"] -= 1
    else:
        altered[damage] = "f" * 64
    altered["safe_transaction_hash"] = video_refunds.proofs.transaction_hash(altered)
    async with await database.new_session() as session:
        await session.execute(refunds.update().values(intent=altered, intent_hash=video_refunds.quotes._hash(altered),
            safe_transaction_hash=altered["safe_transaction_hash"]))
        await session.commit()
    with pytest.raises(video_refunds.VideoRefundError):
        await video_refunds.lookup(owner, quote["id"])
    await unchanged_economics(owner)


@pytest.mark.parametrize("phase", ["prepare", "proof"])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_commit_reply_loss_retains_original_intent_or_atomic_refund_claim(db, admission, monkeypatch, phase, after_commit):
    owner, quote, _account, _payment = await due()
    prepared = await video_refunds.prepare(owner, quote["id"], **POLICY) if phase == "proof" else None
    factory = video_refunds.new_session
    async def uncertain():
        session = await factory()
        commit = session.commit
        async def fail():
            if after_commit:
                await commit()
            raise RuntimeError("private refund SQL must not escape")
        session.commit = fail
        return session
    monkeypatch.setattr(video_refunds, "new_session", uncertain)
    with pytest.raises(video_refunds.VideoRefundError) as error:
        if phase == "prepare":
            await video_refunds.prepare(owner, quote["id"], **POLICY)
        else:
            await settle(owner, quote, prepared)
    assert "private refund SQL" not in str(error.value)
    monkeypatch.setattr(video_refunds, "new_session", factory)
    retained = await video_refunds.lookup(owner, quote["id"])
    if phase == "prepare":
        assert (retained is not None) == after_commit
        prepared = await video_refunds.prepare(owner, quote["id"], **POLICY)
    else:
        assert retained["status"] == ("refunded" if after_commit else "prepared")
    result = await settle(owner, quote, prepared)
    assert result["status"] == "refunded"
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(claims)) == 2
    await unchanged_economics(owner)


async def test_postgres_prepare_and_refund_races_have_one_intent_claim_and_closure(db, admission):
    if db[1] != "postgres":
        pytest.skip("real PostgreSQL concurrency required")
    owner, quote, _account, _payment = await due()
    results = await asyncio.wait_for(asyncio.gather(*(video_refunds.prepare(owner, quote["id"], **POLICY) for _ in range(25))), 30)
    assert all(row == results[0] for row in results)
    prepared = results[0]
    one, two = proof_support.RefundRPC(prepared["intent"]), proof_support.RefundRPC(prepared["intent"])
    async with proof_support.support.sources(one, two) as sources:
        async def reconcile():
            return await video_refunds.reconcile(owner, quote["id"], transaction_hash=proof_support._TX, sources=sources)
        results = await asyncio.wait_for(asyncio.gather(*(reconcile() for _ in range(25)), return_exceptions=True), 30)
        winners = [row for row in results if isinstance(row, dict)]
        assert winners and all(row == winners[0] for row in winners)
        assert all(isinstance(row, (dict, rpc.VideoPaymentUnavailable)) for row in results)
        assert await reconcile() == winners[0]
    await unchanged_economics(owner)


async def test_postgres_same_safe_nonce_cannot_bind_two_refund_obligations(db, admission):
    if db[1] != "postgres":
        pytest.skip("real PostgreSQL concurrency required")
    first = await due()
    second = await due(tx="0x" + "b" * 64)
    results = await asyncio.gather(*(video_refunds.prepare(owner, quote["id"], **POLICY)
                                   for owner, quote, _account, _payment in (first, second)), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, video_refunds.VideoRefundConflict) for result in results) == 1


async def test_migration_roundtrip_matches_schema_and_preserves_every_intent(db, admission):
    spec = importlib.util.spec_from_file_location("refund_migration", Path(__file__).resolve().parents[3]
        / "alembic/versions/0052_video_safe_refunds.py")
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.down_revision == "0051"
    def apply(connection):
        with Operations.context(MigrationContext.configure(connection)):
            revision.downgrade()
            revision.upgrade()
        inspector = sa.inspect(connection)
        assert {column["name"]: column["nullable"] for column in inspector.get_columns(refunds.name)} == {
            column.name: column.nullable for column in refunds.columns}
        for kind, inspect_fn in ((sa.CheckConstraint, inspector.get_check_constraints),
                                 (sa.UniqueConstraint, inspector.get_unique_constraints)):
            assert {item["name"] for item in inspect_fn(refunds.name)} == {
                constraint.name for constraint in refunds.constraints if isinstance(constraint, kind)}
    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(apply)
    owner, quote, _account, _payment = await due()
    prepared = await video_refunds.prepare(owner, quote["id"], **POLICY)
    def refuse(connection):
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="retain every"):
            revision.downgrade()
    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(refuse)
    assert await video_refunds.lookup(owner, quote["id"]) == prepared
    # SQL NULL must not satisfy the 'refunded' check via three-valued logic.
    async with await database.new_session() as session:
        with pytest.raises(IntegrityError):
            await session.execute(refunds.update().values(status="refunded", refunded_at=prepared["created"], proof={"invalid": True}))
        await session.rollback()

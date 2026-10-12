# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real SQL funded handoff/terminal; synthetic chain/storage, no real USDC."""

import asyncio
import copy
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from grid_api import database
from grid_api.services import credits, job_queue, media_jobs, media_source_retention, video_prepaid
from grid_api.services import video_payment_attempts as attempts
from grid_api.services.settlement import aggregate
from grid_api.services.tests import test_video_payment_attempts as attempt_tests
from grid_api.v2.schema import (
    account_aliases,
    credit_ledger,
    ledger,
    payment_receipt_claims,
    reservations,
    video_payment_attempts,
    video_prepaid_payments,
    workers,
)
from grid_api.v2.schema import media_jobs as job_table

db = attempt_tests.db
admission = attempt_tests.admission
registry = attempt_tests.registry
mp4 = attempt_tests.mp4
synthetic_dependencies = attempt_tests.synthetic_dependencies
source_store = attempt_tests.source_store
redis = attempt_tests.quote_tests.journal_tests.redis
pytestmark = pytest.mark.asyncio
_SUBMIT = job_queue.submit_job


def policy(**changes):
    return {"allow_execution": True, "admission_until": datetime.now(UTC) + timedelta(minutes=1), **changes}


async def proven(*, mode="generated", video=None, owner=None, tx=None):
    if owner is None:
        owner, quote, account, payload = await attempt_tests.saved(mode=mode, video=video)
    else:
        inputs = await attempt_tests.videos.validate_video(attempt_tests.plan_tests.request(mode, video=video), {}, async_submission=True)
        plan = attempt_tests.video_plans.compile_video(inputs)
        key = str(uuid4())
        prepared = attempt_tests.quotes.prepare(account_id=owner, idempotency_key=key, request_hash=attempt_tests.quote_tests.INTENT,
            plan=plan, max_cost_micro=100_000, server_max_micro=100_000, network="eip155:84532", pay_to=attempt_tests.quote_tests.PAY_TO)
        quote, _new = await attempt_tests.quotes.persist(prepared, idempotency_key=key, **attempt_tests.quote_tests.LIMITS)
        await attempt_tests.staging.stage(owner, quote["id"], plan, admission_until=policy()["admission_until"])
        account = attempt_tests.Account.create()
        payload = attempt_tests.proof_tests.binding_tests.sign(quote, account)
    await attempt_tests.begin(owner, quote, account, payload)
    tx = tx or attempt_tests.proof_tests._TX
    await attempts.report(owner, quote["id"], tx)
    one = attempt_tests.proof_tests.RPCFixture(quote, account)
    two = attempt_tests.proof_tests.RPCFixture(quote, account)
    for fixture in (one, two):
        fixture.tx["hash"] = fixture.receipt["transactionHash"] = tx
        for event in fixture.receipt["logs"]:
            event["transactionHash"] = tx
    async with attempt_tests.proof_tests.sources(one, two) as sources:
        proof = await attempts.reconcile(owner, quote["id"], sources=sources)
    assert proof["status"] == "proven"
    return owner, quote, account


async def counts():
    async with await database.new_session() as s:
        return tuple([await s.scalar(sa.select(sa.func.count()).select_from(t))
                      for t in (video_prepaid_payments, reservations, job_table, credit_ledger, ledger)])


async def state(job_id):
    async with await database.new_session() as s:
        job = (await s.execute(sa.select(job_table).where(job_table.c.job_id == job_id))).mappings().one()
        hold = (await s.execute(sa.select(reservations).where(reservations.c.job_id == job_id))).mappings().one()
        return dict(job), dict(hold)


async def claim(job, *, worker=None, token=None):
    worker, token = worker or uuid4(), token or str(uuid4())
    status = await media_jobs.claim_execution(job_id=job["job_id"], payload=job["payload"], route_models=job["route_models"],
        preferred_worker=job["preferred_worker"], selected_model=job["route_models"][0], worker_id=worker, job_type="video",
        execution_token=token)
    return status, worker, token


def terminal(job, worker):
    values = {"job_id": job["job_id"], "worker_id": str(worker), "wallet": "", "model": job["route_models"][0],
              "job_type": "video", "den": 1.0, "output_units": job["payload"]["frames"], "prompt_hash": "b" * 64, "result_hash": "c" * 64}
    result = {"media": [{"url": "https://assets.example/frozen.mp4", "key": "video/frozen.mp4", "sha256": "a" * 64, "seed": 0}],
              "model": values["model"], "worker": "test-worker", "gen_time": 1.0, "recipe_root": job["payload"]["recipe_root"]}
    return values, result


@pytest.mark.parametrize("mode", ["generated", "audio", "video"])
async def test_proof_hold_outbox_and_exact_input_authority_commit_once_without_credit_draw(db, admission, mp4, mode, monkeypatch):
    owner, quote, account = await proven(mode=mode, video=mp4)
    monkeypatch.setattr(credits, "authorize_media", AsyncMock(side_effect=AssertionError("no credit draw")))
    first = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    assert first["admitted_at"] is not None and first["refund_due_at"] is None
    assert first["job_id"] == str(quote["job_id"])
    assert await counts() == (1, 1, 1, 0, 0)
    job, hold = await state(first["job_id"])
    assert hold["billing_source"] == "video_prepaid" and hold["external_payer"] == account.address.lower()
    assert hold["reserved_micro"] == first["amount_micro"] == quote["amount_micro"]
    assert hold["free_micro"] == hold["promo_micro"] == 0
    # The journal adopts the original allocated private keys, not URL guesses.
    _payload, authority = await attempt_tests.staging.dispatch_payload(owner, quote["id"])
    assert job["source_objects"] == authority
    original = copy.deepcopy(job)
    repeated = await video_prepaid.admit_proven(owner, quote["id"], **policy(allow_execution=False))
    assert repeated == first and (await state(first["job_id"]))[0] == original
    assert await credits.get_balance(owner) == 0 and await counts() == (1, 1, 1, 0, 0)


@pytest.mark.parametrize("phase", ["missing", "settling", "reported", "unowned", "reviewed"])
async def test_unproven_or_unowned_receipt_never_admits_or_creates_a_refund(db, admission, phase):
    if phase in {"unowned", "reviewed"}:
        owner, quote, _account = await proven()
        async with await database.new_session() as s:
            if phase == "unowned":
                await s.execute(payment_receipt_claims.delete())
            else:
                await s.execute(video_payment_attempts.update().values(status="manual_review", review_code="receipt_claim_conflict"))
            await s.commit()
    else:
        owner, quote, account, payload = await attempt_tests.saved()
        if phase != "missing":
            await attempt_tests.begin(owner, quote, account, payload)
        if phase == "reported":
            await attempts.report(owner, quote["id"], attempt_tests.proof_tests._TX)
    with pytest.raises(video_prepaid.VideoPrepaidError):
        await video_prepaid.admit_proven(owner, quote["id"], **policy())
    assert await counts() == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("reason", ["quote_expired", "admission_closed"])
async def test_late_proof_or_closed_admission_records_exact_unsent_refund_without_a_job(db, admission, monkeypatch, reason):
    owner, quote, _account = await proven()
    if reason == "quote_expired":
        monkeypatch.setattr(video_prepaid, "_now", lambda: quote["expires"] + timedelta(seconds=1))
    original = await video_prepaid.admit_proven(owner, quote["id"], **policy(allow_execution=False))
    assert original["refund_reason"] == reason and original["refund_due_at"] is not None and original["admitted_at"] is None
    assert await counts() == (1, 0, 0, 0, 0)
    assert await video_prepaid.admit_proven(owner, quote["id"], **policy()) == original
    assert await counts() == (1, 0, 0, 0, 0)


@pytest.mark.parametrize("after_commit", [False, True])
async def test_commit_failure_or_reply_loss_preserves_atomicity_and_same_job_recovery(db, admission, monkeypatch, after_commit):
    owner, quote, _account = await proven()
    factory = database.new_session
    async def uncertain_session():
        s = await factory()
        commit = s.commit
        async def fail():
            if after_commit:
                await commit()
            raise RuntimeError("private SQL and source URL")
        s.commit = fail
        return s
    monkeypatch.setattr(video_prepaid, "new_session", uncertain_session)
    with pytest.raises(video_prepaid.VideoPrepaidError) as error:
        await video_prepaid.admit_proven(owner, quote["id"], **policy())
    assert "private SQL" not in str(error.value)
    assert await counts() == ((1, 1, 1, 0, 0) if after_commit else (0, 0, 0, 0, 0))
    monkeypatch.setattr(video_prepaid, "new_session", factory)
    recovered = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    assert recovered["job_id"] == str(quote["job_id"]) and await counts() == (1, 1, 1, 0, 0)


async def test_original_execution_fence_success_result_and_duplicate_survive_admission_rollback(db, admission, monkeypatch):
    owner, quote, _account = await proven()
    payment = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    job, _hold = await state(payment["job_id"])
    accepted, worker, token = await claim(job)
    assert accepted == "claimed" and (await claim(job))[0] == "duplicate"
    values, result = terminal(job, worker)
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_result=result, media_execution_token=token) == "settled"
    assert await credits.record_and_settle(
        ledger_values=values, exact=True, media_result=result, media_execution_token=token,
    ) == "duplicate"
    recovered = await media_jobs.recover(owner, payment["job_id"])
    assert recovered["state"] == "completed" and recovered["result"] == result and recovered["actual_micro"] == payment["amount_micro"]
    assert (await video_prepaid.admit_proven(owner, quote["id"], **policy(allow_execution=False)))["refund_due_at"] is None
    await credits.release_job(payment["job_id"])
    assert await counts() == (1, 1, 1, 0, 1) and await credits.get_balance(owner) == 0
    assert (await video_prepaid.admit_proven(owner, quote["id"], **policy()))["refund_due_at"] is None


@pytest.mark.parametrize("started", [False, True])
async def test_failed_job_owes_exact_usdc_once_never_credit_and_late_success_cannot_pay(db, admission, monkeypatch, started):
    owner, quote, _account = await proven()
    payment = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    job, _hold = await state(payment["job_id"])
    kwargs = {}
    worker, token = uuid4(), str(uuid4())
    if started:
        assert (await claim(job, worker=worker, token=token))[0] == "claimed"
        kwargs["media_execution_token"] = token
    else:
        kwargs["only_if_unstarted_media"] = True
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    await credits.release_job(payment["job_id"], **kwargs)
    closed = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    assert closed["refund_reason"] == "execution_failed" and closed["refund_due_at"] is not None
    assert (await state(payment["job_id"]))[1]["actual_micro"] == 0
    await credits.release_job(payment["job_id"], **kwargs)
    assert await video_prepaid.admit_proven(owner, quote["id"], **policy()) == closed
    assert await counts() == (1, 1, 1, 0, 0) and await credits.get_balance(owner) == 0
    if started:
        values, result = terminal(job, worker)
        assert await credits.record_and_settle(
            ledger_values=values, exact=True, media_result=result, media_execution_token=token,
        ) == "stale_no_payout"
        assert await counts() == (1, 1, 1, 0, 0)


async def test_many_real_postgres_callers_admit_one_hold_and_outbox(db, admission):
    if db[1] != "postgres":
        pytest.skip("PostgreSQL is the concurrent money authority")
    owner, quote, _account = await proven()
    results = await asyncio.gather(*(video_prepaid.admit_proven(owner, quote["id"], **policy()) for _ in range(25)))
    assert all(row == results[0] for row in results)
    assert await counts() == (1, 1, 1, 0, 0)


async def test_capacity_is_shared_with_existing_jobs_and_received_money_becomes_refund_due(db, admission):
    owner, quote, _account = await proven()
    await video_prepaid.admit_proven(owner, quote["id"], **policy(concurrency_limit=1))
    _owner, other, _other_account = await proven(owner=owner, tx="0x" + "f" * 64)
    closed = await video_prepaid.admit_proven(owner, other["id"], **policy(concurrency_limit=1))
    assert closed["refund_reason"] == "capacity_exceeded" and closed["admitted_at"] is None
    assert await counts() == (2, 1, 1, 0, 0)
    await credits.release_job(str(quote["job_id"]))
    assert await video_prepaid.admit_proven(owner, other["id"], **policy()) == closed
    assert await counts() == (2, 1, 1, 0, 0)


async def test_owner_only_read_and_proved_alias_recover_without_new_admission(db, admission, monkeypatch):
    owner, quote, _account = await proven()
    assert await video_prepaid.lookup(owner, quote["id"]) is None
    initial = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    target = await attempt_tests.quote_tests.owner(0)
    with pytest.raises(video_prepaid.VideoPrepaidError):
        await video_prepaid.lookup(target, quote["id"])
    with pytest.raises(video_prepaid.VideoPrepaidError):
        await video_prepaid.lookup(target, uuid4())
    async with await database.new_session() as s:
        await s.execute(account_aliases.insert().values(source_account_id=owner, canonical_account_id=target, merge_ref="synthetic proof"))
        await s.commit()
    monkeypatch.setattr(attempt_tests.staging, "dispatch_payload", AsyncMock(side_effect=AssertionError("GET cannot presign")))
    monkeypatch.setattr(video_prepaid, "_now", lambda: quote["expires"] + timedelta(days=1))
    assert await video_prepaid.lookup(target, quote["id"]) == initial
    assert await counts() == (1, 1, 1, 0, 0)


async def test_paid_job_private_source_cleanup_follows_existing_financial_closure(db, admission, monkeypatch, source_store):
    owner, quote, _account = await proven(mode="audio")
    payment = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    job, _hold = await state(payment["job_id"])
    assert job["source_objects"] is not None
    assert (await media_source_retention.sweep())["deleted"] == 0
    await credits.release_job(payment["job_id"])
    assert (await media_source_retention.sweep())["deleted"] == 1
    assert await counts() == (1, 1, 1, 0, 0)


async def test_real_redis_reply_loss_redelivery_still_has_one_prepaid_execution(db, admission, redis, monkeypatch):
    owner, quote, _account = await proven()
    payment = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    submit = _SUBMIT
    async def lost(*args, **kwargs):
        await submit(*args, **kwargs)
        raise TimeoutError("synthetic lost queue reply")
    monkeypatch.setattr(job_queue, "submit_job", lost)
    with pytest.raises(TimeoutError):
        await media_jobs.dispatch_one(payment["job_id"])
    async with await database.new_session() as s:
        await s.execute(job_table.update().values(dispatch_deadline=datetime.now(UTC) - timedelta(seconds=1)))
        await s.commit()
    monkeypatch.setattr(job_queue, "submit_job", submit)
    assert await media_jobs.dispatch_one(payment["job_id"])
    assert await redis.xlen(job_queue.MEDIA_STREAM_KEY) == 2
    outcomes = []
    for _index in range(2):
        delivery = await job_queue.pop_job(str(uuid4()), timeout_ms=1, job_types=["video"])
        outcomes.append(await media_jobs.claim_execution(job_id=delivery["job_id"], payload=delivery["payload"],
            route_models=delivery["models"], preferred_worker=delivery["preferred_worker"], selected_model=delivery["models"][0],
            worker_id=uuid4(), job_type="video", execution_token=str(uuid4())))
    assert outcomes == ["claimed", "duplicate"] and await counts() == (1, 1, 1, 0, 0)


@pytest.mark.parametrize("damage", ["amount", "source", "graph", "claim", "proof", "missing_job"])
async def test_changed_funding_or_outbox_cannot_dispatch_or_replay(db, admission, damage):
    owner, quote, _account = await proven(mode="audio")
    payment = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    job, _hold = await state(payment["job_id"])
    async with await database.new_session() as s:
        if damage == "amount":
            await s.execute(reservations.update().values(reserved_micro=1))
        elif damage == "source":
            authority = copy.deepcopy(job["source_objects"])
            authority["keys"] = ["source/" + "f" * 32 + ".wav"]
            await s.execute(job_table.update().values(source_objects=authority))
        elif damage == "graph":
            payload = copy.deepcopy(job["payload"])
            payload["prompt"] = "changed paid request"
            await s.execute(job_table.update().values(payload=payload, payload_hash=media_jobs._payload_hash(
                payload, job["route_models"], job["model"], job["preferred_worker"])))
        elif damage == "claim":
            from grid_api.services import payment_receipt_claims as claims
            purpose = {"other": "economic obligation"}
            await s.execute(payment_receipt_claims.update().values(purpose=purpose, purpose_hash=claims._digest(purpose)))
        elif damage == "proof":
            await s.execute(video_payment_attempts.update().values(proof_hash="e" * 64))
        else:
            await s.execute(job_table.delete())
        await s.commit()
    if damage == "missing_job":
        assert await media_jobs.dispatch_one(payment["job_id"]) is False
    else:
        with pytest.raises((video_prepaid.VideoPrepaidError, media_jobs.MediaJobError, attempts.VideoAttemptError)):
            await media_jobs.dispatch_one(payment["job_id"])
    with pytest.raises(video_prepaid.VideoPrepaidError):
        await video_prepaid.admit_proven(owner, quote["id"], **policy())
    assert await credits.get_balance(owner) == 0


async def test_stale_hold_sweep_creates_liability_not_credits(db, admission):
    owner, quote, _account = await proven()
    await video_prepaid.admit_proven(owner, quote["id"], **policy())
    async with await database.new_session() as s:
        await s.execute(reservations.update().values(created=datetime.now(UTC) - timedelta(hours=2)))
        await s.commit()
    assert await credits.sweep_stale_reservations() == 1
    assert (await video_prepaid.admit_proven(owner, quote["id"], **policy()))["refund_reason"] == "execution_failed"
    assert await counts() == (1, 1, 1, 0, 0)


async def test_success_without_verified_result_and_legacy_exact_settle_cannot_charge(db, admission):
    owner, quote, _account = await proven()
    payment = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    job, _hold = await state(payment["job_id"])
    _claimed, worker, token = await claim(job)
    values, _result = terminal(job, worker)
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_execution_token=token) == "error"
    await credits.settle_exact(payment["job_id"])
    assert (await state(payment["job_id"]))[1]["status"] == "held" and await counts() == (1, 1, 1, 0, 0)


@pytest.mark.parametrize("damage", [None, "receipt_owner", "refund_due", "proof_hash", "amount", "no_result"])
async def test_only_exact_completed_receipt_backed_video_supplies_worker_rewards(db, admission, monkeypatch, damage):
    owner, quote, _account = await proven()
    payment = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    job, _hold = await state(payment["job_id"])
    _claimed, worker, token = await claim(job)
    worker_account = await attempt_tests.quote_tests.owner(0)
    async with await database.new_session() as s:
        await s.execute(workers.insert().values(id=worker, account_id=worker_account, name="synthetic prepaid", type="media"))
        await s.commit()
    values, result = terminal(job, worker)
    assert await credits.record_and_settle(
        ledger_values=values, exact=True, media_result=result, media_execution_token=token,
    ) == "settled"
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr(aggregate, "get_settings", lambda: SimpleNamespace(worker_rewards_paid_only_since=cutoff))
    async with await database.new_session() as s:
        if damage == "receipt_owner":
            await s.execute(payment_receipt_claims.update().values(owner_ref="another-obligation"))
        elif damage == "refund_due":
            await s.execute(video_prepaid_payments.update().values(refund_due_at=datetime.now(UTC), refund_reason="execution_failed"))
        elif damage == "proof_hash":
            await s.execute(video_prepaid_payments.update().values(proof_hash="d" * 64))
        elif damage == "amount":
            await s.execute(reservations.update().values(actual_micro=1))
        elif damage == "no_result":
            await s.execute(reservations.update().values(media_result=None))
        await s.commit()
    rows, backing = await aggregate.funded_work_by_account(cutoff, datetime.now(UTC) + timedelta(hours=1))
    assert backing == ({} if damage else {str(worker_account): payment["amount_micro"]})
    assert rows == ([] if damage else [{"account_id": str(worker_account), "den": 1.0, "smollm_den": 0.0, "payout_address": None}])
    assert await aggregate.total_den_in_window(cutoff, datetime.now(UTC) + timedelta(hours=1)) == (0 if damage else 1)


async def test_expiry_during_insert_rolls_back_job_and_records_only_refund_due(db, admission, monkeypatch):
    owner, quote, _account = await proven()
    insert = media_jobs.insert_in_session
    async def expire(*args, **kwargs):
        await insert(*args, **kwargs)
        monkeypatch.setattr(video_prepaid, "_now", lambda: quote["expires"] + timedelta(seconds=1))
    monkeypatch.setattr(media_jobs, "insert_in_session", expire)
    outcome = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    assert outcome["refund_reason"] == "quote_expired" and outcome["admitted_at"] is None
    assert await counts() == (1, 0, 0, 0, 0)


async def test_0051_empty_roundtrip_and_nonempty_downgrade_refusal(db, admission):
    spec = importlib.util.spec_from_file_location("prepaid_migration", Path(__file__).resolve().parents[3]
        / "alembic/versions/0051_video_prepaid_admission.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory, _backend = db
    async with factory().bind.begin() as connection:
        def roundtrip(sync):
            with Operations.context(MigrationContext.configure(sync)):
                module.downgrade()
                module.upgrade()
        await connection.run_sync(roundtrip)
    owner, quote, _account = await proven()
    await video_prepaid.admit_proven(owner, quote["id"], **policy(allow_execution=False))
    async with factory().bind.begin() as connection:
        def refuse(sync):
            with Operations.context(MigrationContext.configure(sync)):
                with pytest.raises(RuntimeError, match="retain prepaid"):
                    module.downgrade()
        await connection.run_sync(refuse)
    assert await counts() == (1, 0, 0, 0, 0)


@pytest.mark.parametrize("after_commit", [False, True])
@pytest.mark.parametrize("success", [False, True])
async def test_terminal_commit_error_never_leaves_half_a_charge_or_refund(db, admission, monkeypatch, after_commit, success):
    owner, quote, _account = await proven()
    payment = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    job, _hold = await state(payment["job_id"])
    _accepted, worker, token = await claim(job)
    values, result = terminal(job, worker)
    factory = database.new_session
    async def uncertain_session():
        session = await factory()
        commit = session.commit
        async def fail():
            if after_commit:
                await commit()
            raise RuntimeError("private terminal SQL")
        session.commit = fail
        return session
    monkeypatch.setattr(credits, "new_session", uncertain_session)
    if success:
        assert await credits.record_and_settle(
            ledger_values=values, exact=True, media_result=result, media_execution_token=token,
        ) == "error"
    else:
        assert await credits.release_job(payment["job_id"], media_execution_token=token) is False
    monkeypatch.setattr(credits, "new_session", factory)
    stored = await video_prepaid.lookup(owner, quote["id"])
    _job, hold = await state(payment["job_id"])
    assert hold["status"] == ("settled" if after_commit else "held")
    assert (stored["refund_due_at"] is not None) == (after_commit and not success)
    assert await counts() == (1, 1, 1, 0, 1 if after_commit and success else 0)
    if success:
        assert await credits.record_and_settle(
            ledger_values=values, exact=True, media_result=result, media_execution_token=token,
        ) == ("duplicate" if after_commit else "settled")
    else:
        assert await credits.release_job(payment["job_id"], media_execution_token=token) is (not after_commit)
    assert await credits.get_balance(owner) == 0


async def test_new_quote_freezes_checked_worker_affinity_and_admission_uses_it(db, admission):
    owner = await attempt_tests.quote_tests.owner(0)
    inputs = await attempt_tests.videos.validate_video(attempt_tests.plan_tests.request("generated"), {}, async_submission=True)
    plan = attempt_tests.video_plans.compile_video(inputs)
    key = str(uuid4())
    prepared = attempt_tests.quotes.prepare(account_id=owner, idempotency_key=key, request_hash=attempt_tests.quote_tests.INTENT,
        plan=plan, max_cost_micro=100_000, server_max_micro=100_000, network="eip155:84532", pay_to=attempt_tests.quote_tests.PAY_TO,
        preferred_worker="synthetic-owned-worker")
    quote, _new = await attempt_tests.quotes.persist(prepared, idempotency_key=key, **attempt_tests.quote_tests.LIMITS)
    await attempt_tests.staging.stage(owner, quote["id"], plan, admission_until=policy()["admission_until"])
    account = attempt_tests.Account.create()
    payload = attempt_tests.proof_tests.binding_tests.sign(quote, account)
    await attempt_tests.begin(owner, quote, account, payload)
    await attempts.report(owner, quote["id"], attempt_tests.proof_tests._TX)
    one, two = (attempt_tests.proof_tests.RPCFixture(quote, account), attempt_tests.proof_tests.RPCFixture(quote, account))
    async with attempt_tests.proof_tests.sources(one, two) as sources:
        assert (await attempts.reconcile(owner, quote["id"], sources=sources))["status"] == "proven"
    payment = await video_prepaid.admit_proven(owner, quote["id"], **policy())
    assert (await state(payment["job_id"]))[0]["preferred_worker"] == "synthetic-owned-worker"

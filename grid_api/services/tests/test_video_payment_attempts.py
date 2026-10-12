# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real SQL attempt lifecycle; fixture RPC, no actual payment or admission."""

import asyncio
import copy
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from eth_account import Account

from alembic.migration import MigrationContext
from alembic.operations import Operations
from grid_api import database
from grid_api.routers import videos
from grid_api.services import video_eip3009 as bindings
from grid_api.services import video_payment_attempts as journal
from grid_api.services import video_payment_proofs as proofs
from grid_api.services import video_plans
from grid_api.services import video_quote_staging as staging
from grid_api.services import video_quotes as quotes
from grid_api.services.tests import test_video_payment_proofs as proof_tests
from grid_api.services.tests import test_video_payment_recovery as search_tests
from grid_api.services.tests import test_video_plans as plan_tests
from grid_api.services.tests import test_video_quotes as quote_tests
from grid_api.services.tests import video_staging_support as stage_support
from grid_api.v2.schema import account_aliases, accounts, metadata
from grid_api.v2.schema import video_payment_attempts as attempts

db = quote_tests.db
admission = quote_tests.admission
registry = quote_tests.registry
mp4 = quote_tests.mp4
synthetic_dependencies = quote_tests.synthetic_dependencies
source_store = stage_support.source_store
pytestmark = pytest.mark.asyncio
POLICY = {"confirmation_tag": "safe", "min_confirmations": 12}


async def saved(*, mode="generated", video=None):
    owner = await quote_tests.owner(0)
    inputs = await videos.validate_video(plan_tests.request(mode, video=video), {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    key = str(uuid4())
    prepared = quotes.prepare(account_id=owner, idempotency_key=key, request_hash=quote_tests.INTENT, plan=plan,
        max_cost_micro=100_000, server_max_micro=100_000, network="eip155:84532", pay_to=quote_tests.PAY_TO)
    row, _new = await quotes.persist(prepared, idempotency_key=key, **quote_tests.LIMITS)
    await staging.stage(owner, row["id"], plan, admission_until=datetime.now(UTC) + timedelta(minutes=1))
    account = Account.create()
    payload = proof_tests.binding_tests.sign(row, account)
    return owner, row, account, payload


async def begin(owner, row, account, payload, **overrides):
    return await journal.begin(owner, row["id"], payload, expected_payer=account.address,
        admission_until=overrides.pop("admission_until", datetime.now(UTC) + timedelta(minutes=1)), **{**POLICY, **overrides})


async def no_economic_effects():
    async with await database.new_session() as session:
        for name in ("grid_reservations", "grid_media_jobs", "grid_ledger", "grid_credit_ledger", "grid_x402_payments", "grid_deposits"):
            assert await session.scalar(sa.select(sa.func.count()).select_from(metadata.tables[name])) == 0


async def test_lost_reply_nonce_recovery_after_expiry_proves_original_without_send_permission(db, admission, monkeypatch):
    owner, row, account, payload = await saved()
    original, winner = await begin(owner, row, account, payload)
    assert winner
    first, second = search_tests.SearchRPC(row, account), search_tests.SearchRPC(row, account)
    monkeypatch.setattr(bindings, "_now", lambda: quotes._aware(row["expires"]) + timedelta(days=1))
    async with proof_tests.sources(first, second) as sources:
        result = await journal.recover_lost_transaction(owner, row["id"], sources=sources, from_block=80, to_block=110)
        assert result["status"] == "proven" and result["transaction_hash"] == proof_tests._TX
        assert result["payload_hash"] == original["payload_hash"]
        first.calls.clear()
        second.calls.clear()
        repeat = await journal.recover_lost_transaction(owner, row["id"], sources=sources, from_block=80, to_block=110)
        assert repeat == result and not first.calls and not second.calls
    repeated, winner = await begin(owner, row, account, payload)
    assert repeated == result and not winner
    await no_economic_effects()


@pytest.mark.parametrize("failure", ["missing", "invalid-receipt", "unavailable"])
async def test_uncertain_nonce_recovery_preserves_original_unreported_attempt(db, admission, failure):
    owner, row, account, payload = await saved()
    original, _winner = await begin(owner, row, account, payload)
    first, second = search_tests.SearchRPC(row, account), search_tests.SearchRPC(row, account)
    if failure == "missing":
        first.logs = second.logs = []
    elif failure == "invalid-receipt":
        first.receipt["logs"].pop()
    else:
        first.fault = TimeoutError("private-provider-detail")
    async with proof_tests.sources(first, second) as sources:
        with pytest.raises(proofs.VideoPaymentProofError):
            await journal.recover_lost_transaction(owner, row["id"], sources=sources, from_block=80, to_block=110)
    assert await journal.lookup(owner, row["id"]) == original
    await no_economic_effects()


async def test_lost_transaction_foreign_or_unattempted_quote_does_no_rpc(db, admission):
    owner, row, account, _payload = await saved()
    stranger = await quote_tests.owner(0)
    first, second = search_tests.SearchRPC(row, account), search_tests.SearchRPC(row, account)
    async with proof_tests.sources(first, second) as sources:
        for principal, identity in ((stranger, row["id"]), (stranger, uuid4()), (owner, row["id"])):
            with pytest.raises(journal.VideoAttemptError):
                await journal.recover_lost_transaction(principal, identity, sources=sources, from_block=80, to_block=110)
    assert not first.calls and not second.calls
    await no_economic_effects()


async def test_lost_recovery_chain_reads_never_hold_account_or_quote_locks(db, admission):
    owner, row, account, payload = await saved()
    await begin(owner, row, account, payload)
    first, second = search_tests.SearchRPC(row, account), search_tests.SearchRPC(row, account)

    async def unlocked(_method, _params):
        async with await database.new_session() as session:
            await session.execute(sa.select(accounts.c.id).where(accounts.c.id == owner).with_for_update(nowait=True))
            await session.execute(sa.select(quotes.video_quotes.c.id).where(quotes.video_quotes.c.id == row["id"])
                .with_for_update(nowait=True))

    first.before_read = second.before_read = unlocked
    async with proof_tests.sources(first, second) as sources:
        result = await journal.recover_lost_transaction(owner, row["id"], sources=sources, from_block=80, to_block=110)
    assert result["status"] == "proven"
    await no_economic_effects()


async def test_postgres_duplicate_lost_recovery_has_one_original_proof_and_no_execution(db, admission):
    if db[1] != "postgres":
        pytest.skip("real PostgreSQL concurrency required")
    owner, row, account, payload = await saved()
    await begin(owner, row, account, payload)
    first, second = search_tests.SearchRPC(row, account), search_tests.SearchRPC(row, account)
    async with proof_tests.sources(first, second) as sources:
        async def recover():
            return await journal.recover_lost_transaction(owner, row["id"], sources=sources,
                                                          from_block=80, to_block=110)

        results = await asyncio.wait_for(asyncio.gather(*(recover() for _ in range(25)), return_exceptions=True), 30)
        winners = [result for result in results if isinstance(result, dict)]
        assert winners and all(result == winners[0] for result in winners)
        assert all(isinstance(result, (dict, proofs.VideoPaymentUnavailable)) for result in results)
        assert await recover() == winners[0]
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(attempts)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(metadata.tables["grid_payment_receipt_claims"])) == 1
    await no_economic_effects()


@pytest.mark.parametrize("phase", ["report", "proof"])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_lost_recovery_commit_reply_errors_never_allow_another_send(db, admission, monkeypatch, phase, after_commit):
    owner, row, account, payload = await saved()
    await begin(owner, row, account, payload)
    first, second = search_tests.SearchRPC(row, account), search_tests.SearchRPC(row, account)
    factory = journal.new_session
    commits = 0

    async def uncertain_session():
        session = await factory()
        commit = session.commit

        async def maybe_fail():
            nonlocal commits
            commits += 1
            if commits == (1 if phase == "report" else 2):
                if after_commit:
                    await commit()
                raise RuntimeError("private SQL and signature must stay private")
            await commit()

        session.commit = maybe_fail
        return session

    monkeypatch.setattr(journal, "new_session", uncertain_session)
    async with proof_tests.sources(first, second) as sources:
        with pytest.raises(journal.VideoAttemptError) as error:
            await journal.recover_lost_transaction(owner, row["id"], sources=sources, from_block=80, to_block=110)
        assert "private SQL" not in str(error.value) and payload["signature"] not in str(error.value)
        monkeypatch.setattr(journal, "new_session", factory)
        retained = await journal.lookup(owner, row["id"])
        expected = "settling" if phase == "report" and not after_commit else (
            "proven" if phase == "proof" and after_commit else "reported")
        assert retained["status"] == expected
        result = await journal.recover_lost_transaction(owner, row["id"], sources=sources, from_block=80, to_block=110)
    assert result["status"] == "proven" and result["transaction_hash"] == proof_tests._TX
    recovered, winner = await begin(owner, row, account, payload)
    assert recovered == result and not winner
    await no_economic_effects()


@pytest.mark.parametrize("mode", ["generated", "audio", "video"])
async def test_commit_precedes_send_permission_and_never_retains_a_spending_capability(db, admission, mp4, mode):
    owner, row, account, payload = await saved(mode=mode, video=mp4)
    result, winner = await begin(owner, row, account, payload)
    assert winner and result["status"] == "settling"
    recovered = await journal.lookup(owner, row["id"])
    assert recovered == result
    assert payload["signature"] not in json.dumps(recovered, default=str)
    assert "authorization" not in recovered and "signature" not in recovered
    assert recovered["payload_hash"] == journal._payload_hash(payload)
    assert recovered["authorization_identity"] == bindings.verify(row, payload, expected_payer=account.address).authorization_identity
    assert not quotes.view(await quotes.lookup(owner, quote_id=row["id"]))["payment_enabled"]
    await no_economic_effects()


async def test_exact_replay_after_expiry_or_admission_rollback_cannot_send_again(db, admission, monkeypatch):
    owner, row, account, payload = await saved()
    initial, winner = await begin(owner, row, account, payload)
    assert winner
    late = row["expires"] + timedelta(days=1)
    monkeypatch.setattr(bindings, "_now", lambda: quotes._aware(late))
    monkeypatch.setattr(journal, "_now", lambda: quotes._aware(late))
    repeated, winner = await begin(owner, row, account, payload, admission_until=quotes._aware(late) - timedelta(seconds=1))
    assert not winner and repeated == initial
    assert await journal.lookup(owner, row["id"]) == initial
    await no_economic_effects()


@pytest.mark.parametrize("change", ["signature", "amount", "payer", "policy"])
async def test_changed_authorization_or_policy_cannot_replace_a_retained_attempt(db, admission, change):
    owner, row, account, payload = await saved()
    original, _ = await begin(owner, row, account, payload)
    changed, overrides = copy.deepcopy(payload), {}
    if change == "signature":
        changed["signature"] = "0x" + "0" * 130
    elif change == "amount":
        changed["authorization"]["value"] = "1"
    elif change == "payer":
        account = Account.create()
    else:
        overrides["confirmation_tag"] = "finalized"
    with pytest.raises(journal.VideoAttemptConflict):
        await begin(owner, row, account, changed, **overrides)
    assert await journal.lookup(owner, row["id"]) == original


async def test_foreign_and_missing_ownership_are_indistinguishable(db, admission):
    owner, row, account, payload = await saved()
    await begin(owner, row, account, payload)
    stranger = await quote_tests.owner(0)
    from uuid import uuid4
    for principal, quote_id in ((stranger, row["id"]), (stranger, uuid4())):
        with pytest.raises(journal.VideoAttemptError, match="quote or attempt unavailable"):
            await journal.lookup(principal, quote_id)
        with pytest.raises(journal.VideoAttemptError):
            await journal.report(principal, quote_id, proof_tests._TX)


async def test_proved_alias_recovers_original_attempt_without_new_payment_or_owner_rewrite(db, admission):
    owner, row, account, payload = await saved()
    original, _ = await begin(owner, row, account, payload)
    target = await quote_tests.owner(0)
    async with await database.new_session() as session:
        await session.execute(account_aliases.insert().values(source_account_id=owner, canonical_account_id=target,
            merge_ref="synthetic merge"))
        await session.commit()
    assert await journal.lookup(target, row["id"]) == original
    replay, winner = await begin(target, row, account, payload)
    assert not winner and replay == original


@pytest.mark.parametrize("invalid", ["bad_signature", "expired", "admission", "policy"])
async def test_invalid_first_attempt_creates_no_journal_or_economic_state(db, admission, monkeypatch, invalid):
    owner, row, account, payload = await saved()
    overrides = {}
    if invalid == "bad_signature":
        payload["signature"] = "0x" + "0" * 130
    elif invalid == "expired":
        monkeypatch.setattr(bindings, "_now", lambda: quotes._aware(row["expires"]))
    elif invalid == "admission":
        overrides["admission_until"] = datetime.now(UTC) - timedelta(seconds=1)
    else:
        overrides["min_confirmations"] = True
    with pytest.raises(journal.VideoAttemptError):
        await begin(owner, row, account, payload, **overrides)
    assert await journal.lookup(owner, row["id"]) is None
    await no_economic_effects()


@pytest.mark.parametrize("after_commit", [False, True])
async def test_database_commit_failure_or_reply_loss_never_mints_another_send_permission(db, admission, monkeypatch, after_commit):
    owner, row, account, payload = await saved()
    factory = database.new_session

    async def uncertain_session():
        session = await factory()
        commit = session.commit

        async def fail():
            if after_commit:
                await commit()
            raise RuntimeError("private SQL and spending capability")

        session.commit = fail
        return session

    monkeypatch.setattr(journal, "new_session", uncertain_session)
    with pytest.raises(journal.VideoAttemptError) as error:
        await begin(owner, row, account, payload)
    assert "private SQL" not in str(error.value) and payload["signature"] not in str(error.value)
    monkeypatch.setattr(journal, "new_session", factory)
    recovered = await journal.lookup(owner, row["id"])
    assert (recovered is not None) == after_commit
    if after_commit:
        _replay, winner = await begin(owner, row, account, payload)
        assert not winner
    await no_economic_effects()


async def test_report_is_immutable_and_is_not_a_receipt_or_funding(db, admission):
    owner, row, account, payload = await saved()
    with pytest.raises(journal.VideoAttemptError):
        await journal.report(owner, row["id"], proof_tests._TX)
    await begin(owner, row, account, payload)
    result = await journal.report(owner, row["id"], proof_tests._TX)
    assert result["status"] == "reported" and result["proof"] is None
    assert await journal.report(owner, row["id"], proof_tests._TX) == result
    with pytest.raises(journal.VideoAttemptConflict):
        await journal.report(owner, row["id"], "0x" + "e" * 64)
    assert await journal.lookup(owner, row["id"]) == result
    await no_economic_effects()


async def test_transaction_unique_guard_rolls_back_the_second_report(db, admission):
    one, first, signer, payload = await saved()
    two, second, other, other_payload = await saved()
    await begin(one, first, signer, payload)
    await begin(two, second, other, other_payload)
    await journal.report(one, first["id"], proof_tests._TX)
    with pytest.raises(journal.VideoAttemptError):
        await journal.report(two, second["id"], proof_tests._TX)
    assert (await journal.lookup(two, second["id"]))["status"] == "settling"
    await no_economic_effects()


async def test_proof_commits_after_expiry_outside_locks_and_replays_without_rpc(db, admission, monkeypatch):
    owner, row, account, payload = await saved()
    await begin(owner, row, account, payload)
    await journal.report(owner, row["id"], proof_tests._TX)
    first, second = proof_tests.RPCFixture(row, account), proof_tests.RPCFixture(row, account)

    async def assert_unlocked(method, params):
        async with await database.new_session() as session:
            await session.execute(sa.select(accounts.c.id).where(accounts.c.id == owner).with_for_update(nowait=True))
            await session.execute(sa.select(quotes.video_quotes.c.id).where(quotes.video_quotes.c.id == row["id"])
                .with_for_update(nowait=True))

    first.before_read = assert_unlocked
    monkeypatch.setattr(journal, "_now", lambda: quotes._aware(row["expires"]) + timedelta(days=1))
    async with proof_tests.sources(first, second) as sources:
        result = await journal.reconcile(owner, row["id"], sources=sources)
        assert result["status"] == "proven" and result["proof_hash"] == quotes._hash(result["proof"])
        assert result["proof"]["amount_micro"] == 7500
        count = len(first.calls) + len(second.calls)
        first.fault = second.fault = RuntimeError("must not retry the original winning proof")
        assert await journal.reconcile(owner, row["id"], sources=sources) == result
        assert count == len(first.calls) + len(second.calls)
    assert await journal.lookup(owner, row["id"]) == result
    assert payload["signature"] not in json.dumps(result, default=str)
    await no_economic_effects()


@pytest.mark.parametrize("kind", ["pending", "transport", "invalid"])
async def test_uncertain_or_failed_proof_never_becomes_paid_or_dispatchable(db, admission, kind):
    owner, row, account, payload = await saved()
    await begin(owner, row, account, payload)
    original = await journal.report(owner, row["id"], proof_tests._TX)
    first, second = proof_tests.RPCFixture(row, account), proof_tests.RPCFixture(row, account)
    if kind == "pending":
        first.tx = None
    elif kind == "transport":
        first.fault = RuntimeError("private RPC error")
    else:
        first.receipt["status"] = "0x0"
    async with proof_tests.sources(first, second) as sources:
        if kind == "invalid":
            result = await journal.reconcile(owner, row["id"], sources=sources)
            assert result["status"] == "manual_review" and result["review_code"] == "receipt_unproven"
            assert result["proof"] is None
        else:
            with pytest.raises(proofs.VideoPaymentProofError):
                await journal.reconcile(owner, row["id"], sources=sources)
            assert await journal.lookup(owner, row["id"]) == original
    await no_economic_effects()


async def test_changed_quote_or_proof_is_not_recovery_authority(db, admission):
    owner, row, account, payload = await saved()
    await begin(owner, row, account, payload)
    async with await database.new_session() as session:
        await session.execute(attempts.update().where(attempts.c.quote_id == row["id"]).values(quote_commitment="f" * 64))
        await session.commit()
    with pytest.raises(journal.VideoAttemptError):
        await journal.lookup(owner, row["id"])
    await no_economic_effects()


async def test_postgres_duplicate_begin_has_exactly_one_send_winner(db, admission):
    if db[1] != "postgres":
        pytest.skip("real PostgreSQL race requires PostgreSQL")
    owner, row, account, payload = await saved()
    results = await asyncio.gather(*(begin(owner, row, account, payload) for _ in range(25)))
    assert sum(winner for _attempt, winner in results) == 1
    assert len({item["payload_hash"] for item, _winner in results}) == 1
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(attempts)) == 1
    await no_economic_effects()


async def test_postgres_waiter_rechecks_quote_expiry_after_the_actual_lock(db, admission, monkeypatch):
    if db[1] != "postgres":
        pytest.skip("lock observation requires PostgreSQL")
    owner, row, account, payload = await saved()
    holder = await database.new_session()
    task = None
    try:
        await holder.execute(sa.select(quotes.video_quotes.c.id).where(quotes.video_quotes.c.id == row["id"]).with_for_update())
        pid = await holder.scalar(sa.text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(begin(owner, row, account, payload,
            admission_until=quotes._aware(row["expires"]) + timedelta(minutes=1)))
        async with await database.new_session() as observer:
            await observer.execute(sa.text("SELECT 1 FROM pg_stat_activity LIMIT 1"))
            for _ in range(300):
                await observer.execute(sa.text("SELECT pg_stat_clear_snapshot()"))
                blocked = await observer.scalar(sa.text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)))"), {"pid": pid})
                if blocked:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("the owned contender did not block on the held quote lock")
        late = quotes._aware(row["expires"])
        monkeypatch.setattr(bindings, "_now", lambda: late)
        monkeypatch.setattr(journal, "_now", lambda: late)
        await holder.rollback()
        with pytest.raises(journal.VideoAttemptError):
            await asyncio.wait_for(task, 5)
    finally:
        await holder.close()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await journal.lookup(owner, row["id"]) is None
    await no_economic_effects()


@pytest.mark.parametrize("after_commit", [False, True])
async def test_proof_commit_failure_or_reply_loss_preserves_the_original_attempt(db, admission, monkeypatch, after_commit):
    owner, row, account, payload = await saved()
    await begin(owner, row, account, payload)
    await journal.report(owner, row["id"], proof_tests._TX)
    first, second = proof_tests.RPCFixture(row, account), proof_tests.RPCFixture(row, account)
    factory = database.new_session

    async def uncertain_session():
        session = await factory()
        commit = session.commit

        async def fail():
            if after_commit:
                await commit()
            raise RuntimeError("private SQL and RPC details")

        session.commit = fail
        return session

    monkeypatch.setattr(journal, "new_session", uncertain_session)
    async with proof_tests.sources(first, second) as sources:
        with pytest.raises(journal.VideoAttemptError):
            await journal.reconcile(owner, row["id"], sources=sources)
    monkeypatch.setattr(journal, "new_session", factory)
    recovered = await journal.lookup(owner, row["id"])
    assert recovered["status"] == ("proven" if after_commit else "reported")
    replay, winner = await begin(owner, row, account, payload)
    assert not winner and replay == recovered
    await no_economic_effects()


async def test_saved_proof_tamper_cannot_authorize_recovery(db, admission):
    owner, row, account, payload = await saved()
    await begin(owner, row, account, payload)
    await journal.report(owner, row["id"], proof_tests._TX)
    first, second = proof_tests.RPCFixture(row, account), proof_tests.RPCFixture(row, account)
    async with proof_tests.sources(first, second) as sources:
        proven = await journal.reconcile(owner, row["id"], sources=sources)
    changed = copy.deepcopy(proven["proof"])
    changed["amount_micro"] = 1
    async with await database.new_session() as session:
        await session.execute(attempts.update().where(attempts.c.quote_id == row["id"]).values(proof=changed))
        await session.commit()
    with pytest.raises(journal.VideoAttemptError):
        await journal.lookup(owner, row["id"])
    await no_economic_effects()


def migration():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0047_video_payment_attempts.py"
    spec = importlib.util.spec_from_file_location("video_attempt_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


async def test_migration_parity_and_liability_retention_on_real_database(db, admission):
    revision = migration()
    assert revision.down_revision == "0046"

    def apply(connection):
        prepaid = metadata.tables["grid_video_prepaid_payments"]
        assert connection.scalar(sa.select(sa.func.count()).select_from(prepaid)) == 0
        prepaid.drop(connection)
        attempts.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            inspector = sa.inspect(connection)
            assert {c["name"]: c["nullable"] for c in inspector.get_columns(attempts.name)} == {
                column.name: column.nullable for column in attempts.columns}
            for kind, read in ((sa.CheckConstraint, inspector.get_check_constraints),
                               (sa.UniqueConstraint, inspector.get_unique_constraints)):
                assert {c["name"] for c in read(attempts.name)} == {c.name for c in attempts.constraints if isinstance(c, kind)}
            assert {c["name"] for c in inspector.get_indexes(attempts.name) if not c.get("duplicates_constraint")} == {
                index.name for index in attempts.indexes}
            assert {c["referred_table"] for c in inspector.get_foreign_keys(attempts.name)} == {"grid_video_quotes"}
            revision.downgrade()
            revision.upgrade()
        prepaid.create(connection)

    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(apply)
    owner, row, account, payload = await saved()
    await begin(owner, row, account, payload)

    def refuse(connection):
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="refuse to discard"):
            revision.downgrade()

    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(refuse)
    assert (await journal.lookup(owner, row["id"]))["status"] == "settling"

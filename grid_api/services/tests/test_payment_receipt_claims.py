# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real cross-rail SQL ownership; chain/signatures/dispatch remain fixtures."""

import asyncio
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from alembic.migration import MigrationContext
from alembic.operations import Operations
from grid_api import database
from grid_api.services import deposits, x402_payments
from grid_api.services import payment_receipt_claims as claims
from grid_api.services import video_payment_attempts as journal
from grid_api.services.tests import test_video_payment_attempts as attempt_tests
from grid_api.services.tests import test_video_payment_proofs as proof_tests
from grid_api.services.tests import test_x402_payments as text_tests
from grid_api.v2.schema import credit_ledger, credits, metadata, reservations
from grid_api.v2.schema import deposits as deposit_rows
from grid_api.v2.schema import payment_receipt_claims as claim_rows
from grid_api.v2.schema import x402_payments as text_rows

db = attempt_tests.db
admission = attempt_tests.admission
registry = attempt_tests.registry
synthetic_dependencies = attempt_tests.synthetic_dependencies
pytestmark = pytest.mark.asyncio
ASSET = "0x" + "2" * 40
OTHER_ASSET = "0x" + "4" * 40
TX = "0x" + "a" * 64


async def claim(*, kind="deposit", ref="synthetic-one", asset=ASSET, tx=TX, purpose=None):
    async with await database.new_session() as session:
        applied = await claims.claim_in_session(session, chain_id=8453, asset=asset, transaction_hash=tx,
            owner_kind=kind, owner_ref=ref, purpose=purpose or {"schema": "synthetic-receipt.v1", "units": 7500})
        await session.commit()
        return applied


async def count(table):
    async with await database.new_session() as session:
        return await session.scalar(sa.select(sa.func.count()).select_from(table))


@pytest.mark.parametrize("first,second", [("deposit", "text_x402"), ("deposit", "video_x402"),
    ("text_x402", "deposit"), ("text_x402", "video_x402"), ("video_x402", "deposit"), ("video_x402", "text_x402")])
async def test_shared_identity_excludes_another_rail_in_both_directions(db, first, second):
    assert await claim(kind=first)
    with pytest.raises(claims.ReceiptClaimConflict):
        await claim(kind=second, ref="synthetic-two")
    assert await count(claim_rows) == 1


async def test_replay_is_immutable_and_one_purpose_cannot_change_its_receipt(db):
    assert await claim()
    assert not await claim(asset=ASSET.upper().replace("0X", "0x"), tx=TX.upper().replace("0X", "0x"))
    with pytest.raises(claims.ReceiptClaimConflict):
        await claim(purpose={"schema": "synthetic-receipt.v1", "units": 1})
    with pytest.raises(claims.ReceiptClaimConflict):
        await claim(tx="0x" + "b" * 64)
    assert await count(claim_rows) == 1


async def test_distinct_token_transfers_in_one_transaction_keep_their_own_value(db):
    assert await claim()
    assert await claim(ref="synthetic-other", asset=OTHER_ASSET)
    assert await count(claim_rows) == 2


async def test_caller_rollback_discards_claim_and_all_associated_writes(db):
    async with await database.new_session() as session:
        await claims.claim_in_session(session, chain_id=8453, asset=ASSET, transaction_hash=TX,
            owner_kind="deposit", owner_ref="rollback-one", purpose={"units": 1})
        await session.rollback()
    assert await count(claim_rows) == 0
    assert await claim()


async def store_deposit(owner, *, asset=ASSET, tx=TX, amount=7500, converted=False,
                        sender=text_tests.PAYER, treasury=text_tests.TREASURY, observed=()):
    return await deposits._record_and_credit(account={"account_id": owner}, asset="ETH" if converted else "USDC",
        token_address=None if converted else asset, tx_hash=tx, block_number=100, sender=sender,
        treasury=treasury, amount_raw=10**18 if converted else amount, decimals=18 if converted else 6,
        price_micro=amount if converted else 1_000_000,
        price_source="swap:actual-base-usdc-proceeds" if converted else "usdc:1:1",
        price_timestamp=datetime.now(UTC), price_block=100, credited_micro=amount,
        receipt_asset=asset if converted else None, receipt_authorizations=observed)


async def text_payment(monkeypatch, *, asset=ASSET, amount=7500, chain_id=8453):
    monkeypatch.setattr(x402_payments, "ENABLED", True)
    monkeypatch.setattr(x402_payments, "NETWORK", "eip155:8453")
    monkeypatch.setattr(x402_payments, "USDC", text_tests.USDC)
    monkeypatch.setattr(x402_payments, "PAY_TO", text_tests.TREASURY)
    monkeypatch.setattr(deposits, "CHAIN_ID", chain_id)
    job = str(uuid4())
    await text_tests._terminal_payment(job, 100_000, amount)
    await x402_payments._before_settle(text_tests._hook_context(job, amount))

    async def confirmed(tx_hash, _label):
        return {"hash": tx_hash}, {"status": "0x1", "blockNumber": "0x64", "logs": [{"address": asset,
            "topics": [text_tests.TRANSFER_TOPIC, text_tests._topic(text_tests.PAYER), text_tests._topic(text_tests.TREASURY)],
            "data": hex(amount)}]}, 100

    async def timestamp(_block):
        return datetime.now(UTC)

    monkeypatch.setattr(deposits, "_confirmed_transaction", confirmed)
    monkeypatch.setattr(deposits, "_block_timestamp", timestamp)
    return job


async def test_actual_deposit_and_text_reconcile_cannot_both_book_one_transfer(db, monkeypatch):
    owner = await attempt_tests.quote_tests.owner(0)
    monkeypatch.setattr(deposits, "CHAIN_ID", 8453)
    await store_deposit(owner)
    job = await text_payment(monkeypatch)
    with pytest.raises(claims.ReceiptClaimConflict):
        await x402_payments.reconcile_transaction(job, TX)
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(text_rows.c.status).where(text_rows.c.job_id == job)) == "settling"
        assert await session.scalar(sa.select(credits.c.balance_micro).where(credits.c.account_id == owner)) == 7500
    assert await count(claim_rows) == await count(deposit_rows) == await count(credit_ledger) == 1


async def test_actual_text_reconcile_blocks_later_deposit_without_creating_credit(db, monkeypatch):
    owner = await attempt_tests.quote_tests.owner(0)
    job = await text_payment(monkeypatch)
    assert (await x402_payments.reconcile_transaction(job, TX))["status"] == "settled"
    with pytest.raises(HTTPException) as error:
        await store_deposit(owner)
    assert error.value.status_code == 409
    assert await count(deposit_rows) == await count(credit_ledger) == 0
    assert await count(claim_rows) == 1


async def test_conversion_claims_actual_usdc_not_the_native_input(db, monkeypatch):
    owner = await attempt_tests.quote_tests.owner(0)
    monkeypatch.setattr(deposits, "CHAIN_ID", 8453)
    await store_deposit(owner, converted=True)
    async with await database.new_session() as session:
        claim_row = (await session.execute(sa.select(claim_rows))).mappings().one()
        assert claim_row["asset"] == ASSET and claim_row["purpose"]["input_asset"] == "ETH"
    with pytest.raises(HTTPException):
        await store_deposit(owner)
    assert await count(deposit_rows) == await count(credit_ledger) == await count(claim_rows) == 1


async def test_deposit_credit_failure_rolls_back_receipt_and_shared_ownership(db, monkeypatch):
    owner = await attempt_tests.quote_tests.owner(0)
    monkeypatch.setattr(deposits, "CHAIN_ID", 8453)
    from grid_api.services import credits as credit_service

    async def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic accounting fault")

    monkeypatch.setattr(credit_service, "_credit_in_session", fail)
    with pytest.raises(RuntimeError):
        await store_deposit(owner)
    assert await count(deposit_rows) == await count(claim_rows) == await count(credit_ledger) == 0


async def test_historical_deposit_is_exclusion_evidence_not_a_fabricated_new_claim(db, monkeypatch):
    owner = await attempt_tests.quote_tests.owner(0)
    monkeypatch.setattr(deposits, "CHAIN_ID", 8453)
    await store_deposit(owner)
    # Explicitly simulate a pre-0048 database's missing claim in this disposable schema.
    async with await database.new_session() as session:
        await session.execute(claim_rows.delete())
        await session.commit()
    with pytest.raises(claims.ReceiptClaimConflict):
        await claim(kind="video_x402", ref="new-video")
    replay, _receipt, balance = await store_deposit(owner)
    assert not replay and balance == 7500
    assert await count(claim_rows) == 0 and await count(deposit_rows) == 1


async def test_ambiguous_legacy_conversion_cannot_be_repriced_or_reassigned(db, monkeypatch):
    owner = await attempt_tests.quote_tests.owner(0)
    monkeypatch.setattr(deposits, "CHAIN_ID", 8453)
    await store_deposit(owner, converted=True)
    async with await database.new_session() as session:
        await session.execute(claim_rows.delete())
        await session.commit()
    for token in (ASSET, OTHER_ASSET):
        with pytest.raises(claims.ReceiptClaimConflict):
            await claim(kind="text_x402", ref="new-text", asset=token)
    assert await count(claim_rows) == 0


async def test_video_payment_conflict_retains_verified_money_evidence_for_review(db, admission, monkeypatch):
    owner, row, signer, payload = await attempt_tests.saved()
    monkeypatch.setattr(deposits, "CHAIN_ID", 84532)
    await store_deposit(owner, asset=row["snapshot"]["payment"]["asset"], tx=proof_tests._TX,
        sender=signer.address.lower(), treasury=row["snapshot"]["payment"]["pay_to"])
    await attempt_tests.begin(owner, row, signer, payload)
    await journal.report(owner, row["id"], proof_tests._TX)
    first, second = proof_tests.RPCFixture(row, signer), proof_tests.RPCFixture(row, signer)
    for source in (first, second):
        source.tx["from"] = source.receipt["from"] = signer.address.lower()
    async with proof_tests.sources(first, second) as sources:
        result = await journal.reconcile(owner, row["id"], sources=sources)
    assert result["status"] == "manual_review" and result["review_code"] == "receipt_claim_conflict"
    assert result["proof"]["amount_micro"] == 7500 and result["proof_hash"] is not None
    assert await count(metadata.tables["grid_media_jobs"]) == 0
    assert await count(claim_rows) == await count(credit_ledger) == 1


async def test_video_shared_ownership_blocks_a_deposit_but_does_not_grant_dispatch(db, admission, monkeypatch):
    owner, row, signer, payload = await attempt_tests.saved()
    await attempt_tests.begin(owner, row, signer, payload)
    await journal.report(owner, row["id"], proof_tests._TX)
    first, second = proof_tests.RPCFixture(row, signer), proof_tests.RPCFixture(row, signer)
    for source in (first, second):
        source.tx["from"] = source.receipt["from"] = signer.address.lower()
    async with proof_tests.sources(first, second) as sources:
        result = await journal.reconcile(owner, row["id"], sources=sources)
    assert result["status"] == "proven"
    monkeypatch.setattr(deposits, "CHAIN_ID", 84532)
    with pytest.raises(HTTPException) as error:
        await store_deposit(owner, asset=row["snapshot"]["payment"]["asset"], tx=proof_tests._TX,
            sender=signer.address.lower(), treasury=row["snapshot"]["payment"]["pay_to"])
    assert error.value.status_code == 409
    async with await database.new_session() as session:
        owned = await claims.require_owned_in_session(session, chain_id=84532, asset=row["snapshot"]["payment"]["asset"],
            transaction_hash=proof_tests._TX, owner_kind="video_x402", owner_ref=str(row["id"]))
        assert owned["purpose"]["quote_commitment"] == row["commitment"]
    assert await count(deposit_rows) == await count(credit_ledger) == await count(reservations) == 0


async def test_spent_nonce_protects_video_before_a_lost_facilitator_reply_is_recorded(db, admission, monkeypatch):
    owner, row, signer, payload = await attempt_tests.saved()
    await attempt_tests.begin(owner, row, signer, payload)
    source = proof_tests.RPCFixture(row, signer)
    asset = row["snapshot"]["payment"]["asset"]
    witnesses = claims.observed_authorizations(source.receipt, asset)
    assert witnesses == ((signer.address.lower(), row["authorization_nonce"]),)
    monkeypatch.setattr(deposits, "CHAIN_ID", 84532)
    with pytest.raises(HTTPException) as error:
        await store_deposit(owner, asset=asset, tx=proof_tests._TX, observed=witnesses,
            sender=signer.address.lower(), treasury=row["snapshot"]["payment"]["pay_to"])
    assert error.value.status_code == 409
    async with await database.new_session() as session:
        with pytest.raises(claims.ReceiptClaimConflict):
            await claims.claim_in_session(session, chain_id=84532, asset=asset, transaction_hash=proof_tests._TX,
                owner_kind="text_x402", owner_ref="wrong-text", purpose={"units": 7500}, observed_nonces=witnesses)
    assert (await journal.lookup(owner, row["id"]))["transaction_hash"] is None
    assert await count(deposit_rows) == await count(credit_ledger) == await count(claim_rows) == 0
    await journal.report(owner, row["id"], proof_tests._TX)
    second = proof_tests.RPCFixture(row, signer)
    async with proof_tests.sources(source, second) as sources:
        assert (await journal.reconcile(owner, row["id"], sources=sources))["status"] == "proven"
    assert await count(claim_rows) == 1


@pytest.mark.parametrize("kind", ["padding", "nonce", "removed", "type", "bound", "data"])
async def test_malformed_spent_nonce_evidence_is_not_silently_ignored(db, kind):
    log = {"address": ASSET, "topics": [claims._AUTH_USED, "0x" + "0" * 24 + "1" * 40, "0x" + "2" * 64], "data": "0x"}
    if kind == "padding":
        log["topics"][1] = "0x" + "e" * 24 + "1" * 40
    elif kind == "nonce":
        log["topics"][2] = "0x12"
    elif kind == "removed":
        log["removed"] = True
    elif kind == "type":
        log["topics"] = True
    elif kind == "data":
        log["data"] = "0x01"
    with pytest.raises(claims.ReceiptClaimError):
        claims.observed_authorizations({"logs": [log] * (129 if kind == "bound" else 1)}, ASSET)


@pytest.mark.parametrize("kind", ["identity", "purpose"])
async def test_corrupt_shared_claim_cannot_authorize_future_admission(db, kind):
    await claim()
    async with await database.new_session() as session:
        await session.execute(claim_rows.update().values(**({"purpose_hash": "f" * 64} if kind == "purpose" else {"claim_key": "f" * 64})))
        await session.commit()
    async with await database.new_session() as session:
        with pytest.raises(claims.ReceiptClaimError):
            await claims.require_owned_in_session(session, chain_id=8453, asset=ASSET, transaction_hash=TX,
                owner_kind="deposit", owner_ref="synthetic-one")


async def test_wrong_configured_text_rpc_chain_never_claims_or_settles(db, monkeypatch):
    job = await text_payment(monkeypatch, chain_id=84532)
    with pytest.raises(RuntimeError, match="recorded payment chain"):
        await x402_payments.reconcile_transaction(job, TX)
    assert await count(claim_rows) == 0


async def test_postgres_mixed_rail_race_claims_value_once(db):
    if db[1] != "postgres":
        pytest.skip("real cross-rail concurrency requires PostgreSQL")

    async def racer(index):
        try:
            await claim(kind=("deposit", "text_x402", "video_x402")[index % 3], ref=f"racer-{index}")
            return True
        except claims.ReceiptClaimConflict:
            return False

    outcomes = await asyncio.gather(*(racer(index) for index in range(25)))
    assert sum(outcomes) == 1 and await count(claim_rows) == 1


def migration():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0048_payment_receipt_claims.py"
    spec = importlib.util.spec_from_file_location("receipt_claim_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


async def test_migration_parity_empty_downgrade_and_retained_ownership(db):
    revision = migration()
    assert revision.down_revision == "0047"

    def apply(connection):
        claim_rows.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            inspector = sa.inspect(connection)
            assert {c["name"]: c["nullable"] for c in inspector.get_columns(claim_rows.name)} == {
                column.name: column.nullable for column in claim_rows.columns}
            for kind, read in ((sa.CheckConstraint, inspector.get_check_constraints),
                               (sa.UniqueConstraint, inspector.get_unique_constraints)):
                assert {c["name"] for c in read(claim_rows.name)} == {c.name for c in claim_rows.constraints if isinstance(c, kind)}
            revision.downgrade()
            revision.upgrade()

    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(apply)
    await claim()

    def refuse(connection):
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="refuse to discard"):
            revision.downgrade()

    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(refuse)
    assert await count(claim_rows) == 1

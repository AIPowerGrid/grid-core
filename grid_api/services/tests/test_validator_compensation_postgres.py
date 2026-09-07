# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable validator allocation proofs; isolated PostgreSQL, never a sender."""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from grid_api import database
from grid_api.services import validator_compensation as comp
from grid_api.services import validators as evidence
from grid_api.v2 import schema as tables

PG = os.environ.get("VALIDATORS_TEST_DB_URL", "")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG.startswith("postgresql"),
        reason="requires disposable PostgreSQL",
    ),
]
START = datetime(2030, 1, 1, tzinfo=UTC)
END = START + timedelta(days=7)
VERSION = "v0.1.0-preview.17"


@pytest_asyncio.fixture
async def db(monkeypatch):
    namespace = "validator_comp_test_" + uuid.uuid4().hex
    engine = create_async_engine(PG, execution_options={"schema_translate_map": {None: namespace}}, pool_size=5, max_overflow=20)
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.schema.CreateSchema(namespace))
            created = True
            await connection.run_sync(tables.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(database, "_session_factory", factory)
        monkeypatch.setattr(comp, "_now", lambda: START - timedelta(minutes=1))
        monkeypatch.setattr(comp, "cohort_version_status", lambda version: (VERSION, version == VERSION))
        yield factory
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(sa.schema.DropSchema(namespace, cascade=True))
        finally:
            await engine.dispose()


async def members(db):
    result = []
    async with db() as session:
        for i in range(3):
            signer = Account.create()
            account = uuid.uuid4()
            validator = "val_" + uuid.uuid4().hex
            await session.execute(sa.insert(tables.accounts).values(id=account, wallet=signer.address.lower()))
            await session.execute(
                sa.insert(tables.validators).values(
                    id=validator,
                    account_id=account,
                    signing_wallet=signer.address.lower(),
                    software_version=VERSION,
                    capabilities=["text.basic.v1"],
                    registration_signature="0x" + "ab" * 65,
                    status="active",
                    last_heartbeat=START - timedelta(seconds=61),
                    independence_status="verified",
                    operator_group_id=f"opg_fixture{i:08d}",
                    independence_reviewed_at=START - timedelta(days=1),
                    independence_expires_at=END + timedelta(days=30),
                    independence_review_ref=f"review:fixture-{i}",
                ),
            )
            result.append((validator, account, signer))
        await session.commit()
    return result


def terms(campaign="pilot-fixture", budget=10**18 + 1):
    return dict(
        campaign_id=campaign,
        starts_at=START.isoformat(),
        ends_at=END.isoformat(),
        budget_atomic=str(budget),
        operator_cap_atomic=str(2_000 * 10**18),
        daily_unit_cap=10,
        software_version=VERSION,
        approval_ref="approval:fixture",
    )


async def create(db, group, campaign="pilot-fixture", **changes):
    request = {**terms(campaign), **changes}
    ids = [row[0] for row in group]
    preview = await comp.create_campaign(request, ids)
    assert preview["dry_run"] is True
    async with db() as session:
        assert not await session.scalar(sa.select(sa.func.count()).select_from(tables.validator_compensation_campaigns))
    return await comp.create_campaign(request, ids, apply=True, expected_digest=preview["digest"])


async def report(db, member, *, verdict="healthy", actual="healthy", completed=None):
    validator, account, signer = member
    completed = completed or START + timedelta(hours=1)
    group, assignment = "vpg_" + uuid.uuid4().hex, "asg_" + uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    worker = str(uuid.uuid4())
    evidence_hash = uuid.uuid4().hex * 2
    payload = dict(
        validator=signer.address.lower(),
        assignment_source="grid",
        assignment_id=assignment,
        probe_group_id=group,
        grid_nonce=nonce,
        evidence_hash=evidence_hash,
        worker_id=worker,
        model="fixture-model",
        modality="text",
        capability="text.basic.v1",
        canary_kind="echo",
        verdict=verdict,
        latency_ms=1,
    )
    signature = "0x" + Account.sign_message(
        encode_defunct(text=json.dumps(payload, sort_keys=True, separators=(",", ":"))),
        signer.key,
    ).signature.hex().removeprefix("0x")
    normalized = evidence._normalize(payload, signature)
    async with db() as session:
        await session.execute(
            sa.insert(tables.validator_probe_groups).values(
                id=group,
                target_worker_id=worker,
                target_worker_name="fixture",
                model="fixture-model",
                modality="text",
                capability="text.basic.v1",
                canary_kind="echo",
                scoring_policy_id="text.generated.v8",
                challenge_hash=uuid.uuid4().hex * 2,
                expires=completed + timedelta(minutes=10),
                created=completed - timedelta(minutes=1),
            ),
        )
        await session.execute(
            sa.insert(tables.validator_assignments).values(
                id=assignment,
                probe_group_id=group,
                account_id=account,
                validator_id=validator,
                validator_wallet=signer.address.lower(),
                grid_nonce=nonce,
                target_worker_id=worker,
                target_worker_name="fixture",
                model="fixture-model",
                modality="text",
                capability="text.basic.v1",
                canary_kind="echo",
                scoring_policy_id="text.generated.v8",
                probe_status="completed",
                probe_evidence_hash=evidence_hash,
                probe_verdict=actual,
                probed=completed,
                created=completed - timedelta(minutes=1),
                expires=completed + timedelta(minutes=10),
            ),
        )
        identifier = await session.scalar(
            sa.insert(tables.validator_attestations)
            .values(
                **normalized,
                account_id=account,
                validator_id=validator,
                created=completed + timedelta(seconds=1),
            )
            .returning(tables.validator_attestations.c.id),
        )
        await session.commit()
    return identifier


def close_time(monkeypatch):
    monkeypatch.setattr(comp, "_now", lambda: END + timedelta(seconds=comp.RECEIPT_GRACE_SECONDS + 1))


async def test_exact_allocations_no_payout_and_idempotent_finalization(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    for member in group:
        await report(db, member)
    close_time(monkeypatch)
    preview = await comp.finalize_campaign("pilot-fixture")
    assert preview["allocated_atomic"] == str(10**18 - 1)
    assert preview["unallocated_atomic"] == "2"
    result = await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=preview["digest"])
    assert result["status"] == "finalized" and result["sendable"] is False
    assert await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=result["digest"]) == result
    async with db() as session:
        allocations = (await session.execute(sa.select(tables.validator_compensation_allocations))).mappings().all()
        assert len(allocations) == 3
        assert sum(int(row["amount_atomic"]) for row in allocations) == 10**18 - 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(tables.validator_compensation_work)) == 3
        for table in (tables.payouts, tables.payout_legs, tables.ledger, tables.credit_ledger):
            assert await session.scalar(sa.select(sa.func.count()).select_from(table)) == 0


async def test_failed_worker_verdict_is_paid_but_incorrect_judgment_is_not(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    await report(db, group[0], verdict="failed", actual="failed")
    await report(db, group[1], verdict="healthy", actual="failed")
    close_time(monkeypatch)
    preview = await comp.finalize_campaign("pilot-fixture")
    assert preview["reviewed_units"] == 1
    assert preview["excluded"] == {"incorrect_task_score": 1}


async def test_review_drift_and_stale_preview_cannot_create_allocations(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    await report(db, group[0])
    close_time(monkeypatch)
    preview = await comp.finalize_campaign("pilot-fixture")
    async with db() as session:
        await session.execute(
            sa.update(tables.validators)
            .where(tables.validators.c.id == group[0][0])
            .values(
                independence_status="rejected",
            ),
        )
        await session.commit()
    with pytest.raises(comp.CompensationError):
        await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=preview["digest"])
    async with db() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(tables.validator_compensation_allocations)) == 0


async def test_early_close_and_unapproved_digest_fail_closed(db):
    group = await members(db)
    ids = [row[0] for row in group]
    with pytest.raises(comp.CompensationError):
        await comp.create_campaign(terms(), ids, apply=True, expected_digest="0" * 64)
    await create(db, group)
    with pytest.raises(comp.CompensationError):
        await comp.finalize_campaign("pilot-fixture")


async def test_one_control_group_cannot_fill_multiple_paid_seats(db):
    group = await members(db)
    async with db() as session:
        await session.execute(
            sa.update(tables.validators)
            .where(tables.validators.c.id == group[1][0])
            .values(
                operator_group_id="opg_fixture00000000",
            ),
        )
        await session.commit()
    with pytest.raises(comp.CompensationError):
        await comp.create_campaign(terms(), [row[0] for row in group])


async def test_signature_corruption_is_not_a_payable_verified_label(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    attestation = await report(db, group[0])
    async with db() as session:
        await session.execute(
            sa.update(tables.validator_attestations)
            .where(
                tables.validator_attestations.c.id == attestation,
            )
            .values(signature="0x" + "ab" * 65),
        )
        await session.commit()
    close_time(monkeypatch)
    with pytest.raises(comp.CompensationError):
        await comp.finalize_campaign("pilot-fixture")


async def test_daily_and_operator_caps_conserve_unallocated_budget(db, monkeypatch):
    group = await members(db)
    await create(db, group, budget_atomic=str(5_000 * 10**18), daily_unit_cap=1)
    for _ in range(3):
        await report(db, group[0])
    close_time(monkeypatch)
    result = await comp.finalize_campaign("pilot-fixture")
    assert result["reviewed_units"] == 1
    assert result["allocated_atomic"] == str(2_000 * 10**18)
    assert result["unallocated_atomic"] == str(3_000 * 10**18)
    assert result["excluded"] == {"daily_cap": 2}


async def test_overlapping_approved_campaign_cannot_pay_the_same_work_twice(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    request = terms("pilot-second")
    ids = [row[0] for row in group]
    preview = await comp.create_campaign(request, ids)
    await comp.create_campaign(request, ids, apply=True, expected_digest=preview["digest"])
    await report(db, group[0])
    close_time(monkeypatch)
    first = await comp.finalize_campaign("pilot-fixture")
    stale_second = await comp.finalize_campaign("pilot-second")
    await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=first["digest"])
    with pytest.raises(comp.CompensationError):
        await comp.finalize_campaign("pilot-second", apply=True, expected_digest=stale_second["digest"])
    current = await comp.finalize_campaign("pilot-second")
    assert current["reviewed_units"] == 0 and current["allocated_atomic"] == "0"
    assert current["excluded"] == {"previously_allocated_or_duplicate_group": 1}


async def test_database_failure_rolls_back_allocations_work_and_campaign_together(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    await report(db, group[0])
    close_time(monkeypatch)
    preview = await comp.finalize_campaign("pilot-fixture")
    # Fail a real database write after allocations have been inserted. Only this
    # disposable schema receives the trigger; the application has no test hook.
    namespace = db.kw["bind"].get_execution_options()["schema_translate_map"][None]
    async with db() as session:
        await session.execute(
            sa.text(
                f'CREATE FUNCTION "{namespace}".reject_claim() RETURNS trigger LANGUAGE plpgsql AS '
                "$$ BEGIN RAISE EXCEPTION 'fixture rejects claim'; END $$",
            ),
        )
        await session.execute(
            sa.text(
                f'CREATE TRIGGER reject_claim BEFORE INSERT ON "{namespace}".grid_validator_compensation_work '
                f'FOR EACH ROW EXECUTE FUNCTION "{namespace}".reject_claim()',
            ),
        )
        await session.commit()
    with pytest.raises(sa.exc.DBAPIError):
        await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=preview["digest"])
    async with db() as session:
        assert await session.scalar(sa.select(tables.validator_compensation_campaigns.c.status)) == "open"
        for table in (tables.validator_compensation_allocations, tables.validator_compensation_work):
            assert await session.scalar(sa.select(sa.func.count()).select_from(table)) == 0
        await session.execute(sa.text(f'DROP TRIGGER reject_claim ON "{namespace}".grid_validator_compensation_work'))
        await session.commit()
    result = await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=preview["digest"])
    assert result["status"] == "finalized"


@pytest.mark.parametrize(
    "column,value",
    [("grid_nonce", "wrong"), ("probe_evidence_hash", "0" * 64), ("validator_wallet", "0x" + "12" * 20), ("probe_status", "running")],
)
async def test_unbound_or_unfinished_assignment_never_allocates(db, monkeypatch, column, value):
    group = await members(db)
    await create(db, group)
    await report(db, group[0])
    async with db() as session:
        await session.execute(sa.update(tables.validator_assignments).values(**{column: value}))
        await session.commit()
    close_time(monkeypatch)
    with pytest.raises(comp.CompensationError):
        await comp.finalize_campaign("pilot-fixture")


async def test_unchanged_campaign_replay_and_changed_budget_are_distinguished(db):
    group = await members(db)
    first = await create(db, group)
    again = await comp.create_campaign(terms(), [row[0] for row in group], apply=True, expected_digest=first["digest"])
    assert again["digest"] == first["digest"]
    with pytest.raises(comp.CompensationError):
        await comp.create_campaign({**terms(), "budget_atomic": "1"}, [row[0] for row in group])


@pytest.mark.parametrize(
    "field,value",
    [
        ("budget_atomic", "10000000000000000000001"),
        ("operator_cap_atomic", "2000000000000000000001"),
        ("budget_atomic", "0"),
        ("budget_atomic", 10.0),
        ("daily_unit_cap", True),
        ("approval_ref", ""),
        ("software_version", "v0.1.0-preview.999"),
    ],
)
async def test_invalid_or_unbounded_terms_never_create_a_contract(db, field, value):
    group = await members(db)
    with pytest.raises(comp.CompensationError):
        await comp.create_campaign({**terms(), field: value}, [row[0] for row in group])


async def test_experimental_lane_does_not_turn_into_paid_work(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    await report(db, group[0])
    async with db() as session:
        await session.execute(sa.update(tables.validator_assignments).values(scoring_policy_id="text.fidelity.v1"))
        await session.commit()
    close_time(monkeypatch)
    result = await comp.finalize_campaign("pilot-fixture")
    assert result["reviewed_units"] == 0
    assert result["excluded"] == {"outside_pilot_policy": 1}


async def test_cli_previews_readonly_without_schema_initialization(db, monkeypatch, tmp_path):
    from scripts import manage_validator_compensation as cli

    group = await members(db)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"terms": terms(), "validator_ids": [row[0] for row in group]}))
    request.chmod(0o600)
    namespace = db.kw["bind"].get_execution_options()["schema_translate_map"][None]
    real_engine = cli.create_async_engine
    flags = []

    def engine(url, **kwargs):
        flags.append(kwargs["connect_args"]["server_settings"]["default_transaction_read_only"])
        return real_engine(url, **kwargs, execution_options={"schema_translate_map": {None: namespace}})

    async def forbidden():
        pytest.fail("operator preview must not initialize any schema")

    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(async_database_url=PG))
    monkeypatch.setattr(cli, "create_async_engine", engine)
    monkeypatch.setattr(database, "init_database", forbidden)
    args = SimpleNamespace(action="create", input=request, campaign_id=None, apply=False, expect_digest=None)
    preview = await cli.run(args)
    assert database._session_factory is db
    async with db() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(tables.validator_compensation_campaigns)) == 0
    args.apply, args.expect_digest = True, preview["digest"]
    result = await cli.run(args)
    assert result["dry_run"] is False and flags == ["on", "off"]
    assert database._session_factory is db


async def test_retained_work_commitment_survives_operational_assignment_pruning(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    identifier = await report(db, group[0])
    close_time(monkeypatch)
    preview = await comp.finalize_campaign("pilot-fixture")
    saved = await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=preview["digest"])
    async with db() as session:
        await session.execute(sa.delete(tables.validator_assignments))
        await session.execute(sa.delete(tables.validator_probe_groups))
        await session.commit()
        claim = (await session.execute(sa.select(tables.validator_compensation_work))).mappings().one()
        attestation = (
            (
                await session.execute(
                    sa.select(tables.validator_attestations).where(
                        tables.validator_attestations.c.id == identifier,
                    ),
                )
            )
            .mappings()
            .one()
        )
        assert attestation["assignment_id"] is None
        payload = attestation["payload"]
        proof = {
            key: payload[key]
            for key in (
                "assignment_id",
                "probe_group_id",
                "grid_nonce",
                "evidence_hash",
                "worker_id",
                "model",
                "modality",
                "capability",
                "canary_kind",
            )
        }
        proof.update(attestation_hash=attestation["attestation_hash"], verification=claim["verification"])
        assert comp._hash(proof) == claim["evidence_commitment"]
    assert await comp.finalize_campaign("pilot-fixture") == saved


@pytest.mark.parametrize("corruption", ["amount", "beneficiary", "manifest"])
async def test_finalized_record_drift_fails_reconciliation(db, monkeypatch, corruption):
    group = await members(db)
    await create(db, group)
    await report(db, group[0])
    close_time(monkeypatch)
    preview = await comp.finalize_campaign("pilot-fixture")
    saved = await comp.finalize_campaign("pilot-fixture", apply=True, expected_digest=preview["digest"])
    async with db() as session:
        if corruption == "manifest":
            await session.execute(sa.update(tables.validator_compensation_campaigns).values(result={**saved, "reviewed_units": 999}))
        else:
            changed = {"amount_atomic": 1} if corruption == "amount" else {"account_id": group[1][1]}
            await session.execute(sa.update(tables.validator_compensation_allocations).values(**changed))
        await session.commit()
    with pytest.raises(comp.CompensationError):
        await comp.finalize_campaign("pilot-fixture")


async def test_twenty_finalizers_allocate_once_under_observed_postgres_contention(db, monkeypatch):
    group = await members(db)
    await create(db, group)
    for member in group:
        await report(db, member)
    close_time(monkeypatch)
    preview = await comp.finalize_campaign("pilot-fixture")
    tasks = []
    try:
        async with db() as lock:
            await comp._lock(lock)
            pid = await lock.scalar(sa.text("SELECT pg_backend_pid()"))
            tasks = [
                asyncio.create_task(
                    comp.finalize_campaign(
                        "pilot-fixture",
                        apply=True,
                        expected_digest=preview["digest"],
                    ),
                )
                for _ in range(20)
            ]
            async with asyncio.timeout(15):
                async with db() as observer:
                    while True:
                        blocked = await observer.scalar(
                            sa.text(
                                "SELECT count(*) FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))",
                            ),
                            {"pid": pid},
                        )
                        if blocked >= 2:
                            break
                        assert not any(task.done() for task in tasks)
                        await observer.rollback()
                        await asyncio.sleep(0.01)
            await lock.rollback()
        async with asyncio.timeout(30):
            results = await asyncio.gather(*tasks)
        assert all(result == results[0] for result in results)
        async with db() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(tables.validator_compensation_allocations)) == 3
            assert await session.scalar(sa.select(sa.func.count()).select_from(tables.validator_compensation_work)) == 3
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real SQL cleanup; synthetic S3/chain, no actual payment or privacy proof."""

import asyncio
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from grid_api import config, database, main
from grid_api.config import GridSettings
from grid_api.services import video_quote_staging as staging
from grid_api.services.tests import test_video_payment_attempts as attempt_tests
from grid_api.services.tests import test_video_quote_staging as stage_tests
from grid_api.v2.schema import video_quote_staging as stages

db = stage_tests.db
admission = stage_tests.admission
registry = stage_tests.registry
synthetic_dependencies = stage_tests.synthetic_dependencies
source_store = stage_tests.source_store
pytestmark = pytest.mark.asyncio


def after_grace(quote, row):
    return max(staging.quotes._aware(quote["expires"]), staging.quotes._aware(row["lease_until"])
               + timedelta(seconds=staging.CLEANUP_GRACE_SECONDS)) + timedelta(seconds=1)


async def ready():
    owner, quote, plan = await stage_tests.saved()
    row = await staging.stage(owner, quote["id"], plan, **stage_tests.policy())
    return owner, quote, plan, row


@pytest.mark.parametrize("state", ["staging", "ready"])
async def test_expired_unpaid_inputs_are_irreversibly_abandoned_and_late_writes_revisited(
        db, admission, source_store, monkeypatch, state):
    owner, quote, plan = await stage_tests.saved()
    if state == "ready":
        original = await staging.stage(owner, quote["id"], plan, **stage_tests.policy())
    else:
        original, _ = await staging._allocate(owner, quote["id"], plan, stage_tests.policy()["admission_until"])
        # Explicitly simulate an interrupted upload into its already recorded key.
        source_store.objects[original["manifest"]["sources"][0]["key"]] = plan.inputs.sources[0].data
    now = after_grace(quote, original)
    monkeypatch.setattr(staging, "_now", lambda: now)
    counts = await staging.sweep_unpaid()
    assert counts == {"abandoned": 1, "acknowledged": 1, "errors": 0}
    observed = await staging.lookup(owner, quote["id"])
    assert observed["status"] == "abandoned" and observed["abandoned_at"] == now
    assert observed["manifest"] == original["manifest"] and observed["proof"] == original["proof"]
    assert observed["cleanup_ack_at"] == now and not source_store.objects
    assert await staging.sweep_unpaid() == {"abandoned": 0, "acknowledged": 0, "errors": 0}
    with pytest.raises(staging.VideoStageError):
        await staging.dispatch_payload(owner, quote["id"])
    with pytest.raises(staging.VideoStageError, match="closed"):
        await staging.stage(owner, quote["id"], plan, admission_until=now + timedelta(minutes=1))
    with pytest.raises(staging.VideoStageError, match="abandoned"):
        await staging._finish(owner, quote["id"], original["lease_token"], original["proof"])
    key = observed["manifest"]["sources"][0]["key"]
    source_store.objects[key] = b"synthetic late producer"
    now += timedelta(seconds=61)
    assert await staging.sweep_unpaid() == {"abandoned": 1, "acknowledged": 1, "errors": 0}
    assert not source_store.objects and source_store.deleted.count(key) == 2
    assert (await staging.lookup(owner, quote["id"]))["abandoned_at"] == observed["abandoned_at"]
    await attempt_tests.no_economic_effects()


@pytest.mark.parametrize("phase", ["quoted", "lease_grace"])
async def test_fresh_or_recently_leased_inputs_cannot_be_abandoned(db, admission, source_store, monkeypatch, phase):
    owner, quote, _plan, row = await ready()
    if phase == "lease_grace":
        monkeypatch.setattr(staging, "_now", lambda: after_grace(quote, row) - timedelta(seconds=2))
    assert await staging.sweep_unpaid() == {"abandoned": 0, "acknowledged": 0, "errors": 0}
    assert await staging.lookup(owner, quote["id"]) == row
    assert not source_store.deleted


@pytest.mark.parametrize("state", ["settling", "reported", "proven", "manual_review"])
async def test_every_payment_attempt_excludes_its_sources_even_if_expired_or_uncertain(
        db, admission, source_store, monkeypatch, state):
    owner, quote, signer, payload = await attempt_tests.saved(mode="audio")
    await attempt_tests.begin(owner, quote, signer, payload)
    if state != "settling":
        await attempt_tests.journal.report(owner, quote["id"], attempt_tests.proof_tests._TX)
    if state in {"proven", "manual_review"}:
        first, second = (attempt_tests.proof_tests.RPCFixture(quote, signer), attempt_tests.proof_tests.RPCFixture(quote, signer))
        if state == "manual_review":
            first.receipt["status"] = second.receipt["status"] = "0x0"
        async with attempt_tests.proof_tests.sources(first, second) as sources:
            assert (await attempt_tests.journal.reconcile(owner, quote["id"], sources=sources))["status"] == state
    original = await staging.lookup(owner, quote["id"])
    monkeypatch.setattr(staging, "_now", lambda: after_grace(quote, original))
    assert await staging.sweep_unpaid() == {"abandoned": 0, "acknowledged": 0, "errors": 0}
    assert await staging.lookup(owner, quote["id"]) == original
    assert source_store.objects and not source_store.deleted
    await attempt_tests.no_economic_effects()


async def test_attempt_between_scan_and_abandon_is_rechecked_under_the_quote_lock(db, admission, source_store, monkeypatch):
    from eth_account import Account

    owner, quote, _plan, row = await ready()
    original = staging._abandon
    signer = Account.create()
    payload = attempt_tests.proof_tests.binding_tests.sign(quote, signer)
    monkeypatch.setattr(staging, "_now", lambda: after_grace(quote, row))

    async def concurrent_attempt(account_id, quote_id):
        # Independently model the race; money-path time is not moved by this
        # sweeper-clock fixture, and the real begin/locks still run.
        _attempt, winner = await attempt_tests.begin(owner, quote, signer, payload)
        assert winner
        return await original(account_id, quote_id)

    monkeypatch.setattr(staging, "_abandon", concurrent_attempt)
    assert await staging.sweep_unpaid() == {"abandoned": 0, "acknowledged": 0, "errors": 0}
    assert (await staging.lookup(owner, quote["id"]))["status"] == "ready"
    assert source_store.objects and not source_store.deleted


async def test_corrupt_authority_rotates_without_deleting_and_does_not_starve_a_valid_row(
        db, admission, source_store, monkeypatch):
    first_owner, first_quote, _plan, first = await ready()
    second_owner, second_quote, _plan, second = await ready()
    async with await database.new_session() as session:
        await session.execute(stages.update().where(stages.c.quote_id == first_quote["id"]).values(manifest_hash="f" * 64))
        await session.commit()
    now = max(after_grace(first_quote, first), after_grace(second_quote, second))
    monkeypatch.setattr(staging, "_now", lambda: now)
    assert await staging.sweep_unpaid(limit=1) == {"abandoned": 0, "acknowledged": 0, "errors": 1}
    assert not source_store.deleted
    assert await staging.sweep_unpaid(limit=1) == {"abandoned": 1, "acknowledged": 1, "errors": 0}
    assert (await staging.lookup(second_owner, second_quote["id"]))["status"] == "abandoned"
    assert len(source_store.objects) == 1


async def test_storage_failure_retains_abandonment_and_retries_without_unclosing(db, admission, source_store, monkeypatch):
    owner, quote, _plan, row = await ready()
    now = after_grace(quote, row)
    monkeypatch.setattr(staging, "_now", lambda: now)
    source_store.failure = "delete"
    assert await staging.sweep_unpaid() == {"abandoned": 1, "acknowledged": 0, "errors": 1}
    first = await staging.lookup(owner, quote["id"])
    assert first["status"] == "abandoned" and first["cleanup_ack_at"] is None and source_store.objects
    source_store.failure = None
    now += timedelta(seconds=61)
    assert await staging.sweep_unpaid() == {"abandoned": 1, "acknowledged": 1, "errors": 0}
    assert (await staging.lookup(owner, quote["id"]))["abandoned_at"] == first["abandoned_at"]
    assert not source_store.objects


async def test_cancellation_keeps_cleanup_guard_until_its_thread_finishes(db, admission, source_store, monkeypatch):
    owner, quote, _plan, row = await ready()
    monkeypatch.setattr(staging, "_now", lambda: after_grace(quote, row))
    entered, release = threading.Event(), threading.Event()
    delete = source_store.delete_object

    def hold(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return delete(**kwargs)

    monkeypatch.setattr(source_store, "delete_object", hold)
    task = asyncio.create_task(staging.sweep_unpaid())
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and staging._cleanup_lock.locked()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and staging._cleanup_lock.locked()
        assert await staging.sweep_unpaid() == {"abandoned": 0, "acknowledged": 0, "errors": 0}
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not staging._cleanup_lock.locked()
    assert (await staging.lookup(owner, quote["id"]))["cleanup_ack_at"] is None


async def test_migration_preserves_ready_history_and_refuses_cleanup_downgrade(db, admission, source_store, monkeypatch):
    owner, quote, _plan, original = await ready()
    revision = stage_tests.cleanup_migration()
    assert revision.down_revision == "0049"

    def roundtrip(connection):
        with Operations.context(MigrationContext.configure(connection)):
            revision.downgrade()
            revision.upgrade()
            inspector = sa.inspect(connection)
            assert {column["name"]: column["nullable"] for column in inspector.get_columns(stages.name)} == {
                column.name: column.nullable for column in stages.columns}
            assert {item["name"] for item in inspector.get_check_constraints(stages.name)} == {
                item.name for item in stages.constraints if isinstance(item, sa.CheckConstraint)}
            assert {item["name"] for item in inspector.get_indexes(stages.name)} == {index.name for index in stages.indexes}

    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(roundtrip)
    assert await staging.lookup(owner, quote["id"]) == original
    monkeypatch.setattr(staging, "_now", lambda: after_grace(quote, original))
    await staging.sweep_unpaid()

    def refuse(connection):
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="refuse to discard"):
            revision.downgrade()

    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(refuse)
    assert (await staging.lookup(owner, quote["id"]))["status"] == "abandoned"


async def test_disabled_background_loop_does_not_query_staging(monkeypatch):
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(video_unpaid_cleanup_enabled=False))
    sweep = AsyncMock(side_effect=AssertionError("disabled cleanup must not select optional tables"))
    monkeypatch.setattr(staging, "sweep_unpaid", sweep)
    await main._video_unpaid_input_sweeper()
    sweep.assert_not_awaited()


async def test_enabled_loop_recovers_after_failure_without_logging_private_values(monkeypatch, caplog):
    settings = SimpleNamespace(video_unpaid_cleanup_enabled=True, video_unpaid_cleanup_seconds=60)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    sweep = AsyncMock(side_effect=[RuntimeError("private source URL and SQL"),
        {"errors": 1}, {"errors": 0}])
    monkeypatch.setattr(staging, "sweep_unpaid", sweep)
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await main._video_unpaid_input_sweeper()
    assert sweep.await_count == 3 and sleep.await_count == 3
    assert "private source URL" not in caplog.text and "SQL" not in caplog.text


async def test_cleanup_settings_are_default_off_and_bounded():
    settings = GridSettings(_env_file=None)
    assert not settings.video_unpaid_cleanup_enabled and settings.video_unpaid_cleanup_seconds == 60
    for interval in (0, 29, 3601):
        with pytest.raises(ValueError):
            GridSettings(_env_file=None, video_unpaid_cleanup_seconds=interval)


async def test_recorded_private_bucket_survives_rotation_but_never_becomes_public_cleanup_authority(
        db, admission, source_store, monkeypatch):
    owner, quote, _plan, row = await ready()
    now = after_grace(quote, row)
    monkeypatch.setattr(staging, "_now", lambda: now)
    monkeypatch.setattr(staging.storage, "private_source_bucket", lambda: "new-synthetic-private")
    monkeypatch.setattr(staging.storage, "media_bucket", lambda: "synthetic-private-inputs")
    assert await staging.sweep_unpaid() == {"abandoned": 0, "acknowledged": 0, "errors": 1}
    assert not source_store.deleted
    monkeypatch.setattr(staging.storage, "media_bucket", lambda: "synthetic-public-output")
    now += timedelta(seconds=61)
    assert await staging.sweep_unpaid() == {"abandoned": 1, "acknowledged": 1, "errors": 0}
    assert not source_store.objects
    assert (await staging.lookup(owner, quote["id"]))["manifest"]["bucket"] == "synthetic-private-inputs"

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real SQL staging; synthetic source storage, no payment/GPU/R2 qualification."""

import asyncio
import copy
import importlib.util
import io
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import boto3
import pytest
import sqlalchemy as sa
from botocore.response import StreamingBody
from botocore.stub import Stubber

from alembic.migration import MigrationContext
from alembic.operations import Operations
from grid_api import database
from grid_api.routers import videos
from grid_api.services import media_source_retention as retention
from grid_api.services import storage, video_plans
from grid_api.services import video_quote_staging as staging
from grid_api.services import video_quotes as quotes
from grid_api.services.tests import test_video_payment_attempts as attempt_tests
from grid_api.services.tests import test_video_plans as plan_tests
from grid_api.services.tests import test_video_quotes as quote_tests
from grid_api.services.tests import video_staging_support as stage_support
from grid_api.v2.schema import account_aliases
from grid_api.v2.schema import video_quote_staging as stages

db = quote_tests.db
admission = quote_tests.admission
registry = quote_tests.registry
mp4 = quote_tests.mp4
synthetic_dependencies = quote_tests.synthetic_dependencies
source_store = stage_support.source_store
pytestmark = pytest.mark.asyncio


async def saved(*, mode="audio", video=None, pin=False):
    owner = await quote_tests.owner(0)
    inputs = await videos.validate_video(plan_tests.request(mode, video=video, pin=pin), {}, async_submission=True)
    plan = video_plans.compile_video(inputs)
    key = str(uuid4())
    prepared = quotes.prepare(account_id=owner, idempotency_key=key, request_hash=quote_tests.INTENT,
        plan=plan, max_cost_micro=100_000, server_max_micro=100_000, network="eip155:84532", pay_to=quote_tests.PAY_TO)
    row, _ = await quotes.persist(prepared, idempotency_key=key, **quote_tests.LIMITS)
    return owner, row, plan


def policy():
    return {"admission_until": datetime.now(UTC) + timedelta(minutes=2)}


@pytest.mark.parametrize("mode", ["generated", "audio", "video"])
@pytest.mark.parametrize("pin", [False, True])
async def test_exact_sources_persist_before_upload_and_recover_without_restaging(db, admission, mp4, source_store, mode, pin):
    owner, quote, plan = await saved(mode=mode, video=mp4, pin=pin)
    loop = asyncio.get_running_loop()

    def before_put(key):
        original = asyncio.run_coroutine_threadsafe(staging.lookup(owner, quote["id"]), loop).result(timeout=5)
        assert original["status"] == "staging"
        assert key in staging.authority(original["manifest"])["keys"]

    source_store.before_put = before_put
    ready = await staging.stage(owner, quote["id"], plan, **policy())
    assert ready["status"] == "ready" and ready == await staging.lookup(owner, quote["id"])
    assert "synthetic red ball" not in quotes._canonical(ready["manifest"])
    assert "data:" not in quotes._canonical(ready["manifest"]) and "https" not in quotes._canonical(ready["proof"])
    count = len(source_store.puts)
    assert count == {"generated": 0, "audio": 1, "video": 2}[mode] + int(pin)
    assert await staging.stage(owner, quote["id"], plan, **policy()) == ready
    first, authority = await staging.dispatch_payload(owner, quote["id"])
    second, same_authority = await staging.dispatch_payload(owner, quote["id"])
    assert authority == same_authority == staging.authority(ready["manifest"])
    assert first["seed"] == second["seed"] == 0
    location_keys = {"source_image_url", "source_image_urls", "source_media"}
    assert {key: value for key, value in first.items() if key not in location_keys} == {
        key: value for key, value in plan.payload.items() if key not in location_keys}
    assert source_store.puts == list(source_store.objects) and len(source_store.puts) == count
    assert all(body.closed for body in source_store.bodies)
    if mode != "generated":
        assert first["source_media"][0]["url"] != second["source_media"][0]["url"]
    await attempt_tests.no_economic_effects()
    assert not quotes.view(quote)["payment_enabled"]


@pytest.mark.parametrize("failure", ["put", "digest", "extra", "short", "length", "etag"])
async def test_partial_or_unproven_storage_never_becomes_ready(db, admission, source_store, failure):
    owner, quote, plan = await saved()
    source_store.failure = failure
    with pytest.raises(staging.VideoStageError) as error:
        await staging.stage(owner, quote["id"], plan, **policy())
    assert "credentials" not in str(error.value)
    original = await staging.lookup(owner, quote["id"])
    assert original["status"] == "staging" and original["proof"] is None
    with pytest.raises(staging.VideoStageError):
        await staging.dispatch_payload(owner, quote["id"])
    assert all(body.closed for body in source_store.bodies)
    await attempt_tests.no_economic_effects()


async def test_retry_after_storage_failure_keeps_recorded_keys_and_rejects_old_lease(db, admission, source_store, monkeypatch):
    owner, quote, plan = await saved()
    source_store.failure = "digest"
    with pytest.raises(staging.VideoStageError):
        await staging.stage(owner, quote["id"], plan, **policy())
    original = await staging.lookup(owner, quote["id"])
    source_store.failure = None
    later = quotes._aware(original["lease_until"]) + timedelta(seconds=1)
    monkeypatch.setattr(staging, "_now", lambda: later)
    ready = await staging.stage(owner, quote["id"], plan, admission_until=later + timedelta(seconds=30))
    assert ready["manifest"] == original["manifest"] and ready["lease_token"] != original["lease_token"]
    assert len(source_store.objects) == 1 and len(source_store.puts) == 2
    with pytest.raises(staging.VideoStageError, match="lease changed"):
        await staging._finish(owner, quote["id"], original["lease_token"], ready["proof"])
    assert await staging.lookup(owner, quote["id"]) == ready


@pytest.mark.parametrize("commit_number", [1, 2])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_database_reply_loss_does_not_erase_keys_or_repeat_ready_upload(
        db, admission, source_store, monkeypatch, commit_number, after_commit):
    owner, quote, plan = await saved()
    factory, commits = database.new_session, 0

    async def uncertain_session():
        session = await factory()
        commit = session.commit

        async def uncertain_commit():
            nonlocal commits
            commits += 1
            if commits == commit_number:
                if after_commit:
                    await commit()
                raise RuntimeError("private SQL and input location")
            await commit()

        session.commit = uncertain_commit
        return session

    monkeypatch.setattr(staging, "new_session", uncertain_session)
    with pytest.raises(staging.VideoStageError) as error:
        await staging.stage(owner, quote["id"], plan, **policy())
    assert "private SQL" not in str(error.value)
    monkeypatch.setattr(staging, "new_session", factory)
    original = await staging.lookup(owner, quote["id"])
    assert (original is not None) == (commit_number == 2 or after_commit)
    if commit_number == 1:
        assert not source_store.puts
    if commit_number == 2 and after_commit:
        assert original["status"] == "ready"
        assert await staging.stage(owner, quote["id"], plan, **policy()) == original
        assert len(source_store.puts) == 1
    await attempt_tests.no_economic_effects()


async def test_expired_lease_cannot_publish_ready_and_expired_quote_cannot_restage(db, admission, source_store, monkeypatch):
    owner, quote, plan = await saved()
    row, _ = await staging._allocate(owner, quote["id"], plan, policy()["admission_until"])
    proof = staging._upload(row, [source.data for source in plan.inputs.sources])
    monkeypatch.setattr(staging, "_now", lambda: quotes._aware(row["lease_until"]))
    with pytest.raises(staging.VideoStageError, match="lease expired"):
        await staging._finish(owner, quote["id"], row["lease_token"], proof)
    monkeypatch.setattr(staging, "_now", lambda: quotes._aware(quote["expires"]))
    with pytest.raises(staging.VideoStageError, match="closed"):
        await staging.stage(owner, quote["id"], plan, admission_until=quotes._aware(quote["expires"]) + timedelta(minutes=1))
    assert len(source_store.objects) == 1


async def test_foreign_missing_changed_plan_and_closed_policy_have_no_storage_effect(db, admission, source_store):
    owner, quote, plan = await saved()
    stranger = await quote_tests.owner(0)
    for principal, quote_id in ((stranger, quote["id"]), (stranger, uuid4())):
        with pytest.raises(staging.VideoStageError, match="private video input staging failed"):
            await staging.stage(principal, quote_id, plan, **policy())
    other_owner, _other_quote, other_plan = await saved(mode="generated")
    with pytest.raises(staging.VideoStageError, match="original video quote"):
        await staging.stage(owner, quote["id"], other_plan, **policy())
    with pytest.raises(staging.VideoStageError, match="closed"):
        await staging.stage(owner, quote["id"], plan, admission_until=datetime.now(UTC) - timedelta(seconds=1))
    assert not source_store.puts and await staging.lookup(owner, quote["id"]) is None


async def test_alias_recovery_preserves_original_sources_after_expiry(db, admission, source_store, monkeypatch):
    owner, quote, plan = await saved()
    ready = await staging.stage(owner, quote["id"], plan, **policy())
    target = await quote_tests.owner(0)
    async with await database.new_session() as session:
        await session.execute(account_aliases.insert().values(source_account_id=owner, canonical_account_id=target,
                                                             merge_ref="synthetic merge"))
        await session.commit()
    monkeypatch.setattr(staging, "_now", lambda: quotes._aware(quote["expires"]) + timedelta(days=1))
    assert await staging.lookup(target, quote["id"]) == ready
    assert await staging.stage(target, quote["id"], plan, admission_until=datetime.now(UTC) - timedelta(seconds=1)) == ready
    await staging.dispatch_payload(target, quote["id"])
    assert len(source_store.puts) == 1


@pytest.mark.parametrize("field", ["manifest", "proof", "storage_account", "output_bucket", "url"])
async def test_corrupt_or_rebound_storage_cannot_be_recovered(db, admission, source_store, monkeypatch, field):
    owner, quote, plan = await saved()
    ready = await staging.stage(owner, quote["id"], plan, **policy())
    if field in {"manifest", "proof"}:
        changed = copy.deepcopy(ready[field])
        if field == "manifest":
            changed["sources"][0]["commitment"]["sha256"] = "f" * 64
        else:
            changed["objects"][0]["bytes"] += 1
        async with await database.new_session() as session:
            await session.execute(stages.update().values(**{field: changed, field + "_hash": quotes._hash(changed)}))
            await session.commit()
    elif field == "storage_account":
        monkeypatch.setenv("R2_TRANSIENT_ACCOUNT", "https://" + "b" * 32 + ".r2.cloudflarestorage.com")
    elif field == "output_bucket":
        monkeypatch.setattr(storage, "media_bucket", lambda: "synthetic-private-inputs")
    else:
        monkeypatch.setattr(source_store, "generate_presigned_url", lambda *_args, **_kwargs: "https://other.example/unsafe")
    with pytest.raises(staging.VideoStageError):
        await staging.dispatch_payload(owner, quote["id"])


async def test_cancellation_keeps_storage_slot_until_owned_thread_finishes(db, admission, source_store):
    owner, quote, plan = await saved()
    started, release = threading.Event(), threading.Event()

    def hold(_key):
        started.set()
        assert release.wait(timeout=5)

    source_store.before_put = hold
    task = asyncio.create_task(staging.stage(owner, quote["id"], plan, **policy()))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and quote["id"] in staging._active
        with pytest.raises(staging.VideoStageBusy):
            await staging.stage(owner, quote["id"], plan, **policy())
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert quote["id"] not in staging._active
    assert (await staging.lookup(owner, quote["id"]))["status"] == "staging"


async def test_real_postgres_duplicate_stagers_allocate_one_manifest(db, admission, source_store):
    if db[1] != "postgres":
        pytest.skip("real staging concurrency requires PostgreSQL")
    owner, quote, plan = await saved()

    async def racer():
        try:
            return await staging._allocate(owner, quote["id"], plan, policy()["admission_until"])
        except staging.VideoStageBusy:
            return None

    outcomes = await asyncio.gather(*(racer() for _ in range(25)))
    assert sum(outcome is not None and outcome[1] for outcome in outcomes) == 1
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(stages)) == 1
    assert not source_store.puts


async def test_first_payment_attempt_requires_actual_ready_staging(db, admission, source_store):
    from eth_account import Account

    owner, quote, plan = await saved()
    signer = Account.create()
    payload = attempt_tests.proof_tests.binding_tests.sign(quote, signer)
    with pytest.raises(attempt_tests.journal.VideoAttemptError):
        await attempt_tests.begin(owner, quote, signer, payload)
    assert await attempt_tests.journal.lookup(owner, quote["id"]) is None
    source_store.failure = "digest"
    with pytest.raises(staging.VideoStageError):
        await staging.stage(owner, quote["id"], plan, **policy())
    with pytest.raises(attempt_tests.journal.VideoAttemptError):
        await attempt_tests.begin(owner, quote, signer, payload)
    assert await attempt_tests.journal.lookup(owner, quote["id"]) is None
    await attempt_tests.no_economic_effects()


async def test_verified_stage_then_payment_attempt_and_observation_only_recovery(db, admission, source_store):
    from eth_account import Account

    owner, quote, plan = await saved()
    ready = await staging.stage(owner, quote["id"], plan, **policy())
    signer = Account.create()
    payload = attempt_tests.proof_tests.binding_tests.sign(quote, signer)
    first, winner = await attempt_tests.begin(owner, quote, signer, payload)
    assert winner and first["status"] == "settling"
    assert await staging.lookup(owner, quote["id"]) == ready
    # Explicitly model a pre-0049 attempt lacking its staging row in this scratch DB.
    async with await database.new_session() as session:
        await session.execute(stages.delete())
        await session.commit()
    replay, winner = await attempt_tests.begin(owner, quote, signer, payload)
    assert not winner and replay == first
    with pytest.raises(staging.VideoStageError, match="closed"):
        await staging.stage(owner, quote["id"], plan, **policy())
    assert len(source_store.puts) == 1
    await attempt_tests.no_economic_effects()


async def test_partial_multi_source_upload_retains_all_keys_but_grants_no_send(db, admission, source_store, monkeypatch):
    owner, quote, plan = await saved(pin=True)
    def fail_second(_key):
        if source_store.puts:
            raise RuntimeError("synthetic second-source failure")

    source_store.before_put = fail_second
    with pytest.raises(staging.VideoStageError):
        await staging.stage(owner, quote["id"], plan, **policy())
    initial = await staging.lookup(owner, quote["id"])
    assert len(staging.authority(initial["manifest"])["keys"]) == 2 and len(source_store.objects) == 1
    source_store.before_put = None
    later = quotes._aware(initial["lease_until"]) + timedelta(seconds=1)
    monkeypatch.setattr(staging, "_now", lambda: later)
    ready = await staging.stage(owner, quote["id"], plan, admission_until=later + timedelta(seconds=30))
    assert ready["manifest"] == initial["manifest"] and len(source_store.objects) == 2


async def test_whole_storage_deadline_prevents_upload_and_ready_state(db, admission, source_store, monkeypatch):
    owner, quote, plan = await saved()
    ticks = iter((0, staging.STORAGE_SECONDS + 1))
    monkeypatch.setattr(staging, "monotonic", lambda: next(ticks))
    with pytest.raises(staging.VideoStageError, match="storage deadline"):
        await staging.stage(owner, quote["id"], plan, **policy())
    assert not source_store.puts
    assert (await staging.lookup(owner, quote["id"]))["status"] == "staging"


@pytest.mark.parametrize("already_present", [False, True])
async def test_actual_botocore_model_accepts_conditional_put_and_streamed_readback(
        db, admission, source_store, monkeypatch, already_present):
    owner, quote, plan = await saved()
    row, _ = await staging._allocate(owner, quote["id"], plan, policy()["admission_until"])
    client = boto3.client("s3", endpoint_url=stage_support.ENDPOINT, region_name="auto",
                          aws_access_key_id="synthetic-access", aws_secret_access_key="synthetic-secret")
    item = row["manifest"]["sources"][0]
    raw = plan.inputs.sources[0].data
    put = {"Bucket": "synthetic-private-inputs", "Key": item["key"], "Body": raw,
           "ContentType": "audio/wav", "IfNoneMatch": "*"}
    body = StreamingBody(io.BytesIO(raw), len(raw))
    try:
        with Stubber(client) as stub:
            if already_present:
                stub.add_client_error("put_object", service_error_code="PreconditionFailed", http_status_code=412,
                                      expected_params=put)
            else:
                stub.add_response("put_object", {}, put)
            stub.add_response("get_object", {"Body": body, "ContentLength": len(raw), "ETag": '"sdk-object"',
                "ResponseMetadata": {"HTTPStatusCode": 200}}, {"Bucket": "synthetic-private-inputs", "Key": item["key"]})
            monkeypatch.setattr(retention, "_client", lambda _endpoint: client)
            proof = staging._upload(row, [raw])
            assert proof["objects"][0]["sha256"] == item["commitment"]["sha256"]
            stub.assert_no_pending_responses()
        assert body._raw_stream.closed
    finally:
        body.close()
        client.close()


async def test_postgres_waiting_stager_rechecks_quote_expiry_before_storage(db, admission, source_store, monkeypatch):
    if db[1] != "postgres":
        pytest.skip("lock observation requires PostgreSQL")
    owner, quote, plan = await saved()
    holder, task = await database.new_session(), None
    try:
        await holder.execute(sa.select(quotes.video_quotes.c.id).where(quotes.video_quotes.c.id == quote["id"]).with_for_update())
        pid = await holder.scalar(sa.text("SELECT pg_backend_pid()"))
        task = asyncio.create_task(staging.stage(owner, quote["id"], plan,
            admission_until=quotes._aware(quote["expires"]) + timedelta(minutes=1)))
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
                pytest.fail("the owned stager did not block on the held quote lock")
        monkeypatch.setattr(staging, "_now", lambda: quotes._aware(quote["expires"]))
        await holder.rollback()
        with pytest.raises(staging.VideoStageError, match="closed"):
            await asyncio.wait_for(task, 5)
    finally:
        await holder.close()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert not source_store.puts and await staging.lookup(owner, quote["id"]) is None


def migration():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0049_video_quote_staging.py"
    spec = importlib.util.spec_from_file_location("video_stage_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


async def test_migration_parity_empty_downgrade_and_refusal_after_allocation(db, admission, source_store):
    revision = migration()
    assert revision.down_revision == "0048"

    def apply(connection):
        stages.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            inspector = sa.inspect(connection)
            assert {column["name"]: column["nullable"] for column in inspector.get_columns(stages.name)} == {
                column.name: column.nullable for column in stages.columns}
            assert {check["name"] for check in inspector.get_check_constraints(stages.name)} == {
                check.name for check in stages.constraints if isinstance(check, sa.CheckConstraint)}
            assert {item["referred_table"] for item in inspector.get_foreign_keys(stages.name)} == {"grid_video_quotes"}
            revision.downgrade()
            revision.upgrade()

    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(apply)
    owner, quote, plan = await saved()
    await staging._allocate(owner, quote["id"], plan, policy()["admission_until"])

    def refuse(connection):
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="refuse to discard"):
            revision.downgrade()

    async with db[0].kw["bind"].begin() as connection:
        await connection.run_sync(refuse)
    assert (await staging.lookup(owner, quote["id"]))["status"] == "staging"

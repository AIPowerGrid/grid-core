# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real scoped auth and SQL; recipe rates, worker/storage/quota are fixtures."""

import asyncio
import base64
import io
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from fastapi import FastAPI
from PIL import Image

from grid_api import auth, database
from grid_api.config import GridSettings
from grid_api.routers import video_jobs, videos, worker_ws
from grid_api.services import accounts, credits, media_jobs, recipes, video_plans
from grid_api.services.tests.test_video_sources import mp4 as mp4
from grid_api.services.tests.test_video_sources import wav
from grid_api.services.tests.test_media_job_journal import (
    MODEL,
    OPENING,
    counts,
    owner,
)
from grid_api.services.tests.test_media_job_journal import (
    db as db,
)
from grid_api.services.tests.test_media_job_journal import (
    synthetic_dependencies as synthetic_dependencies,
)
from grid_api.v2.schema import media_jobs as journal


@pytest_asyncio.fixture
async def api(db, monkeypatch):
    monkeypatch.setattr(auth, "_API_KEY_SALT", uuid4().hex)
    aid = await owner()
    key = await accounts.issue_key(aid, scopes=["inference.submit"])
    settings = SimpleNamespace(media_async_enabled=True, media_async_concurrency=4,
                               media_async_nvfp4_enabled=False,
                               media_async_admission_mode="open", media_async_canary_accounts=[],
                               media_async_canary_until=None,
                               media_async_source_admission_mode="open",
                               media_async_source_canary_accounts=[], media_async_source_canary_until=None)
    monkeypatch.setattr(video_jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(video_jobs.limiter, "enabled", False)
    available = AsyncMock(return_value=[MODEL])
    monkeypatch.setattr(videos, "get_available_models", available)
    monkeypatch.setattr(worker_ws, "get_available_models", available)
    monkeypatch.setattr(videos.generation_admission, "require_media", lambda *_args: None)
    monkeypatch.setattr(videos.storage, "private_source_bucket", lambda: "private-synthetic-source")
    monkeypatch.setenv("R2_TRANSIENT_ACCOUNT", "https://" + "a" * 32 + ".r2.cloudflarestorage.com")
    monkeypatch.setenv("R2_TRANSIENT_BUCKET", "synthetic-public-outputs")
    quota = AsyncMock()
    monkeypatch.setattr(videos.quota, "check_and_consume", quota)
    app = FastAPI()
    app.include_router(video_jobs.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, aid, key, settings, quota, available


async def post(api, *, key=None, body=None, credential=None):
    client, _aid, issued, *_rest = api
    return await client.post("/v1/videos/jobs", json={
        "prompt": "A synthetic red ball rolls on a table", "model": MODEL,
        "max_cost_micro_usd": 100_000, **(body or {}),
    }, headers={"apikey": credential or issued, "Idempotency-Key": str(key or uuid4())})


async def stored(job_id):
    async with await database.new_session() as session:
        return dict((await session.execute(sa.select(journal).where(journal.c.job_id == job_id))).mappings().one())


@pytest.mark.asyncio
async def test_explicit_public_nvfp4_rollout_keeps_one_hold_and_recovery_after_closure(api):
    client, aid, key, settings, quota, available = api
    settings.media_async_nvfp4_enabled = True
    available.return_value = ["LTX-2.5 Distilled NVFP4"]
    request_key = uuid4()
    body = {"model": "LTX-2.5 Distilled NVFP4", "frames": 81, "seed": 0}
    first = await post(api, key=request_key, body=body)
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]
    row = await stored(job_id)
    assert row["payload"]["recipe_spec"]["model"]["inputs"]["unet_name"].endswith("-nvfp4.safetensors")
    balance = await credits.get_balance(aid)
    settings.media_async_nvfp4_enabled = False
    replay = await post(api, key=request_key, body=body)
    assert replay.status_code == 202 and replay.json()["job_id"] == job_id
    recovered = await client.get(f"/v1/videos/jobs/{job_id}", headers={"apikey": key})
    assert recovered.status_code == 200 and recovered.json()["job_id"] == job_id
    assert await counts() == (1, 1, 0)
    assert await credits.get_balance(aid) == balance
    settings.media_async_nvfp4_enabled = True
    denied = await post(api, body={**body, "reference_audio": "private-invalid-source"})
    assert denied.status_code == 503
    assert await counts() == (1, 1, 0)


@pytest.mark.asyncio
async def test_pilot_rejects_fresh_keys_before_input_work_without_bill_or_quota(api, monkeypatch):
    _client, aid, _key, settings, quota, available = api
    settings.media_async_admission_mode = "pilot"
    settings.media_async_canary_accounts = [uuid4()]
    settings.media_async_canary_until = datetime.now(UTC) + timedelta(minutes=15)

    def forbidden(_body):
        pytest.fail("outside-cohort requests must not decode or hash private input")

    monkeypatch.setattr(video_jobs, "request_hash", forbidden)
    response = await post(api, body={"image": "invalid-inline-data"})
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert await counts() == (0, 0, 0) and await credits.get_balance(aid) == OPENING
    quota.assert_not_awaited()
    available.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_admission_rejects_unqualified_models_before_private_input_work(api, monkeypatch):
    _client, aid, _key, _settings, quota, available = api

    def forbidden(_body):
        pytest.fail("publicly blocked models must not decode private input")

    monkeypatch.setattr(video_jobs, "request_hash", forbidden)
    for model in ("LTX-2.5 Distilled NVFP4", "LTX-2.5 Dev BF16", "unknown-video"):
        response = await post(api, body={"model": model, "image": "invalid-inline-data"})
        assert response.status_code == 503, response.text
    assert await counts() == (0, 0, 0) and await credits.get_balance(aid) == OPENING
    quota.assert_not_awaited()
    available.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_cohort_rejects_before_hash_decode_quota_upload_or_hold(api, monkeypatch):
    _client, aid, _key, settings, quota, available = api
    settings.media_async_source_admission_mode = "pilot"
    settings.media_async_source_canary_accounts = [uuid4()]
    settings.media_async_source_canary_until = datetime.now(UTC)+timedelta(minutes=15)

    def forbidden(_body):
        pytest.fail("source admission must precede private input processing")

    monkeypatch.setattr(video_jobs, "request_hash", forbidden)
    for source in ({"reference_audio": "invalid"}, {"reference_video": "invalid"}):
        response = await post(api, body=source)
        assert response.status_code == 503
    assert await counts() == (0, 0, 0) and await credits.get_balance(aid) == OPENING
    quota.assert_not_awaited()
    available.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_bf16_only_policy_keeps_private_nvfp4_replay_and_get(api):
    client, aid, key, settings, quota, available = api
    settings.media_async_admission_mode = "pilot"
    settings.media_async_canary_accounts = [aid]
    settings.media_async_canary_until = datetime.now(UTC) + timedelta(minutes=15)
    available.return_value = ["LTX-2.5 Distilled NVFP4"]
    request_key = uuid4()
    body = {"model": "LTX-2.5 Distilled NVFP4"}
    accepted = await post(api, key=request_key, body=body)
    assert accepted.status_code == 202, accepted.text
    balance = await credits.get_balance(aid)
    settings.media_async_admission_mode = "open"
    settings.media_async_canary_accounts = []
    settings.media_async_canary_until = None
    replay = await post(api, key=request_key, body=body)
    assert replay.status_code == 202 and replay.json() == accepted.json()
    status = await client.get(accepted.headers["location"], headers={"apikey": key})
    assert status.status_code == 200 and status.json() == accepted.json()
    assert (await post(api, body=body)).status_code == 503
    assert await counts() == (1, 1, 0) and await credits.get_balance(aid) == balance
    quota.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_removed_pilot_retains_replay_and_get_recovery(api):
    client, aid, key, settings, quota, _available = api
    settings.media_async_admission_mode = "pilot"
    settings.media_async_canary_accounts = [aid]
    settings.media_async_canary_until = datetime.now(UTC) + timedelta(minutes=15)
    request_key = uuid4()
    accepted = await post(api, key=request_key)
    assert accepted.status_code == 202, accepted.text
    balance = await credits.get_balance(aid)
    settings.media_async_canary_until = datetime.now(UTC) - timedelta(seconds=1)
    settings.media_async_canary_accounts = []
    assert (await post(api)).status_code == 503
    replay = await post(api, key=request_key)
    assert replay.status_code == 202 and replay.json() == accepted.json()
    status = await client.get(accepted.headers["location"], headers={"apikey": key})
    assert status.status_code == 200 and status.json() == accepted.json()
    changed = await post(api, key=request_key, body={"prompt": "a different synthetic intent"})
    assert changed.status_code == 409
    assert await counts() == (1, 1, 0) and await credits.get_balance(aid) == balance
    quota.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("closure", ["expiry", "removed", "disabled"])
async def test_pilot_rechecked_after_staging_without_new_hold(api, monkeypatch, closure):
    _client, aid, _key, settings, _quota, _available = api
    settings.media_async_admission_mode = "pilot"
    settings.media_async_canary_accounts = [aid]
    settings.media_async_canary_until = datetime.now(UTC) + timedelta(minutes=15)
    stage = video_plans.FrozenVideoPlan.stage

    async def staged(plan):
        result = await stage(plan)
        if closure == "expiry":
            settings.media_async_canary_until = datetime.now(UTC) - timedelta(seconds=1)
        elif closure == "removed":
            settings.media_async_canary_accounts = []
        else:
            settings.media_async_enabled = False
        return result

    monkeypatch.setattr(video_plans.FrozenVideoPlan, "stage", staged)
    response = await post(api)
    assert response.status_code == 503, response.text
    assert await counts() == (0, 0, 0) and await credits.get_balance(aid) == OPENING


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["extend", "shorten", "open"])
async def test_captured_pilot_deadline_can_only_be_shortened(api, monkeypatch, change):
    _client, aid, _key, settings, _quota, _available = api
    settings.media_async_admission_mode = "pilot"
    settings.media_async_canary_accounts = [aid]
    original_deadline = datetime.now(UTC) + timedelta(minutes=15)
    settings.media_async_canary_until = original_deadline
    stage = video_plans.FrozenVideoPlan.stage
    expected = original_deadline - timedelta(minutes=5) if change == "shorten" else original_deadline

    async def staged(plan):
        result = await stage(plan)
        if change == "open":
            settings.media_async_admission_mode = "open"
        else:
            settings.media_async_canary_until = expected if change == "shorten" else original_deadline + timedelta(hours=1)
        return result

    async def reserve(job, **_kwargs):
        assert job.admission_deadline == expected
        raise media_jobs.MediaJobAdmissionClosed("synthetic admission close")

    monkeypatch.setattr(video_plans.FrozenVideoPlan, "stage", staged)
    monkeypatch.setattr(media_jobs, "reserve", reserve)
    response = await post(api)
    assert response.status_code == 503
    assert await counts() == (0, 0, 0) and await credits.get_balance(aid) == OPENING


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["before_insert", "after_insert"])
async def test_expiry_inside_credit_transaction_rolls_back_debit_hold_and_job(api, monkeypatch, point):
    _client, aid, _key, settings, _quota, _available = api
    settings.media_async_admission_mode = "pilot"
    settings.media_async_canary_accounts = [aid]
    deadline = settings.media_async_canary_until = datetime.now(UTC) + timedelta(minutes=15)
    original = media_jobs.insert_in_session
    original_capture = media_jobs.media_source_retention.capture

    async def insert(session, job):
        from grid_api.v2.schema import credits as balances

        assert await session.scalar(sa.select(balances.c.balance_micro).where(
            balances.c.account_id == aid)) == OPENING - 67500
        if point == "before_insert":
            monkeypatch.setattr(media_jobs, "_now", lambda: deadline + timedelta(seconds=1))
        await original(session, job)

    def capture(payload):
        result = original_capture(payload)
        if point == "after_insert":
            monkeypatch.setattr(media_jobs, "_now", lambda: deadline + timedelta(seconds=1))
        return result

    monkeypatch.setattr(media_jobs, "insert_in_session", insert)
    monkeypatch.setattr(media_jobs.media_source_retention, "capture", capture)
    response = await post(api)
    assert response.status_code == 503, response.text
    assert await counts() == (0, 0, 0) and await credits.get_balance(aid) == OPENING
    from grid_api.v2.schema import credit_ledger
    from grid_api.v2.schema import credits as balances
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(balances.c.funded_balance_micro).where(
            balances.c.account_id == aid)) == OPENING
        assert await session.scalar(sa.select(sa.func.count()).select_from(credit_ledger).where(
            credit_ledger.c.reason == "reserve:video")) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["before_insert", "after_insert"])
async def test_source_expiry_inside_credit_transaction_rolls_back_all_writes(api, monkeypatch, point):
    _client, aid, _key, settings, _quota, _available = api
    settings.media_async_source_admission_mode = "pilot"
    settings.media_async_source_canary_accounts = [aid]
    deadline = settings.media_async_source_canary_until = datetime.now(UTC) + timedelta(minutes=15)
    monkeypatch.setattr(videos.storage, "upload_private_source", lambda *_args:
        "https://" + "a" * 32 + ".r2.cloudflarestorage.com/private-synthetic-source/source/"
        + "b" * 32 + ".wav?X-Amz-Signature=synthetic")
    original_insert = media_jobs.insert_in_session
    original_capture = media_jobs.media_source_retention.capture

    async def insert(session, job):
        assert job.admission_deadline == deadline
        if point == "before_insert":
            monkeypatch.setattr(media_jobs, "_now", lambda: deadline + timedelta(seconds=1))
        await original_insert(session, job)

    def capture(payload):
        result = original_capture(payload)
        if point == "after_insert":
            monkeypatch.setattr(media_jobs, "_now", lambda: deadline + timedelta(seconds=1))
        return result

    monkeypatch.setattr(media_jobs, "insert_in_session", insert)
    monkeypatch.setattr(media_jobs.media_source_retention, "capture", capture)
    response = await post(api, body={"frames": 33, "size": "256x256",
        "reference_audio": base64.b64encode(wav(33)).decode("ascii")})
    assert response.status_code == 503, response.text
    assert await counts() == (0, 0, 0) and await credits.get_balance(aid) == OPENING
    from grid_api.v2.schema import credit_ledger
    from grid_api.v2.schema import credits as balances
    async with await database.new_session() as session:
        assert await session.scalar(sa.select(balances.c.funded_balance_micro).where(
            balances.c.account_id == aid)) == OPENING
        assert await session.scalar(sa.select(sa.func.count()).select_from(credit_ledger).where(
            credit_ledger.c.reason == "reserve:video")) == 0


@pytest.mark.asyncio
async def test_submit_is_reserved_promptly_and_retry_is_not_new_quota_or_seed(api, monkeypatch):
    client, aid, credential, settings, quota, available = api
    key = uuid4()
    first = await post(api, key=key)
    assert first.status_code == 202, first.text
    data = first.json()
    assert data["schema"] == "aipg.video-job.v1" and data["state"] == "pending_dispatch"
    assert data["reserved_micro"] == 67_500 and data["max_cost_micro"] == 100_000
    assert first.headers["cache-control"] == "no-store" and first.headers["retry-after"] == "5"
    row = await stored(data["job_id"])
    assert row["payload"]["recipe_spec"] and row["payload"]["recipe_root"]
    assert row["payload"]["required_media_features"] == ["async-video-resume-v1"]
    assert "payload" not in data and "account_id" not in data
    assert await credits.get_balance(aid) == OPENING - 67_500
    available.assert_awaited_once_with(job_type="video", media_features=("async-video-resume-v1",))
    # A rollback closes new admission, not recovery; price/recipe availability
    # and free mode must not invalidate an already accepted request.
    settings.media_async_enabled = False
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    recipes._BY_MODEL.clear()
    second = await post(api, key=key)
    assert second.status_code == 202 and second.json()["job_id"] == data["job_id"]
    assert (await stored(data["job_id"]))["payload"] == row["payload"]
    assert await counts() == (1, 1, 0)
    quota.assert_awaited_once()
    status = await client.get(first.headers["location"], headers={"apikey": credential})
    assert status.json()["job_id"] == data["job_id"] and status.headers["cache-control"] == "no-store"
    assert (await post(api)).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"prompt": "different"}, {"seed": 2}, {"max_cost_micro_usd": 90_000}, {"frames": 89}])
async def test_changed_intent_conflicts_without_second_money_or_quota(api, change):
    key = uuid4()
    assert (await post(api, key=key)).status_code == 202
    conflict = await post(api, key=key, body=change)
    assert conflict.status_code == 409 and conflict.headers["cache-control"] == "no-store"
    assert await counts() == (1, 1, 0)
    api[4].assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_reservation_creates_no_async_work(api, monkeypatch):
    for change in ({"max_cost_micro_usd": 1},):
        response = await post(api, body=change)
        assert response.status_code == 402, response.text
        assert await counts() == (0, 0, 0)
    monkeypatch.setattr(credits, "_CHARGING_MODE_ENV", "off")
    assert (await post(api)).status_code == 402
    assert await counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_only_owner_with_inference_scope_can_recover(api):
    client, aid, credential, *_ = api
    response = await post(api)
    url = response.headers["location"]
    stranger = await owner()
    stranger_key = await accounts.issue_key(stranger, scopes=["inference.submit"])
    assert (await client.get(url, headers={"apikey": stranger_key})).status_code == 404
    observer = await accounts.issue_key(aid, scopes=["account.read"])
    assert (await client.get(url, headers={"apikey": observer})).status_code == 403
    assert (await post(api, credential=observer)).status_code == 403
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers={"apikey": credential})).status_code == 200


@pytest.mark.asyncio
async def test_completed_result_recovers_after_admission_rollback(api):
    client, aid, credential, settings, *_ = api
    key = uuid4()
    response = await post(api, key=key, body={"seed": 0})
    row = await stored(response.json()["job_id"])
    worker = uuid4()
    token = str(uuid4())
    assert await media_jobs.claim_execution(job_id=row["job_id"], payload=row["payload"],
        route_models=row["route_models"], preferred_worker="", selected_model=MODEL,
        worker_id=worker, job_type="video", execution_token=token) == "claimed"
    result = {"media": [{"url": "https://media.invalid/video.mp4", "key": "synthetic/video.mp4",
                         "sha256": "a" * 64, "seed": 0}], "model": MODEL,
              "worker": "synthetic-worker", "gen_time": 1.0, "recipe_root": row["payload"]["recipe_root"]}
    values = dict(job_id=row["job_id"], worker_id=str(worker), wallet="", model=MODEL,
                  job_type="video", den=1.0, output_units=81, prompt_hash="b" * 64, result_hash="c" * 64)
    assert await credits.record_and_settle(ledger_values=values, exact=True, media_result=result,
                                          media_execution_token=token) == "settled"
    settings.media_async_enabled = False
    recovered = await client.get(response.headers["location"], headers={"apikey": credential})
    assert recovered.json()["state"] == "completed" and recovered.json()["result"] == result
    assert recovered.json()["actual_micro"] == 67_500
    replay = await post(api, key=key, body={"seed": 0})
    assert replay.status_code == 200 and replay.json()["result"] == result
    assert await counts() == (1, 1, 1)


@pytest.mark.asyncio
async def test_lost_commit_response_recovers_original_request_not_another_debit(api, monkeypatch):
    original = media_jobs.reserve
    committed = []

    async def lose_reply(*args, **kwargs):
        committed.append(await original(*args, **kwargs))
        raise ConnectionError("synthetic lost SQL response")

    monkeypatch.setattr(media_jobs, "reserve", lose_reply)
    key = uuid4()
    assert (await post(api, key=key)).status_code == 503
    retry = await post(api, key=key)
    assert retry.status_code == 202 and retry.json()["job_id"] == committed[0]["job_id"]
    assert len(committed) == 1 and await counts() == (1, 1, 0)


@pytest.mark.asyncio
async def test_lost_reply_recovers_by_key_with_no_post_upload_quota_or_second_hold(api):
    client, aid, credential, settings, quota, available = api
    key = uuid4()
    original = await post(api, key=key)
    row = await stored(original.json()["job_id"])
    balance = await credits.get_balance(aid)
    settings.media_async_enabled = False
    available.reset_mock()
    recovered = await client.get("/v1/videos/jobs", headers={"apikey": credential, "Idempotency-Key": str(key)})
    assert recovered.status_code == 200
    assert recovered.json() == original.json()
    assert recovered.json()["request_hash"] == row["request_hash"]
    assert recovered.headers["cache-control"] == "no-store"
    assert "payload" not in recovered.json() and "idempotency_hash" not in recovered.json()
    assert await counts() == (1, 1, 0) and await credits.get_balance(aid) == balance
    available.assert_not_awaited()
    quota.assert_awaited_once()


@pytest.mark.asyncio
async def test_key_recovery_is_scoped_and_not_a_bearer_capability(api):
    client, aid, credential, *_ = api
    key = uuid4()
    original = await post(api, key=key)
    observer = await accounts.issue_key(aid, scopes=["account.read"])
    stranger = await accounts.issue_key(await owner(), scopes=["inference.submit"])
    for auth_key, status in ((None, 401), (observer, 403), (stranger, 404), (credential, 200)):
        headers = {"Idempotency-Key": str(key), **({"apikey": auth_key} if auth_key else {})}
        result = await client.get("/v1/videos/jobs", headers=headers)
        assert result.status_code == status
        assert result.headers["cache-control"] == "no-store"
        if status != 200:
            assert original.json()["job_id"] not in result.text
    missing = await client.get("/v1/videos/jobs", headers={"apikey": credential, "Idempotency-Key": str(uuid4())})
    assert missing.status_code == 404
    assert await counts() == (1, 1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, "private-marker", "0" * 65, "00000000-0000-1000-8000-000000000000"])
async def test_key_recovery_rejects_missing_malformed_and_unbounded_keys_without_echo(api, key):
    client, _, credential, *_ = api
    response = await client.get("/v1/videos/jobs", headers={
        "apikey": credential, **({"Idempotency-Key": key} if key is not None else {}),
    })
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert "private-marker" not in response.text
    assert await counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_key_recovery_database_failure_is_private(api, monkeypatch):
    client, _, credential, *_ = api
    monkeypatch.setattr(media_jobs, "recover", AsyncMock(side_effect=RuntimeError("private-marker")))
    response = await client.get("/v1/videos/jobs", headers={"apikey": credential, "Idempotency-Key": str(uuid4())})
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert "private-marker" not in response.text
    assert await counts() == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"seed": True}, {"n": True}, {"n": 2}, {"audio": False},
    {"response_format": "b64_json"}, {"recipe_spec": {"secret": "private-marker"}},
    {"keyframes": [{"image": "private-marker", "frame": 80}]}])
async def test_invalid_typed_inputs_reject_without_admission_or_echoing_input(api, body):
    response = await post(api, body=body)
    assert response.status_code in {400, 422}, response.text
    assert "private-marker" not in response.text
    assert await counts() == (0, 0, 0)
    api[4].assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_key_and_non_resume_worker_reject_before_quota(api):
    client, _aid, credential, _settings, quota, available = api
    response = await client.post("/v1/videos/jobs", json={"prompt": "x", "max_cost_micro_usd": 100_000},
                                 headers={"apikey": credential})
    assert response.status_code == 422
    available.return_value = []
    assert (await post(api)).status_code == 503
    assert await counts() == (0, 0, 0)
    quota.assert_not_awaited()


@pytest.mark.asyncio
async def test_pg_concurrent_admission_cap_and_duplicate_requests(api, db):
    if db[1] != "postgres":
        pytest.skip("SQLite is not evidence for concurrent admission")
    api[3].media_async_concurrency = 2
    key = uuid4()
    same = await asyncio.wait_for(asyncio.gather(*(post(api, key=key) for _ in range(12))), timeout=30)
    assert all(item.status_code == 202 for item in same)
    assert len({item.json()["job_id"] for item in same}) == 1
    distinct = await asyncio.wait_for(asyncio.gather(*(post(api) for _ in range(12))), timeout=30)
    assert sum(item.status_code == 202 for item in distinct) == 1
    assert sum(item.status_code == 429 for item in distinct) == 11
    assert await counts() == (2, 2, 0)
    assert await credits.get_balance(api[1]) == OPENING - 2 * 67_500


@pytest.mark.asyncio
async def test_bounded_route_checks_stream_before_json_validation(api, monkeypatch):
    monkeypatch.setattr(video_jobs, "MAX_BODY_BYTES", 128)
    client, *_ = api

    async def chunks():
        yield b'{"prompt":"'
        yield b"x" * 100
        yield b"x" * 100
        pytest.fail("oversized stream must stop before this chunk")

    response = await client.post("/v1/videos/jobs", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413 and response.headers["cache-control"] == "no-store"
    assert (await client.post("/v1/videos/jobs", content=b"{}", headers={"Content-Type": "text/plain"})).status_code == 415
    assert await counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_deep_json_is_redacted_and_rejected_before_auth_or_upload(api):
    client, *_ = api
    response = await client.post("/v1/videos/jobs", content=b"[" * 2000 + b"0" + b"]" * 2000,
                                 headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert await counts() == (0, 0, 0)


def test_normalized_intent_hashes_image_bytes_and_preserves_size_semantics():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32)).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    original = video_jobs.AsyncVideoRequest(prompt="synthetic", max_cost_micro_usd=100_000, image=encoded)
    data_uri = original.model_copy(update={"image": "data:image/png;base64," + encoded})
    assert video_jobs.request_hash(original) == video_jobs.request_hash(data_uri)
    explicit = video_jobs.AsyncVideoRequest(**{**original.model_dump(), "size": "768x512"})
    assert video_jobs.request_hash(original) != video_jobs.request_hash(explicit)
    assert video_jobs.request_hash(original) != video_jobs.request_hash(original.model_copy(update={"seed": 0}))


@pytest.mark.parametrize("variant,cfg,digest", [
    ("Distilled", 1.0, "ff5564001dd3f129a4edc10db7d434d8805dc62b5ef71efae37654283f2d9bb4"),
    ("Dev", 3.0, "196b13865d0062b00c158e8d112c4eee379e02320592df9acf9032105a2fa94b"),
])
def test_normalized_intent_matches_director_v1_golden(variant, cfg, digest):
    body = video_jobs.AsyncVideoRequest(
        prompt="A synthetic red ball rolls on a table", model=f"LTX-2.5 {variant} BF16",
        size="256x256", frames=33, seed=42, steps=8 if variant == "Distilled" else 40,
        cfg_scale=cfg, max_cost_micro_usd=40_000,
    )
    assert video_jobs.request_hash(body) == digest


def test_audio_intent_matches_director_golden_and_commits_decoded_bytes():
    encoded = base64.b64encode(wav(33)).decode("ascii")
    body = video_jobs.AsyncVideoRequest(
        prompt="A synthetic red ball rolls on a table", model=MODEL,
        size="256x256", frames=33, seed=42, steps=8, cfg_scale=1.0,
        max_cost_micro_usd=40_000, reference_audio="data:audio/wav;base64," + encoded,
    )
    digest = video_jobs.request_hash(body)
    assert digest == "b2942c5edc0745627231ac72fe6e6c8e80b6931902914662feff54cc3d679804"
    assert video_jobs.request_hash(body.model_copy(update={"reference_audio": encoded})) == digest
    different = base64.b64encode(wav(33)[:-1] + b"\1").decode("ascii")
    assert video_jobs.request_hash(body.model_copy(update={"reference_audio": different})) != digest


@pytest.mark.asyncio
@pytest.mark.parametrize("closure", ["all_video", "source_only"])
async def test_audio_replay_keeps_one_hold_without_another_source_upload_or_quota(api, monkeypatch, closure):
    client, aid, credential, settings, quota, _available = api
    uploaded = []

    def source_upload(raw, extension):
        uploaded.append((raw, extension))
        return ("https://" + "a" * 32 + ".r2.cloudflarestorage.com/private-synthetic-source/source/"
                + "b" * 32 + ".wav?X-Amz-Signature=synthetic")

    monkeypatch.setattr(videos.video_sources.storage, "upload_private_source", source_upload)
    body = {"frames": 33, "size": "256x256", "seed": 42,
            "reference_audio": base64.b64encode(wav(33)).decode("ascii")}
    key = uuid4()
    first = await post(api, key=key, body=body)
    assert first.status_code == 202, first.text
    job = first.json()["job_id"]
    row = await stored(job)
    assert row["payload"]["recipe_inputs"]["source_mode"] == "audio"
    assert row["payload"]["recipe_source_bindings"] == [
        {"role": "reference_audio", "path": "source_audio.inputs.audio"}]
    assert row["payload"]["source_media"][0]["samples"] == 66_000
    assert row["source_objects"]["keys"] == ["source/" + "b" * 32 + ".wav"]
    assert row["source_cleanup_at"] is None
    assert row["payload"]["required_media_features"] == [
        "async-video-resume-v1", "recipe-media-sources-v1"]
    assert await credits.get_balance(aid) == OPENING - 27_500
    assert len(uploaded) == 1 and uploaded[0] == (wav(33), "wav")
    if closure == "all_video":
        settings.media_async_enabled = False
    else:
        settings.media_async_source_admission_mode = "off"
    quota.reset_mock()

    async def forbidden(*_args, **_kwargs):
        pytest.fail("accepted replay must not decode sources, upload or prepare again")

    monkeypatch.setattr(video_jobs, "validate_video", forbidden)
    replay = await post(api, key=key, body=body)
    assert replay.status_code == 202 and replay.json()["job_id"] == job
    assert len(uploaded) == 1 and quota.await_count == 0
    assert await counts() == (1, 1, 0)
    assert await credits.get_balance(aid) == OPENING - 27_500
    status = await client.get(first.headers["location"], headers={"apikey": credential})
    assert status.status_code == 200 and status.json()["job_id"] == job
    assert (await post(api, body=body)).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("source_mode", ["audio", "video"])
async def test_ingredients_intent_retains_distinct_sources_and_one_hold_after_rollback(api, monkeypatch, tmp_path, source_mode):
    from grid_api.services import pricing

    model = "LTX-2.5 Dev BF16 Ingredients"
    client, aid, credential, settings, quota, available = api
    settings.media_async_admission_mode = "pilot"
    settings.media_async_canary_accounts = [aid]
    settings.media_async_canary_until = datetime.now(UTC) + timedelta(minutes=15)
    available.return_value = [model]
    # Test-only tariff; the real appearance candidate stays unpriced/dark.
    monkeypatch.setitem(pricing.PRICING, model.lower(), pricing.ModelPrice(0, 0, video_per_second=0.02))
    uploaded = []

    def source_upload(raw, extension):
        uploaded.append((raw, extension))
        return ("https://" + "a" * 32 + ".r2.cloudflarestorage.com/private-synthetic-source/source/"
                + f"{len(uploaded):032x}.{extension}?X-Amz-Signature=synthetic")

    monkeypatch.setattr(videos.storage, "upload_private_source", source_upload)
    stream = io.BytesIO()
    Image.new("RGB", (768, 448), "red").save(stream, "PNG")
    body = {"model": model, "seed": 0, "max_cost_micro_usd": 150_000,
            "reference_image": base64.b64encode(stream.getvalue()).decode(),
            "reference_audio": base64.b64encode(wav(121)).decode()}
    roles = ["reference_image", "reference_audio"]
    if source_mode == "video":
        encoder = shutil.which("ffmpeg")
        if not encoder or not shutil.which("ffprobe"):
            pytest.skip("independent FFmpeg source-decoder gate unavailable")
        motion = tmp_path / "ingredients-motion.mp4"
        subprocess.run([encoder, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                        "testsrc2=size=768x448:rate=24", "-frames:v", "121", "-an",
                        "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(motion)],
                       capture_output=True, timeout=15, check=True)
        body.update(reference_video=base64.b64encode(motion.read_bytes()).decode(), size="768x448")
        roles.append("reference_video")
    key = uuid4()
    first = await post(api, key=key, body=body)
    assert first.status_code == 202, first.text
    job = first.json()["job_id"]
    row = await stored(job)
    payload = row["payload"]
    assert "source_image_url" not in payload
    assert payload["appearance_profile"] == "ingredients-dev-v1"
    assert [source["role"] for source in payload["source_media"]] == roles
    assert payload["recipe_spec"]["reference_guide"]["inputs"]["latent"] == ["latent_video", 0]
    assert len(row["source_objects"]["keys"]) == len(roles)
    if source_mode == "video":
        assert payload["recipe_spec"]["video_guide"]["inputs"]["positive"] == ["reference_guide", 0]
    assert "recipe-appearance-reference-v1" in payload["required_media_features"]
    assert first.json()["reserved_micro"] == 100_834
    assert await credits.get_balance(aid) == OPENING - 100_834
    assert len(uploaded) == len(roles)
    settings.media_async_enabled = False
    quota.reset_mock()
    monkeypatch.setattr(video_jobs, "validate_video", AsyncMock(side_effect=AssertionError("must not prepare accepted replay")))
    replay = await post(api, key=key, body=body)
    assert replay.status_code == 202 and replay.json()["job_id"] == job
    assert len(uploaded) == len(roles) and quota.await_count == 0
    assert await counts() == (1, 1, 0)
    conflict = await post(api, key=key, body={**body, "reference_strength": 0.5})
    assert conflict.status_code == 409
    assert await counts() == (1, 1, 0)
    recovered = await client.get(first.headers["location"], headers={"apikey": credential})
    assert recovered.status_code == 200 and recovered.json()["job_id"] == job


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["generated", "audio", "video"])
async def test_compilation_failure_rejects_before_quota_upload_or_hold(api, monkeypatch, mode, mp4):
    _client, aid, _credential, _settings, quota, _available = api
    before = await counts()
    balance = await credits.get_balance(aid)
    stream = io.BytesIO()
    Image.new("RGB", (256, 256), "red").save(stream, "PNG")
    body = {"frames": 9, "size": "256x256", "seed": 0,
            "keyframes": [{"image": base64.b64encode(stream.getvalue()).decode(), "frame": 0}]}
    if mode != "generated":
        body["reference_audio"] = base64.b64encode(wav(9)).decode()
    if mode == "video":
        body["reference_video"] = base64.b64encode(mp4).decode()

    def invalid(*_args, **_kwargs):
        raise recipes.RecipeError("Synthetic invalid governed scaffold")

    def forbidden(*_args, **_kwargs):
        pytest.fail("compilation failure must not upload a source")

    monkeypatch.setattr(recipes, "resolve_for_model", invalid)
    monkeypatch.setattr(videos.storage, "upload_private_source", forbidden)
    response = await post(api, body=body)
    assert response.status_code == 422, response.text
    quota.assert_not_awaited()
    assert await counts() == before
    assert await credits.get_balance(aid) == balance


@pytest.mark.asyncio
async def test_async_staging_does_not_resolve_recipe_after_compilation(api, monkeypatch):
    _client, _aid, _credential, _settings, _quota, _available = api
    calls = []
    original = video_plans.compile_video

    def compile_once(inputs):
        result = original(inputs)
        calls.append(result)
        # Any recipe selection after freezing would change what this admission
        # authorizes. Staging and reservation must consume only the snapshot.
        monkeypatch.setattr(recipes, "resolve_for_model",
                            lambda *_args, **_kwargs: pytest.fail("recipe re-resolved after planning"))
        return result

    monkeypatch.setattr(video_plans, "compile_video", compile_once)
    response = await post(api, body={"frames": 9, "seed": 0})
    assert response.status_code == 202, response.text
    assert len(calls) == 1
    row = await stored(response.json()["job_id"])
    assert row["payload"]["recipe_spec"] == calls[0].payload["recipe_spec"]
    assert row["payload"]["seed"] == 0
    assert await counts() == (1, 1, 0)


def test_async_settings_are_dark_and_bounded():
    assert not GridSettings(_env_file=None).media_async_enabled
    for field, value in (("media_async_concurrency", 0), ("media_job_batch_size", 51),
                         ("media_job_sweep_seconds", 0), ("media_job_dispatch_lease_seconds", 301)):
        with pytest.raises(ValueError):
            GridSettings(_env_file=None, **{field: value})


@pytest.mark.asyncio
async def test_outbox_loop_drains_after_admission_rollback_and_cancels_cleanly(monkeypatch, caplog):
    from grid_api import config, main

    observed = asyncio.Event()
    settings = SimpleNamespace(media_async_enabled=False, media_job_sweep_seconds=10,
                               media_job_batch_size=3, media_job_dispatch_lease_seconds=60)
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    async def dispatch(**kwargs):
        assert kwargs == {"limit": 3, "lease_seconds": 60}
        observed.set()
        raise RuntimeError("private-marker must not appear in logs")

    monkeypatch.setattr(media_jobs, "dispatch_batch", dispatch)
    task = asyncio.create_task(main._media_job_outbox())
    try:
        await asyncio.wait_for(observed.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "RuntimeError" in caplog.text and "private-marker" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("combined", [False, True])
async def test_all_media_features_must_exist_on_the_same_worker(monkeypatch, combined):
    workers = [
        {"job_types": ["video"], "models": [MODEL], "media_features": ["async-video-resume-v1"]},
        {"job_types": ["video"], "models": [MODEL], "media_features": ["recipe-image-bindings-v1"]},
    ]
    if combined:
        workers.append({"job_types": ["video"], "models": [MODEL],
                        "media_features": ["async-video-resume-v1", "recipe-image-bindings-v1"]})
    pipeline = SimpleNamespace(get=lambda _key: None,
                               execute=AsyncMock(return_value=[json.dumps(item) for item in workers]))
    redis = SimpleNamespace(smembers=AsyncMock(return_value=[str(i) for i in range(len(workers))]),
                            pipeline=lambda: pipeline)
    monkeypatch.setattr(worker_ws, "get_redis", lambda: redis)
    monkeypatch.setattr(recipes, "list_recipes", list)
    found = await worker_ws.get_available_models(job_type="video", media_features=(
        "async-video-resume-v1", "recipe-image-bindings-v1"))
    assert found == ([MODEL] if combined else [])


def test_async_ingress_has_a_typed_openapi_contract():
    app = FastAPI()
    app.include_router(video_jobs.router)
    spec = app.openapi()
    request = spec["paths"]["/v1/videos/jobs"]["post"]["requestBody"]
    assert request["content"]["application/json"]["schema"]["$ref"].endswith("/AsyncVideoRequest")
    schema = spec["components"]["schemas"]["AsyncVideoRequest"]
    assert schema["additionalProperties"] is False
    assert "max_cost_micro_usd" in schema["required"]

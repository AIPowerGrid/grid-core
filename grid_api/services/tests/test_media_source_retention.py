# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exact-key cleanup authority, real SQL closure, and hermetic storage failures."""

import asyncio
import threading
from unittest.mock import Mock
from uuid import uuid4

import pytest

from grid_api import database, main
from grid_api.services import credits
from grid_api.services import media_jobs as jobs
from grid_api.services import media_source_retention as retention
from grid_api.services.tests.test_media_job_journal import (
    claim,
    counts,
    owner,
    prepared,
    snapshot,
    terminal_values,
)
from grid_api.services.tests.test_media_job_journal import (
    db as db,
)
from grid_api.services.tests.test_media_job_journal import (
    synthetic_dependencies as synthetic_dependencies,
)
from grid_api.v2.schema import media_jobs, reservations

ENDPOINT = "https://" + "a" * 32 + ".r2.cloudflarestorage.com"
BUCKET = "synthetic-private-sources"
KEY = "source/" + "b" * 32 + ".wav"
URL = f"{ENDPOINT}/{BUCKET}/{KEY}?X-Amz-Signature=synthetic"
RECORD = {"endpoint": ENDPOINT, "bucket": BUCKET, "keys": [KEY]}


@pytest.fixture(autouse=True)
def private_storage(monkeypatch):
    monkeypatch.setenv("R2_TRANSIENT_ACCOUNT", ENDPOINT)
    monkeypatch.setenv("R2_TRANSIENT_BUCKET", "synthetic-public-outputs")
    monkeypatch.setattr(retention.storage, "private_source_bucket", lambda: BUCKET)
    monkeypatch.setattr(retention, "_sweep_lock", asyncio.Lock())


def test_capture_records_only_own_unique_inputs_without_signed_queries():
    assert retention.capture({}) is None
    assert retention.capture({"source_media": [{"url": URL}]}) == RECORD
    assert retention.capture({"source_image_url": URL, "source_image_urls": [URL]}) == RECORD
    assert "Signature" not in str(RECORD)
    retention.validate(RECORD)


def test_all_sixteen_pins_and_two_media_sources_have_bounded_authority():
    urls = [f"{ENDPOINT}/{BUCKET}/source/{index:032x}.png?synthetic=1" for index in range(16)]
    record = retention.capture(
        {"source_image_url": urls[0], "source_image_urls": urls, "source_media": [{"url": URL}, {"url": URL.replace(".wav", ".mp4")}]}
    )
    assert len(record["keys"]) == 18
    retention.validate(record)
    with pytest.raises(ValueError):
        retention.capture({"source_image_urls": urls + [urls[0]]})


def appearance_payload():
    return {"appearance_profile": "ingredients-dev-v1", "source_media": [
        {"role": role, "url": f"{ENDPOINT}/{BUCKET}/source/{index:032x}.{ext}?synthetic=1"}
        for index, (role, ext) in enumerate([
            ("reference_image", "png"), ("reference_audio", "wav"), ("reference_video", "mp4"),
        ])
    ]}


def test_ingredients_records_three_distinct_role_inputs_without_opening_pins(monkeypatch):
    record = retention.capture(appearance_payload())
    assert record == {"endpoint": ENDPOINT, "bucket": BUCKET,
                      "keys": [f"source/{0:032x}.png", f"source/{1:032x}.wav", f"source/{2:032x}.mp4"]}
    retention.validate(record)
    client = Mock()
    client.delete_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 204}}
    monkeypatch.setattr(retention, "_client", lambda _endpoint: client)
    retention.delete(record)
    assert client.delete_object.call_count == 3
    assert [call.kwargs for call in client.delete_object.call_args_list] == [
        {"Bucket": BUCKET, "Key": key} for key in record["keys"]
    ]


@pytest.mark.parametrize("change", [
    {"appearance_profile": None}, {"appearance_profile": "unknown"},
    {"source_image_url": URL}, {"source_image_urls": [URL]},
    {"source_media": [{"role": "reference_audio", "url": URL}] * 3},
    {"source_media": [{"role": role, "url": URL} for role in
                      ("reference_image", "reference_video", "reference_audio")]},
    {"source_media": [{"role": role, "url": URL} for role in
                      ("reference_image", "reference_audio", "reference_video", "extra")]},
])
def test_three_source_authority_is_exclusive_to_the_separate_ingredients_profile(change):
    with pytest.raises(ValueError):
        retention.capture({**appearance_payload(), **change})


def test_ingredients_third_input_cannot_grant_foreign_deletion_authority():
    payload = appearance_payload()
    payload["source_media"][2]["url"] = URL.replace(ENDPOINT, "https://foreign.example")
    with pytest.raises(ValueError):
        retention.capture(payload)


@pytest.mark.parametrize(
    "url",
    [
        URL.replace(BUCKET, "synthetic-public-outputs"),
        URL.replace(ENDPOINT, "https://other.example"),
        URL.replace("https:", "http:"),
        URL.replace("source/", "video/"),
        URL.replace("source/", "source/../"),
        URL.replace("source/", "source%2f"),
        URL + "#fragment",
        URL.split("?")[0],
        URL.replace("https://", "https://user@"),
        URL.replace(".com/", ".com:443/"),
        URL.replace(".wav", ".exe"),
        "x" * 8193,
        None,
    ],
)
def test_capture_rejects_foreign_or_malformed_inputs(url):
    with pytest.raises(ValueError):
        retention.capture({"source_media": [{"url": url}]})


@pytest.mark.parametrize("value", [None, {}, [None], [{}], [{"url": URL}] * 3])
def test_source_collection_is_bounded(value):
    with pytest.raises(ValueError):
        retention.capture({"source_media": value})


@pytest.mark.parametrize(
    "change",
    [
        {"bucket": "synthetic-public-outputs"},
        {"bucket": "../bucket"},
        {"endpoint": "https://different.example"},
        {"keys": [KEY, KEY]},
        {"keys": [KEY, "video/retained.mp4"]},
        {"keys": []},
        {"keys": [None]},
        {"unexpected": "not-authority"},
    ],
)
def test_stored_authority_is_validated_before_any_storage_call(monkeypatch, change):
    client = Mock()
    monkeypatch.setattr(retention, "_client", client)
    with pytest.raises(ValueError):
        retention.delete({**RECORD, **change})
    client.assert_not_called()


def test_exact_delete_requires_confirmed_response_and_allows_recorded_bucket_rotation(monkeypatch):
    client = Mock()
    monkeypatch.setattr(retention, "_client", lambda _endpoint: client)
    monkeypatch.setattr(retention.storage, "private_source_bucket", lambda: "new-private-bucket")
    client.delete_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 204}}
    retention.delete(RECORD)
    client.delete_object.assert_called_once_with(Bucket=BUCKET, Key=KEY)
    client.delete_object.return_value = {}
    with pytest.raises(RuntimeError):
        retention.delete(RECORD)


def test_partial_delete_retry_repeats_only_the_original_exact_keys(monkeypatch):
    client = Mock()
    monkeypatch.setattr(retention, "_client", lambda _endpoint: client)
    record = {**RECORD, "keys": [KEY, KEY.replace(".wav", ".mp4")]}
    confirmed = {"ResponseMetadata": {"HTTPStatusCode": 204}}
    client.delete_object.side_effect = [confirmed, TimeoutError("synthetic"), confirmed, confirmed]
    with pytest.raises(TimeoutError):
        retention.delete(record)
    retention.delete(record)
    assert [call.kwargs for call in client.delete_object.call_args_list] == [{"Bucket": BUCKET, "Key": key} for key in record["keys"] * 2]


async def source_job():
    job = prepared(await owner())
    payload = {**job.payload, "source_media": [{"url": URL}]}
    return jobs.prepare(
        account_id=job.account_id,
        idempotency_key=str(uuid4()),
        request_hash=job.request_hash,
        model=job.model,
        payload=payload,
        route_models=job.route_models,
        max_cost_micro=job.max_cost_micro,
    )


@pytest.mark.asyncio
async def test_held_unknown_and_legacy_rows_never_delete_even_expired_sources(db, monkeypatch):
    job = await source_job()
    await jobs.reserve(job)
    delete = Mock()
    monkeypatch.setattr(retention, "delete", delete)
    assert await retention.sweep() == {"deleted": 0, "errors": 0}
    journal, _, _ = await snapshot(job)
    assert journal["source_objects"] == RECORD
    assert journal["source_cleanup_attempted_at"] is None
    # Even a historical payload URL is not authority: do not backfill it.
    async with await database.new_session() as session:
        await session.execute(media_jobs.update().values(source_objects=None))
        await session.commit()
    await credits.release_job(job.job_id)
    assert await retention.sweep() == {"deleted": 0, "errors": 0}
    delete.assert_not_called()


@pytest.mark.asyncio
async def test_release_cleanup_retries_partial_failure_without_new_money_or_completion(db, monkeypatch):
    job = await source_job()
    await jobs.reserve(job)
    await credits.release_job(job.job_id)
    before = await snapshot(job)
    delete = Mock(side_effect=[RuntimeError("synthetic storage timeout"), None])
    monkeypatch.setattr(retention, "delete", delete)
    assert await retention.sweep() == {"deleted": 0, "errors": 1}
    row, _, _ = await snapshot(job)
    assert row["source_cleanup_at"] is None and row["source_cleanup_attempted_at"]
    assert await retention.sweep() == {"deleted": 1, "errors": 0}
    after = await snapshot(job)
    assert after[0]["source_cleanup_at"] and before[1:] == after[1:]
    assert await credits.get_balance(job.account_id) == 1_000_000
    assert await counts() == (1, 1, 0)
    assert await retention.sweep() == {"deleted": 0, "errors": 0}
    assert delete.call_count == 2 and delete.call_args.args == (RECORD,)


@pytest.mark.asyncio
async def test_paid_settlement_cleanup_does_not_change_charge_or_append_ledger(db, monkeypatch):
    job = await source_job()
    await jobs.reserve(job)
    worker, token = uuid4(), str(uuid4())
    assert await claim(job, worker_id=worker, execution_token=token) == "claimed"
    assert await credits.record_and_settle(ledger_values=terminal_values(job, worker), exact=True, media_execution_token=token) == "settled"
    before = await snapshot(job)
    monkeypatch.setattr(retention, "delete", Mock())
    assert await retention.sweep() == {"deleted": 1, "errors": 0}
    after = await snapshot(job)
    assert before[1:] == after[1:]
    assert await credits.get_balance(job.account_id) == 932_500
    assert await counts() == (1, 1, 1)


@pytest.mark.asyncio
async def test_missing_closure_timestamp_is_not_cleanup_authority(db, monkeypatch):
    job = await source_job()
    await jobs.reserve(job)
    async with await database.new_session() as session:
        await session.execute(reservations.update().values(status="settled", settled=None))
        await session.commit()
    delete = Mock()
    monkeypatch.setattr(retention, "delete", delete)
    assert await retention.sweep() == {"deleted": 0, "errors": 0}
    delete.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_authority_rolls_back_the_entire_hold_and_debit(db):
    job = prepared(await owner())
    invalid = jobs.prepare(
        account_id=job.account_id,
        idempotency_key=str(uuid4()),
        request_hash=job.request_hash,
        model=job.model,
        payload={**job.payload, "source_image_url": "https://foreign.example/input.png"},
        route_models=job.route_models,
        max_cost_micro=job.max_cost_micro,
    )
    with pytest.raises(jobs.MediaJobPaymentRequired):
        await jobs.reserve(invalid)
    assert await counts() == (0, 0, 0)
    assert await credits.get_balance(job.account_id) == 1_000_000


@pytest.mark.asyncio
async def test_failed_oldest_cleanup_does_not_starve_later_closed_jobs(db, monkeypatch):
    first, second = await source_job(), await source_job()
    await jobs.reserve(first)
    await jobs.reserve(second)
    await credits.release_job(first.job_id)
    await credits.release_job(second.job_id)
    monkeypatch.setattr(retention, "delete", Mock(side_effect=[TimeoutError("synthetic"), None, None]))
    assert await retention.sweep(limit=1) == {"deleted": 0, "errors": 1}
    assert await retention.sweep(limit=1) == {"deleted": 1, "errors": 0}
    assert (await snapshot(first))[0]["source_cleanup_at"] is None
    assert (await snapshot(second))[0]["source_cleanup_at"] is not None
    assert await retention.sweep(limit=1) == {"deleted": 1, "errors": 0}


@pytest.mark.asyncio
async def test_cancelled_sweep_keeps_single_flight_until_delete_finishes(db, monkeypatch):
    job = await source_job()
    await jobs.reserve(job)
    await credits.release_job(job.job_id)
    started, finish = threading.Event(), threading.Event()

    def delete(_record):
        started.set()
        assert finish.wait(timeout=5)

    monkeypatch.setattr(retention, "delete", delete)
    task = asyncio.create_task(retention.sweep())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert await retention.sweep() == {"deleted": 0, "errors": 0}
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await snapshot(job))[0]["source_cleanup_at"] is None
    assert await retention.sweep() == {"deleted": 1, "errors": 0}


@pytest.mark.asyncio
async def test_cleanup_background_loop_redacts_failures_and_survives_admission_rollback(monkeypatch, caplog):
    observed = asyncio.Event()

    async def sweep():
        observed.set()
        raise RuntimeError("private-source-secret must never be logged")

    monkeypatch.setattr(retention, "sweep", sweep)
    task = asyncio.create_task(main._media_source_sweeper())
    try:
        await asyncio.wait_for(observed.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "RuntimeError" in caplog.text and "private-source-secret" not in caplog.text

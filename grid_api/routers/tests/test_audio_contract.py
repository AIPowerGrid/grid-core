# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from grid_api.routers import audio as audio_router
from grid_api.routers import worker_ws
from grid_api.services import audio, credits, den, media, pricing, storage


def test_audio_recipe_root_is_a_canonical_contract():
    assert audio.recipe_root() == audio.ACE_STEP_RECIPE_ROOT
    assert audio.ACE_STEP_RECIPE_SPEC["fixed"]["model"] == "acestep-v15-turbo"
    assert audio.ACE_STEP_RECIPE_SPEC["fixed"]["use_random_seed"] is False


def test_audio_request_is_strict_and_bounded():
    request = audio_router.AudioRequest(
        prompt="clean electronic pulse",
        seed=0,
        bpm=96,
        key_scale="A minor",
        time_signature="4/4",
        vocal_language="en",
    )
    assert request.seed == 0
    assert request.bpm == 96
    assert request.key_scale == "A minor"
    assert request.time_signature == "4/4"
    assert request.vocal_language == "en"
    for invalid in (
        {"prompt": "x", "seconds": 9},
        {"prompt": "x", "inference_steps": 21},
        {"prompt": "x", "bpm": 301},
        {"prompt": "x", "key_scale": "A minor; drop table"},
        {"prompt": "x", "time_signature": "7/8"},
        {"prompt": "x", "vocal_language": "English"},
        {"prompt": "x", "model": "m" * 129},
        {"prompt": "x", "unknown": True},
    ):
        with pytest.raises(ValidationError):
            audio_router.AudioRequest(**invalid)


def test_audio_pricing_and_den_are_duration_based():
    assert pricing.quote_audio(audio.DEFAULT_AUDIO_MODEL, 30) == 60_000
    assert den.calculate_media_den("audio", 1, 1, n=1, seconds=30) == 1.8
    assert den.calculate_media_den("audio", 4096, 4096, n=1, seconds=30) == 1.8


def test_audio_storage_uses_audio_prefix_and_mime(monkeypatch):
    class Client:
        def generate_presigned_url(self, *_args, **_kwargs):
            return "https://put.example/one"

    monkeypatch.setattr(storage, "_client", lambda: Client())
    monkeypatch.setattr(storage, "media_bucket", lambda: "bucket")
    monkeypatch.setattr(storage, "public_media_base", lambda: "https://media.example")
    slot = storage.presign_outputs("job-1", 1, "wav", job_type="audio")[0]
    assert slot["key"] == "audio/job-1/0.wav"
    assert slot["content_type"] == "audio/wav"


def test_audio_storage_verifies_object_type_and_size(monkeypatch):
    class Client:
        def head_object(self, **_kwargs):
            return {"ContentLength": 1_920_044, "ContentType": "audio/wav"}

    monkeypatch.setattr(storage, "_client", lambda: Client())
    monkeypatch.setattr(storage, "media_bucket", lambda: "bucket")
    assert storage.uploaded_outputs_present(
        [{"key": "audio/job/0.wav", "content_type": "audio/wav"}],
        min_bytes=audio.MIN_WAV_BYTES,
        max_bytes=audio.MAX_AUDIO_BYTES,
    )


def test_audio_receipt_binds_output_and_recipe_root():
    outputs = [{"sha256": "a" * 64, "key": "audio/job/0.wav"}]
    commitment = worker_ws._media_result_commitment("audio", {"recipe_root": audio.ACE_STEP_RECIPE_ROOT}, outputs)
    assert commitment == {
        "outputs": ["a" * 64],
        "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
    }
    assert worker_ws._media_output_units("audio", {"seconds": 30}, 1) == 30


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"index": 0, "sha256": "a" * 63}],
        [{"index": 0, "sha256": "A" * 64}],
        [{"index": 0, "sha256": "a" * 62 + "  "}],
        [{"index": 1, "sha256": "a" * 64}],
        [
            {"index": 0, "sha256": "a" * 64},
            {"index": 0, "sha256": "b" * 64},
        ],
    ],
)
def test_audio_results_require_exact_canonical_output_hashes(results):
    expected = 2 if len(results) == 2 else 1
    with pytest.raises(ValueError):
        worker_ws._validated_audio_results(results, expected)


def test_audio_registration_requires_managed_profile():
    assert worker_ws._requires_managed_profile(["audio"], None)
    assert not worker_ws._requires_managed_profile(["audio"], {"digest": "a" * 64})
    assert not worker_ws._requires_managed_profile(["image"], None)


@pytest.mark.asyncio
async def test_live_audio_billing_uses_requested_seconds(monkeypatch):
    observed = {}

    async def authorize(account_id, model, job_type, n, seconds, job_id, **kwargs):
        observed.update(
            account_id=account_id,
            model=model,
            job_type=job_type,
            n=n,
            seconds=seconds,
            record_reservation=kwargs["record_reservation"],
        )
        return {"ok": False, "reason": "stop after quote"}

    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    monkeypatch.setattr(credits, "authorize_media", authorize)
    with pytest.raises(HTTPException) as exc:
        await media.submit_and_wait(
            audio.DEFAULT_AUDIO_MODEL,
            "audio",
            {"n": 1, "seconds": 45},
            1,
            account_id=uuid.uuid4(),
        )
    assert exc.value.status_code == 402
    assert observed["seconds"] == 45
    assert observed["job_type"] == "audio"
    assert observed["record_reservation"] is True


@pytest.mark.asyncio
async def test_audio_router_dispatches_governed_payload(monkeypatch):
    account_id = uuid.uuid4()
    observed = {}

    async def authenticate(*_args, **_kwargs):
        return {"account_id": account_id}

    async def available(**_kwargs):
        return [audio.DEFAULT_AUDIO_MODEL]

    async def quota_check(_user):
        return None

    async def submit(model, job_type, payload, timeout, **kwargs):
        observed.update(model=model, job_type=job_type, payload=payload, timeout=timeout, kwargs=kwargs)
        return ([{"url": "https://media.example/audio.wav", "seed": payload["seed"]}], {"worker": "rig"})

    monkeypatch.setattr(audio_router.accounts_svc, "authenticate", authenticate)
    monkeypatch.setattr(
        audio_router,
        "get_settings",
        lambda: type("Settings", (), {"audio_enabled": True})(),
    )
    monkeypatch.setattr(audio_router, "get_available_models", available)
    monkeypatch.setattr(audio_router.quota, "check_and_consume", quota_check)
    monkeypatch.setattr(audio_router.media, "submit_and_wait", submit)

    request = Request({"type": "http", "method": "POST", "path": "/v1/audio/generations", "headers": []})
    response = await audio_router.create_audio(
        request,
        audio_router.AudioRequest(
            prompt="clean pulse",
            seconds=30,
            seed=0,
            bpm=96,
            key_scale="A minor",
            time_signature="4/4",
            vocal_language="en",
        ),
        apikey="test-key",
    )
    assert response["data"][0]["seed"] == 0
    assert observed["job_type"] == "audio"
    assert observed["payload"]["recipe_root"] == audio.ACE_STEP_RECIPE_ROOT
    assert observed["payload"]["bpm"] == 96
    assert observed["payload"]["key_scale"] == "A minor"
    assert observed["payload"]["time_signature"] == "4"
    assert observed["payload"]["vocal_language"] == "en"
    assert response["grid"]["controls"] == {
        "bpm": 96,
        "key_scale": "A minor",
        "time_signature": "4",
        "vocal_language": "en",
    }
    assert observed["kwargs"]["account_id"] == account_id


@pytest.mark.asyncio
async def test_audio_router_is_default_off(monkeypatch):
    monkeypatch.setattr(
        audio_router,
        "get_settings",
        lambda: type("Settings", (), {"audio_enabled": False})(),
    )
    request = Request({"type": "http", "method": "POST", "path": "/v1/audio/generations", "headers": []})

    with pytest.raises(HTTPException) as exc:
        await audio_router.create_audio(
            request,
            audio_router.AudioRequest(prompt="clean pulse"),
            apikey="unused",
        )

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_unsigned_audio_result_is_released_without_ledger_write(monkeypatch):
    class WorkerSocket:
        async def send_json(self, _value):
            return None

        async def receive_json(self):
            return {
                "type": "done",
                "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
                "results": [{"index": 0, "sha256": "a" * 64, "seed": 7}],
            }

    events = []
    presign = {}

    def presign_outputs(*_args, **kwargs):
        presign.update(kwargs)
        return [
            {
                "put_url": "https://put.example/audio",
                "public_url": "https://media.example/audio.wav",
                "key": "audio/job-audio/0.wav",
                "content_type": "audio/wav",
            },
        ]

    monkeypatch.setattr(
        worker_ws.storage,
        "presign_outputs",
        presign_outputs,
    )
    monkeypatch.setattr(worker_ws.signing, "verify_worker_sig", lambda *_a, **_k: None)
    monkeypatch.setattr(worker_ws.storage, "uploaded_outputs_present", lambda *_a, **_k: True)

    async def publish_error(job_id, message, **_kwargs):
        events.append(("error", job_id, message))

    async def release(job_id):
        events.append(("release", job_id))

    async def forbidden_settle(**_kwargs):
        raise AssertionError("unsigned audio must not reach settlement")

    monkeypatch.setattr(worker_ws.token_stream, "publish_error", publish_error)
    monkeypatch.setattr(worker_ws.credits, "release_job", release)
    monkeypatch.setattr(worker_ws.credits, "record_and_settle", forbidden_settle)

    result = await worker_ws._handle_media_job(
        WorkerSocket(),
        {
            "job_id": "job-audio",
            "job_type": "audio",
            "payload": {
                "n": 1,
                "seconds": 30,
                "inference_steps": 8,
                "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
            },
        },
        audio.DEFAULT_AUDIO_MODEL,
        "worker-1",
        {
            "name": "audio-rig-1",
            "wallet_address": "0x" + "1" * 40,
            "signer_address": "0x" + "2" * 40,
            "worker_profile": {"digest": "a" * 64},
        },
    )
    assert result is True
    assert presign["expires"] == audio.AUDIO_UPLOAD_URL_TTL
    assert presign["expires"] > audio.AUDIO_WORKER_TIMEOUT
    assert events == [
        ("error", "job-audio", "Worker receipt verification failed."),
        ("release", "job-audio"),
    ]


@pytest.mark.asyncio
async def test_incomplete_audio_result_is_released_before_signature_or_settlement(monkeypatch):
    class WorkerSocket:
        async def send_json(self, _value):
            return None

        async def receive_json(self):
            return {
                "type": "done",
                "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
                "results": [],
            }

    events = []
    monkeypatch.setattr(
        worker_ws.storage,
        "presign_outputs",
        lambda *_args, **_kwargs: [
            {
                "put_url": "https://put.example/audio",
                "public_url": "https://media.example/audio.wav",
                "key": "audio/job-audio/0.wav",
                "content_type": "audio/wav",
            },
        ],
    )

    async def publish_error(job_id, message, **_kwargs):
        events.append(("error", job_id, message))

    async def release(job_id):
        events.append(("release", job_id))

    monkeypatch.setattr(worker_ws.token_stream, "publish_error", publish_error)
    monkeypatch.setattr(worker_ws.credits, "release_job", release)
    monkeypatch.setattr(
        worker_ws.signing,
        "verify_worker_sig",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("invalid audio output must not reach signature verification")),
    )

    async def forbidden_settle(**_kwargs):
        raise AssertionError("invalid audio output must not reach settlement")

    monkeypatch.setattr(worker_ws.credits, "record_and_settle", forbidden_settle)
    result = await worker_ws._handle_media_job(
        WorkerSocket(),
        {
            "job_id": "job-audio",
            "job_type": "audio",
            "payload": {
                "n": 1,
                "seconds": 30,
                "inference_steps": 8,
                "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
            },
        },
        audio.DEFAULT_AUDIO_MODEL,
        "worker-1",
        {
            "name": "audio-rig-1",
            "wallet_address": "0x" + "1" * 40,
            "signer_address": "0x" + "2" * 40,
            "worker_profile": {"digest": "a" * 64},
        },
    )
    assert result is True
    assert events == [
        ("error", "job-audio", "Worker output verification failed."),
        ("release", "job-audio"),
    ]


@pytest.mark.asyncio
async def test_missing_audio_object_is_released_before_signature_or_settlement(monkeypatch):
    class WorkerSocket:
        async def send_json(self, _value):
            return None

        async def receive_json(self):
            return {
                "type": "done",
                "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
                "results": [{"index": 0, "sha256": "a" * 64, "seed": 7}],
            }

    events = []
    monkeypatch.setattr(
        worker_ws.storage,
        "presign_outputs",
        lambda *_args, **_kwargs: [
            {
                "put_url": "https://put.example/audio",
                "public_url": "https://media.example/audio.wav",
                "key": "audio/job-audio/0.wav",
                "content_type": "audio/wav",
            },
        ],
    )
    monkeypatch.setattr(worker_ws.storage, "uploaded_outputs_present", lambda *_a, **_k: False)

    async def publish_error(job_id, message, **_kwargs):
        events.append(("error", job_id, message))

    async def release(job_id):
        events.append(("release", job_id))

    monkeypatch.setattr(worker_ws.token_stream, "publish_error", publish_error)
    monkeypatch.setattr(worker_ws.credits, "release_job", release)
    monkeypatch.setattr(
        worker_ws.signing,
        "verify_worker_sig",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("missing object must not reach signature verification")),
    )

    async def forbidden_settle(**_kwargs):
        raise AssertionError("missing object must not reach settlement")

    monkeypatch.setattr(worker_ws.credits, "record_and_settle", forbidden_settle)
    result = await worker_ws._handle_media_job(
        WorkerSocket(),
        {
            "job_id": "job-audio",
            "job_type": "audio",
            "payload": {
                "n": 1,
                "seconds": 30,
                "inference_steps": 8,
                "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
            },
        },
        audio.DEFAULT_AUDIO_MODEL,
        "worker-1",
        {
            "name": "audio-rig-1",
            "wallet_address": "0x" + "1" * 40,
            "signer_address": "0x" + "2" * 40,
            "worker_profile": {"digest": "a" * 64},
        },
    )
    assert result is True
    assert events == [
        ("error", "job-audio", "Worker output verification failed."),
        ("release", "job-audio"),
    ]

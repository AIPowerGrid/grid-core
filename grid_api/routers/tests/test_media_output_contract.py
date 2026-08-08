# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import pytest

from grid_api.routers import worker_ws


def _slots(count: int) -> list[dict]:
    return [
        {
            "put_url": f"https://put.example/image/{index}",
            "public_url": f"https://media.example/image/{index}.webp",
            "key": f"image/job-image/{index}.webp",
            "content_type": "image/webp",
        }
        for index in range(count)
    ]


def test_media_results_require_every_unique_presigned_slot():
    with pytest.raises(ValueError, match="count"):
        worker_ws._validated_media_results(
            [{"index": 0, "sha256": "a" * 64}],
            2,
        )

    with pytest.raises(ValueError, match="duplicated"):
        worker_ws._validated_media_results(
            [
                {"index": 0, "sha256": "a" * 64},
                {"index": 0, "sha256": "b" * 64},
            ],
            2,
        )


@pytest.mark.asyncio
async def test_incomplete_image_batch_releases_before_settlement(monkeypatch):
    class WorkerSocket:
        async def send_json(self, _value):
            return None

        async def receive_json(self):
            return {
                "type": "done",
                "results": [{"index": 0, "sha256": "a" * 64, "seed": 7}],
            }

    events: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(worker_ws.storage, "presign_outputs", lambda *_a, **_k: _slots(2))

    async def publish_error(job_id, message, **_kwargs):
        events.append(("error", job_id, message))

    async def release(job_id):
        events.append(("release", job_id, None))

    monkeypatch.setattr(worker_ws.token_stream, "publish_error", publish_error)
    monkeypatch.setattr(worker_ws.credits, "release_job", release)
    monkeypatch.setattr(
        worker_ws.storage,
        "uploaded_outputs_present",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("incomplete results must not reach object verification")
        ),
    )
    monkeypatch.setattr(
        worker_ws.credits,
        "record_and_settle",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("incomplete results must not settle")
        ),
    )

    result = await worker_ws._handle_media_job(
        WorkerSocket(),
        {
            "job_id": "job-image",
            "job_type": "image",
            "payload": {"n": 2, "width": 1024, "height": 1024, "steps": 8},
        },
        "Krea 2 Turbo",
        "worker-1",
        {"name": "image-rig-1", "wallet_address": "0x" + "1" * 40},
    )

    assert result is True
    assert events == [
        ("error", "job-image", "Worker output verification failed."),
        ("release", "job-image", None),
    ]


@pytest.mark.asyncio
async def test_missing_image_object_releases_before_settlement(monkeypatch):
    class WorkerSocket:
        async def send_json(self, _value):
            return None

        async def receive_json(self):
            return {
                "type": "done",
                "results": [
                    {"index": 0, "sha256": "a" * 64, "seed": 7},
                    {"index": 1, "sha256": "b" * 64, "seed": 8},
                ],
            }

    events: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(worker_ws.storage, "presign_outputs", lambda *_a, **_k: _slots(2))
    monkeypatch.setattr(worker_ws.storage, "uploaded_outputs_present", lambda *_a, **_k: False)

    async def publish_error(job_id, message, **_kwargs):
        events.append(("error", job_id, message))

    async def release(job_id):
        events.append(("release", job_id, None))

    monkeypatch.setattr(worker_ws.token_stream, "publish_error", publish_error)
    monkeypatch.setattr(worker_ws.credits, "release_job", release)
    monkeypatch.setattr(
        worker_ws.credits,
        "record_and_settle",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing output objects must not settle")
        ),
    )

    result = await worker_ws._handle_media_job(
        WorkerSocket(),
        {
            "job_id": "job-image",
            "job_type": "image",
            "payload": {"n": 2, "width": 1024, "height": 1024, "steps": 8},
        },
        "Krea 2 Turbo",
        "worker-1",
        {"name": "image-rig-1", "wallet_address": "0x" + "1" * 40},
    )

    assert result is True
    assert events == [
        ("error", "job-image", "Worker output verification failed."),
        ("release", "job-image", None),
    ]

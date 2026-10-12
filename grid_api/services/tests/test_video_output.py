# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib
import io
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from grid_api.services import video_output as video

MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + b"\x00\x00\x00\x0cmdatdata"


class Client:
    def __init__(self, data=MP4):
        self.source = data
        self.objects = {}
        self.calls = []
        self.head = {"ContentLength": len(data), "ContentType": "video/mp4", "ETag": '"source-v1"'}
        self.get = {}
        self.copy_error = None
        self.body = None
        self.read_sizes = []
        self.read_error = None
        self.overwrite_source = False

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        return self.head

    def copy_object(self, **kwargs):
        self.calls.append(("copy", kwargs))
        if self.copy_error:
            raise self.copy_error
        self.objects[kwargs["Key"]] = self.source
        if self.overwrite_source:
            self.source = b"changed through the old PUT URL"

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        data = self.objects[kwargs["Key"]]
        stream = io.BytesIO(data)
        self.body = stream
        client = self

        class Body:
            def read(self, size):
                client.read_sizes.append(size)
                if client.read_error:
                    raise client.read_error
                return stream.read(size)

            def close(self):
                stream.close()

        return {"ContentLength": len(data), "ContentType": "video/mp4", "Body": Body(), **self.get}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        self.objects.pop(kwargs["Key"], None)


@pytest.fixture
def setup(monkeypatch):
    client = Client()
    job_id = str(uuid4())
    slot = {"key": f"video/{job_id}/attempt-{uuid4().hex}/0.mp4", "content_type": "video/mp4"}
    monkeypatch.setattr(video, "_client", lambda: client)
    monkeypatch.setattr(video.storage, "media_bucket", lambda: "media")
    monkeypatch.setattr(video.storage, "public_media_base", lambda: "https://assets.example")
    return client, job_id, slot


def freeze(setup, digest=None):
    client, job_id, slot = setup
    return video.freeze(slot, job_id=job_id, expected_sha256=digest or hashlib.sha256(client.source).hexdigest())


def test_frozen_hash_survives_source_overwrite_and_never_issues_put(setup):
    client, job_id, slot = setup
    client.overwrite_source = True
    result = freeze(setup)
    assert result["key"].startswith(f"video/{job_id}/final-")
    assert result["key"].endswith(".mp4") and result["key"] != slot["key"]
    assert result["url"] == f"https://assets.example/{result['key']}"
    assert result["sha256"] == hashlib.sha256(MP4).hexdigest()
    assert result["bytes"] == len(MP4) and result["content_type"] == "video/mp4"
    assert client.objects[result["key"]] == MP4 and client.source != MP4
    assert [name for name, _ in client.calls] == ["head", "copy", "get"]
    copy = client.calls[1][1]
    assert copy["CopySource"] == {"Bucket": "media", "Key": slot["key"]}
    assert copy["CopySourceIfMatch"] == '"source-v1"'
    assert client.body.closed


def test_each_core_attempt_uses_a_distinct_private_destination(setup):
    first, second = freeze(setup), freeze(setup)
    assert first["key"] != second["key"]
    assert setup[0].objects[first["key"]] == setup[0].objects[second["key"]] == MP4


def test_verification_client_has_bounded_transport_and_retries(monkeypatch):
    client_factory = video._client
    client_factory.cache_clear()
    calls = []
    client = object()

    def construct(*args, **kwargs):
        calls.append((args, kwargs))
        return client

    monkeypatch.setattr(video.storage, "_client", lambda: SimpleNamespace(meta=SimpleNamespace(endpoint_url="https://r2.example")))
    monkeypatch.setattr(video.boto3, "client", construct)
    try:
        assert client_factory() is client_factory() is client
        assert len(calls) == 1 and calls[0][0] == ("s3",)
        assert calls[0][1]["endpoint_url"] == "https://r2.example"
        config = calls[0][1]["config"]
        assert config.signature_version == "s3v4"
        assert config.connect_timeout == 5 and config.read_timeout == 10
        assert config.retries == {"total_max_attempts": 2, "mode": "standard"}
    finally:
        client_factory.cache_clear()


def test_missing_source_version_is_uncertain_before_copy(setup):
    del setup[0].head["ETag"]
    with pytest.raises(RuntimeError, match="version"):
        freeze(setup)
    assert [name for name, _ in setup[0].calls] == ["head"]


@pytest.mark.parametrize(
    "metadata",
    [
        {"ContentLength": 0},
        {"ContentLength": True},
        {"ContentLength": "36"},
        {"ContentLength": video.MAX_VIDEO_BYTES + 1},
        {"ContentType": "text/html"},
    ],
)
def test_bad_source_bounds_reject_before_copy(setup, metadata):
    setup[0].head.update(metadata)
    with pytest.raises(video.InvalidVideoOutput):
        freeze(setup)
    assert [name for name, _ in setup[0].calls] == ["head"]


@pytest.mark.parametrize("mutation", ["hash", "header", "short", "extra", "metadata"])
def test_bad_copy_is_closed_and_only_its_unique_key_is_deleted(setup, mutation):
    client = setup[0]
    client.objects["unrelated-paid-result"] = MP4
    digest = hashlib.sha256(MP4).hexdigest()
    if mutation == "hash":
        digest = "a" * 64
    elif mutation == "header":
        client.source = b"x" * len(MP4)
        digest = hashlib.sha256(client.source).hexdigest()
    elif mutation == "short":
        client.source = MP4[:-1]
        client.get["ContentLength"] = len(MP4)
    elif mutation == "extra":
        client.source = MP4 + b"extra"
        client.get["ContentLength"] = len(MP4)
    else:
        client.get["ContentType"] = "application/octet-stream"
    with pytest.raises(video.InvalidVideoOutput):
        freeze(setup, digest)
    assert client.body.closed
    assert client.objects == {"unrelated-paid-result": MP4}
    assert client.calls[-1][0] == "delete"
    assert client.calls[-1][1]["Key"] == client.calls[1][1]["Key"]


@pytest.mark.parametrize("code", ["PreconditionFailed", "SlowDown", "InternalError"])
def test_changed_source_or_storage_outage_is_uncertain_not_invalid(setup, code):
    setup[0].copy_error = ClientError({"Error": {"Code": code}}, "CopyObject")
    with pytest.raises(ClientError):
        freeze(setup)
    assert [name for name, _ in setup[0].calls] == ["head", "copy"]


def test_stream_timeout_closes_body_without_deleting_uncertain_copy(setup):
    client = setup[0]
    client.read_error = TimeoutError("read unavailable")
    with pytest.raises(TimeoutError):
        freeze(setup)
    assert client.body.closed and len(client.objects) == 1
    assert all(name != "delete" for name, _ in client.calls)


def test_hash_streaming_is_bounded_and_requires_eof(setup):
    client = setup[0]
    client.source = MP4 + b"x" * (video.CHUNK_BYTES * 3)
    client.head["ContentLength"] = len(client.source)
    result = freeze(setup)
    assert result["sha256"] == hashlib.sha256(client.source).hexdigest()
    assert max(client.read_sizes) == video.CHUNK_BYTES and client.read_sizes[-1] == 1


def test_deadline_is_uncertain_and_capacity_is_released(setup, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(video, "VERIFY_SECONDS", 0)
        with pytest.raises(TimeoutError):
            freeze(setup)
    assert freeze(setup)["bytes"] == len(MP4)


def test_saturated_verification_does_not_start_storage_work(setup, monkeypatch):
    semaphore = threading.BoundedSemaphore(2)
    assert semaphore.acquire() and semaphore.acquire()
    monkeypatch.setattr(video, "_slots", semaphore)
    monkeypatch.setattr(video, "SLOT_WAIT_SECONDS", 0)
    with pytest.raises(TimeoutError):
        freeze(setup)
    assert setup[0].calls == []


@pytest.mark.parametrize("key", ["video/other/0.mp4", "video/../0.mp4", "image/job/0.webp"])
def test_only_core_attempt_slot_can_be_frozen(setup, key):
    setup[2]["key"] = key
    with pytest.raises(ValueError, match="contract"):
        freeze(setup)
    assert setup[0].calls == []

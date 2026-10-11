# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from grid_api.config import GridSettings
from grid_api.services import storage


class _Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self, limit: int) -> bytes:
        return self.value[:limit]


class _StorageClient:
    def __init__(self, body: bytes = b"immutable-image"):
        self.body = body
        self.calls = []

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        return {"ContentLength": len(self.body), "ContentType": "image/webp"}

    def copy_object(self, **kwargs):
        self.calls.append(("copy", kwargs))

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        return {"Body": _Body(self.body), "ContentType": "image/webp"}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))


def test_media_bucket_requires_explicit_configuration(monkeypatch):
    monkeypatch.delenv("R2_TRANSIENT_BUCKET", raising=False)

    with pytest.raises(RuntimeError, match="R2_TRANSIENT_BUCKET not configured"):
        storage.media_bucket()


def test_media_bucket_returns_configured_name(monkeypatch):
    monkeypatch.setenv("R2_TRANSIENT_BUCKET", "configured-media-bucket")

    assert storage.media_bucket() == "configured-media-bucket"


@pytest.mark.parametrize("bucket", ["", "public-outputs"])
def test_private_sources_cannot_use_missing_or_public_output_bucket(monkeypatch, bucket):
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(media_private_source_bucket=bucket))
    monkeypatch.setattr(storage, "media_bucket", lambda: "public-outputs")
    with pytest.raises(HTTPException) as error:
        storage.private_source_bucket()
    assert error.value.status_code == 503
    if bucket:
        assert bucket not in error.value.detail


@pytest.mark.parametrize("bucket", ["private-source-test", "aipg-ltx-test-1234"])
def test_private_sources_accept_separate_reviewed_bucket(monkeypatch, bucket):
    settings = GridSettings(_env_file=None, media_private_source_bucket=bucket)
    monkeypatch.setattr(storage, "get_settings", lambda: settings)
    monkeypatch.setattr(storage, "media_bucket", lambda: "public-outputs")
    assert storage.private_source_bucket() == bucket


@pytest.mark.parametrize("bucket", ["https://private.invalid", "../public", "a", "with spaces", "UPPERCASE"])
def test_private_bucket_config_rejects_paths_or_invalid_names(bucket):
    with pytest.raises(ValueError):
        GridSettings(_env_file=None, media_private_source_bucket=bucket)


def test_private_upload_has_bounded_signed_get_and_no_public_output_bucket(monkeypatch):
    calls = []

    class Client:
        def put_object(self, **kwargs):
            calls.append(("put", kwargs))

        def generate_presigned_url(self, operation, **kwargs):
            calls.append((operation, kwargs))
            return "https://private-signed.invalid/source"

    monkeypatch.setattr(storage, "private_source_bucket", lambda: "private-source-test")
    monkeypatch.setattr(storage, "_client", Client)
    url = storage.upload_private_source(b"synthetic-wave", "wav")
    assert url == "https://private-signed.invalid/source"
    assert calls[0][1]["Bucket"] == calls[1][1]["Params"]["Bucket"] == "private-source-test"
    assert calls[0][1]["Key"] == calls[1][1]["Params"]["Key"]
    assert calls[0][1]["ContentType"] == "audio/wav"
    assert calls[1][1]["ExpiresIn"] == 3600


@pytest.mark.parametrize("data,ext,expires", [(b"", "wav", 3600), (b"x", "exe", 3600),
                                              pytest.param(b"x" * (32 * 1024 * 1024 + 1), "mp4", 3600, id="oversize"),
                                              (b"x", "wav", 3601), (b"x", "wav", 0), (b"x", "wav", True)])
def test_invalid_private_upload_is_rejected_before_network(monkeypatch, data, ext, expires):
    monkeypatch.setattr(storage, "private_source_bucket", lambda: "private-source-test")
    monkeypatch.setattr(storage, "_client", lambda: pytest.fail("storage must not be called"))
    with pytest.raises(ValueError):
        storage.upload_private_source(data, ext, expires)


def test_validator_output_is_copied_hashed_then_source_deleted(monkeypatch):
    client = _StorageClient()
    monkeypatch.setattr(storage, "_client", lambda: client)
    monkeypatch.setattr(storage, "media_bucket", lambda: "media")
    monkeypatch.setattr(storage, "public_media_base", lambda: "https://media.example")

    witness = storage.freeze_validator_output(
        {"key": "image/job/0.webp", "content_type": "image/webp"},
        witness_key="validator/job/0.webp",
        max_bytes=1024,
    )

    assert witness == {
        "key": "validator/job/0.webp",
        "url": "https://media.example/validator/job/0.webp",
        "sha256": "de2988ddda527e646c4c5f62a172ab620cdc69683333a7918821bcc1b598592b",
        "bytes": 15,
        "content_type": "image/webp",
    }
    assert [kind for kind, _kwargs in client.calls] == ["head", "copy", "get", "delete"]
    copy = client.calls[1][1]
    assert copy["CopySource"] == {"Bucket": "media", "Key": "image/job/0.webp"}
    assert copy["Key"] == "validator/job/0.webp"


def test_validator_output_rejects_oversize_before_copy(monkeypatch):
    client = _StorageClient(body=b"x" * 20)
    monkeypatch.setattr(storage, "_client", lambda: client)
    monkeypatch.setattr(storage, "media_bucket", lambda: "media")

    with pytest.raises(ValueError, match="outside its declared bounds"):
        storage.freeze_validator_output(
            {"key": "image/job/0.webp", "content_type": "image/webp"},
            witness_key="validator/job/0.webp",
            max_bytes=10,
        )

    assert [kind for kind, _kwargs in client.calls] == ["head"]


def test_delete_outputs_removes_only_exact_nonempty_slot_keys(monkeypatch):
    client = _StorageClient()
    monkeypatch.setattr(storage, "_client", lambda: client)
    monkeypatch.setattr(storage, "media_bucket", lambda: "media")

    storage.delete_outputs(
        [
            {"key": "audio/canary/0.wav"},
            {"key": ""},
            {},
            {"key": "image/canary/0.webp"},
        ],
    )

    assert client.calls == [
        ("delete", {"Bucket": "media", "Key": "audio/canary/0.wav"}),
        ("delete", {"Bucket": "media", "Key": "image/canary/0.webp"}),
    ]

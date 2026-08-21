# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

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

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Synthetic private S3 for SQL staging/payment tests, never live storage proof."""

import io

import pytest
from botocore.exceptions import ClientError

from grid_api.services import media_source_retention as retention
from grid_api.services import storage

ENDPOINT = "https://" + "a" * 32 + ".r2.cloudflarestorage.com"


class SourceStore:
    def __init__(self):
        self.objects, self.puts, self.reads, self.bodies = {}, [], [], []
        self.failure, self.before_put = None, None
        self.signed = 0

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch):
        assert IfNoneMatch == "*" and Bucket == "synthetic-private-inputs"
        if self.before_put:
            self.before_put(Key)
        self.puts.append(Key)
        if self.failure == "put":
            raise RuntimeError("private credentials and source URL")
        if Key in self.objects:
            raise ClientError({"ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        self.objects[Key] = Body
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_object(self, *, Bucket, Key):
        self.reads.append(Key)
        raw = self.objects[Key]
        observed = (b"x" * len(raw) if self.failure == "digest" else raw + b"x" if self.failure == "extra"
                    else raw[:-1] if self.failure == "short" else raw)
        body = io.BytesIO(observed)
        self.bodies.append(body)
        return {"Body": body, "ContentLength": len(raw) + (1 if self.failure == "length" else 0),
                "ETag": "invalid" if self.failure == "etag" else '"synthetic-object"',
                "ResponseMetadata": {"HTTPStatusCode": 200}}

    def generate_presigned_url(self, _operation, *, Params, ExpiresIn):
        self.signed += 1
        return f"{ENDPOINT}/{Params['Bucket']}/{Params['Key']}?synthetic={self.signed}&expires={ExpiresIn}"


@pytest.fixture(autouse=True)
def source_store(monkeypatch):
    store = SourceStore()
    monkeypatch.setenv("R2_TRANSIENT_ACCOUNT", ENDPOINT)
    monkeypatch.setattr(storage, "media_bucket", lambda: "synthetic-public-output")
    monkeypatch.setattr(storage, "private_source_bucket", lambda: "synthetic-private-inputs")
    monkeypatch.setattr(retention, "_client", lambda _endpoint: store)
    return store

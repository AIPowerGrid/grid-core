# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Freeze candidate async video uploads before their economic terminal.

The worker's PUT slot is mutable transport. Only a unique Core-owned copy,
hashed directly through S3, may become the durable paid result. This verifies
byte integrity and a minimal MP4 envelope, not codec quality or model fidelity.
Call from a thread; storage uncertainty must preserve the execution hold.
"""

import hashlib
import logging
import re
import threading
import time
from functools import lru_cache
from typing import Any, TypedDict
from uuid import UUID, uuid4

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

from . import storage

logger = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 256 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
VERIFY_SECONDS = 120
SLOT_WAIT_SECONDS = 5
_slots = threading.BoundedSemaphore(2)


class InvalidVideoOutput(ValueError):
    """A witnessed output definitively violates the candidate byte contract."""


class FrozenVideo(TypedDict):
    key: str
    url: str
    sha256: str
    bytes: int
    content_type: str


@lru_cache
def _client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=storage._client().meta.endpoint_url,
        config=BotoConfig(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=10,
            retries={"total_max_attempts": 2, "mode": "standard"},
        ),
    )


def _length(metadata: dict[str, Any]) -> int:
    length = metadata.get("ContentLength")
    content_type = str(metadata.get("ContentType") or "").split(";", 1)[0].strip().lower()
    if type(length) is not int or not 16 <= length <= MAX_VIDEO_BYTES or content_type != "video/mp4":
        raise InvalidVideoOutput("video output is outside its declared bounds")
    return length


def _deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("video verification deadline exceeded")


def _freeze(slot: dict[str, Any], job_id: str, expected_sha256: str) -> FrozenVideo:
    client = _client()
    bucket = storage.media_bucket()
    source_key = slot["key"]
    deadline = time.monotonic() + VERIFY_SECONDS
    try:
        metadata = client.head_object(Bucket=bucket, Key=source_key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in {"404", "NoSuchKey", "NotFound"}:
            raise InvalidVideoOutput("video upload is missing") from exc
        raise
    length = _length(metadata)
    etag = metadata.get("ETag")
    if not isinstance(etag, str) or not etag:
        raise RuntimeError("video source version is unavailable")
    _deadline(deadline)

    # A new private destination for every attempt prevents an old Core session
    # from replacing the winner's object after its execution lease expires.
    frozen_key = f"video/{job_id}/final-{uuid4().hex}.mp4"
    try:
        client.copy_object(
            Bucket=bucket,
            Key=frozen_key,
            CopySource={"Bucket": bucket, "Key": source_key},
            CopySourceIfMatch=etag,
            ContentType="video/mp4",
            MetadataDirective="REPLACE",
        )
        _deadline(deadline)
        frozen = client.get_object(Bucket=bucket, Key=frozen_key)
        body = frozen["Body"]
        try:
            if _length(frozen) != length:
                raise InvalidVideoOutput("frozen video metadata changed")
            digest = hashlib.sha256()
            total = 0
            prefix = bytearray()
            while True:
                _deadline(deadline)
                part = body.read(min(CHUNK_BYTES, length - total + 1))
                _deadline(deadline)
                if not part:
                    break
                total += len(part)
                if total > length:
                    raise InvalidVideoOutput("frozen video exceeds its declared length")
                if len(prefix) < 16:
                    prefix.extend(part[: 16 - len(prefix)])
                digest.update(part)
            if total != length:
                raise InvalidVideoOutput("frozen video is truncated")
            box_size = int.from_bytes(prefix[:4], "big")
            if prefix[4:8] != b"ftyp" or not 16 <= box_size <= length or box_size % 4:
                raise InvalidVideoOutput("frozen output has no supported MP4 header")
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != expected_sha256:
                raise InvalidVideoOutput("video digest does not match the uploaded bytes")
        finally:
            body.close()
    except InvalidVideoOutput:
        try:
            client.delete_object(Bucket=bucket, Key=frozen_key)
        except Exception as exc:
            logger.warning("Invalid video copy cleanup unavailable: %s", type(exc).__name__)
        raise
    return {
        "key": frozen_key,
        "url": f"{storage.public_media_base()}/{frozen_key}",
        "sha256": actual_sha256,
        "bytes": total,
        "content_type": "video/mp4",
    }


def freeze(slot: dict[str, Any], *, job_id: str, expected_sha256: str) -> FrozenVideo:
    """Bound copy/hash concurrency; return only a Core-owned verified object.

    InvalidVideoOutput permits the lease-owning caller to release its hold.
    Other exceptions mean uncertainty: do not pay, refund or acknowledge.
    Input/source/final retention remains a separately qualified storage policy.
    """
    if str(UUID(job_id)) != job_id:
        raise ValueError("video job ID must be canonical")
    if (
        slot.get("content_type") != "video/mp4"
        or not isinstance(slot.get("key"), str)
        or not re.fullmatch(rf"video/{re.escape(job_id)}/attempt-[0-9a-f]{{32}}/0\.mp4", slot["key"])
        or not isinstance(expected_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise ValueError("video upload contract is invalid")
    if not _slots.acquire(timeout=SLOT_WAIT_SECONDS):
        raise TimeoutError("video verification capacity is busy")
    try:
        return _freeze(slot, job_id, expected_sha256)
    finally:
        _slots.release()

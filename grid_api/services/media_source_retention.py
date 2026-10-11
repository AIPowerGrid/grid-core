# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Delete exact, Core-recorded private inputs only after financial closure.

Never infer deletion authority from historical payloads or elapsed time. The
provider lifecycle remains the backstop for uploads whose admission never commits.
"""

import asyncio
import os
import re
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import boto3
import sqlalchemy as sa
from botocore.client import Config as BotoConfig

from ..database import new_session
from ..v2.schema import media_jobs, reservations
from . import storage

MAX_OBJECTS = 18
_HOST = re.compile(r"[0-9a-f]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_KEY = re.compile(r"source/[0-9a-f]{32}\.(?:png|jpg|webp|wav|mp4)")
_sweep_lock = asyncio.Lock()


def _endpoint() -> str:
    endpoint = os.getenv("R2_TRANSIENT_ACCOUNT", "").rstrip("/")
    url = urlsplit(endpoint)
    if url.scheme != "https" or not _HOST.fullmatch(url.netloc) or url.path or url.query or url.fragment:
        raise ValueError("private source storage endpoint is unavailable")
    return endpoint


def _bucket(value: str) -> None:
    if not isinstance(value, str) or not _BUCKET.fullmatch(value) or value == storage.media_bucket():
        raise ValueError("private source bucket is invalid")


def capture(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Called only on the Core-compiled snapshot, inside hold+journal admission."""
    urls = []
    if payload.get("source_image_url"):
        urls.append(payload["source_image_url"])
    images, media = payload.get("source_image_urls", []), payload.get("source_media", [])
    if (
        not isinstance(images, list)
        or len(images) > 16
        or not isinstance(media, list)
        or len(media) > 3
        or any(not isinstance(item, dict) for item in media)
    ):
        raise ValueError("private source input collection is invalid")
    if len(media) == 3 and (
        payload.get("appearance_profile") != "ingredients-dev-v1"
        or payload.get("source_image_url")
        or images
        or [item.get("role") for item in media] != ["reference_image", "reference_audio", "reference_video"]
    ):
        raise ValueError("three private sources require the separate Ingredients profile")
    urls.extend(images)
    urls.extend(item.get("url") for item in media)
    if not urls:
        return None
    endpoint, bucket = _endpoint(), storage.private_source_bucket()
    _bucket(bucket)
    keys = set()
    for value in urls:
        if not isinstance(value, str) or not 0 < len(value) <= 8192:
            raise ValueError("private source URL is invalid")
        url = urlsplit(value)
        key = url.path.removeprefix(f"/{bucket}/")
        if (
            url.scheme != "https"
            or f"https://{url.netloc}" != endpoint
            or url.fragment
            or not url.query
            or not _KEY.fullmatch(key)
            or url.path != f"/{bucket}/{key}"
        ):
            raise ValueError("private source URL is outside Core storage")
        keys.add(key)
    if not 1 <= len(keys) <= MAX_OBJECTS:
        raise ValueError("private source object count is invalid")
    return {"endpoint": endpoint, "bucket": bucket, "keys": sorted(keys)}


def validate(record: dict[str, Any]) -> None:
    """Validate the entire stored authority before deleting even one object."""
    if not isinstance(record, dict) or set(record) != {"endpoint", "bucket", "keys"}:
        raise ValueError("private source retention record is invalid")
    if record["endpoint"] != _endpoint():
        raise ValueError("private source storage account changed")
    _bucket(record["bucket"])
    keys = record["keys"]
    if (
        not isinstance(keys, list)
        or not 1 <= len(keys) <= MAX_OBJECTS
        or any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in keys)
        or len(set(keys)) != len(keys)
    ):
        raise ValueError("private source retention keys are invalid")


@lru_cache
def _client(endpoint: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        config=BotoConfig(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=5,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )


def delete(record: dict[str, Any]) -> None:
    validate(record)
    client = _client(record["endpoint"])
    for key in record["keys"]:
        result = client.delete_object(Bucket=record["bucket"], Key=key)
        if result.get("ResponseMetadata", {}).get("HTTPStatusCode") not in {200, 204}:
            raise RuntimeError("private source deletion was not confirmed")


def _closed() -> sa.Select[Any]:
    return sa.select(reservations.c.job_id).where(
        reservations.c.status == "settled",
        reservations.c.settled.is_not(None),
    )


async def sweep(*, limit: int = 2) -> dict[str, int]:
    """Bounded single-flight cleanup; retries cannot change financial history.

    Recorded old private buckets remain eligible after bucket rotation, but a
    storage-account change or rebinding one as public output fails closed.
    No admission flag is consulted: accepted jobs still clean up after rollback.
    """
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("private source cleanup batch is invalid")
    counts = {"deleted": 0, "errors": 0}
    if _sweep_lock.locked():
        return counts
    async with _sweep_lock:
        async with await new_session() as session:
            rows = (
                (
                    await session.execute(
                        sa.select(media_jobs.c.job_id, media_jobs.c.source_objects)
                        .where(
                            media_jobs.c.source_objects.is_not(None),
                            media_jobs.c.source_cleanup_at.is_(None),
                            media_jobs.c.job_id.in_(_closed()),
                        )
                        .order_by(sa.func.coalesce(media_jobs.c.source_cleanup_attempted_at, media_jobs.c.created), media_jobs.c.job_id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            # A crash before this update merely retries the same exact keys.
            async with await new_session() as session:
                await session.execute(
                    media_jobs.update()
                    .where(
                        media_jobs.c.job_id == row["job_id"],
                        media_jobs.c.source_cleanup_at.is_(None),
                        media_jobs.c.job_id.in_(_closed()),
                    )
                    .values(source_cleanup_attempted_at=datetime.now(UTC))
                )
                await session.commit()
            task = asyncio.create_task(asyncio.to_thread(delete, row["source_objects"]))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Do not free the single-flight guard while a delete is in flight.
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception:
                counts["errors"] += 1
                continue
            async with await new_session() as session:
                result: Any = await session.execute(
                    media_jobs.update()
                    .where(
                        media_jobs.c.job_id == row["job_id"],
                        media_jobs.c.source_cleanup_at.is_(None),
                        media_jobs.c.job_id.in_(_closed()),
                    )
                    .values(source_cleanup_at=datetime.now(UTC))
                )
                await session.commit()
                counts["deleted"] += result.rowcount
    return counts

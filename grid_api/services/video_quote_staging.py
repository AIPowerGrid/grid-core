# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private, quote-bound inputs before any payment sender or executable outbox.

SQL records exact private keys before storage I/O. Retries retain those keys;
only verified stored bytes can become ready. No route or sender calls this yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

import sqlalchemy as sa
from botocore.exceptions import ClientError

from ..database import new_session
from ..v2.schema import video_quote_staging as stages
from . import media_source_retention as retention
from . import storage, video_plans
from . import video_payment_attempts as attempts
from . import video_quotes as quotes

LEASE_SECONDS = 180
STORAGE_SECONDS = 120
MAX_OBJECT_BYTES = 32 * 1024 * 1024
_active: set = set()


class VideoStageError(ValueError):
    """Opaque staging failure; never expose input data, URLs or SQL values."""


class VideoStageBusy(VideoStageError):
    """A bounded local slot or current quote lease is already occupied."""


def _now():
    return datetime.now(UTC)


@contextmanager
def _slot(quote_id):
    if quote_id in _active or len(_active) >= 2:
        raise VideoStageBusy("video input staging is busy")
    _active.add(quote_id)
    try:
        yield
    finally:
        _active.remove(quote_id)


def _objects(manifest):
    return manifest["images"] + manifest["sources"]


def _extension(commitment):
    if "role" in commitment:
        ext = {"reference_image": "png", "reference_audio": "wav", "reference_video": "mp4"}.get(commitment["role"])
        if ext is None or commitment.get("content_type") != storage.CONTENT_TYPES[ext]:
            raise VideoStageError("unsupported private source role")
        return ext
    return commitment["ext"]


def authority(manifest):
    items = _objects(manifest)
    if not items:
        return None
    record = {"endpoint": manifest["endpoint"], "bucket": manifest["bucket"], "keys": [item["key"] for item in items]}
    retention.validate(record)
    return record


def _manifest(quote):
    inputs = quote["snapshot"]["plan"]["inputs"]
    value = {"schema": "aipg.video-source-manifest.v1", "endpoint": None, "bucket": None, "images": [], "sources": []}
    if inputs["images"] or inputs["sources"]:
        value.update(endpoint=retention._endpoint(), bucket=storage.private_source_bucket())
    for group in ("images", "sources"):
        value[group] = [{"key": f"source/{uuid4().hex}.{_extension(item)}", "commitment": dict(item)} for item in inputs[group]]
    authority(value)
    return value


def _etag(value):
    if (type(value) is not str or not 2 < len(value) <= 256 or not value.startswith('"')
            or not value.endswith('"') or any(ord(char) < 32 or ord(char) > 126 for char in value)):
        raise VideoStageError("invalid private source object identity")
    return value


def _validate(row, quote):
    quotes._validate_row(quote)
    manifest = row["manifest"]
    if (row["quote_id"] != quote["id"] or row["quote_commitment"] != quote["commitment"]
            or type(manifest) is not dict or set(manifest) != {"schema", "endpoint", "bucket", "images", "sources"}
            or manifest["schema"] != "aipg.video-source-manifest.v1" or row["manifest_hash"] != quotes._hash(manifest)
            or not quotes._aware(quote["created"]) <= quotes._aware(row["created"]) < quotes._aware(quote["expires"])
            or quotes._aware(row["lease_until"]) <= quotes._aware(row["created"])
            or quotes._uuid(row["lease_token"]).version != 4):
        raise VideoStageError("stored video staging binding changed")
    inputs = quote["snapshot"]["plan"]["inputs"]
    for group, limit in (("images", 16), ("sources", 3)):
        items = manifest[group]
        if (type(items) is not list or len(items) > limit
                or any(type(item) is not dict or set(item) != {"key", "commitment"} for item in items)
                or [item["commitment"] for item in items] != inputs[group]):
            raise VideoStageError("stored video source commitments changed")
        for item in items:
            commitment = item["commitment"]
            ext = _extension(commitment)
            if (type(commitment["bytes"]) is not int or not 0 < commitment["bytes"] <= MAX_OBJECT_BYTES
                    or not re.fullmatch(r"[0-9a-f]{64}", commitment["sha256"])
                    or ext not in {"png", "jpg", "webp", "wav", "mp4"}
                    or not item["key"].endswith("." + ext)):
                raise VideoStageError("stored video source identity changed")
    authority(manifest)
    if not _objects(manifest) and (manifest["endpoint"] is not None or manifest["bucket"] is not None):
        raise VideoStageError("source-free staging cannot adopt storage authority")
    ready = row["status"] == "ready"
    if (row["status"] not in {"staging", "ready"}
            or ready != (row["ready_at"] is not None and row["proof"] is not None and row["proof_hash"] is not None)
            or (not ready and any(row[key] is not None for key in ("ready_at", "proof", "proof_hash")))):
        raise VideoStageError("stored video staging state changed")
    if ready:
        proof = row["proof"]
        if (type(proof) is not dict or set(proof) != {"schema", "manifest_hash", "objects"}
                or proof["schema"] != "aipg.video-source-proof.v1" or proof["manifest_hash"] != row["manifest_hash"]
                or row["proof_hash"] != quotes._hash(proof) or type(proof["objects"]) is not list
                or len(proof["objects"]) != len(_objects(manifest))
                or quotes._aware(row["ready_at"]) < quotes._aware(row["created"])):
            raise VideoStageError("stored video source proof changed")
        for observed, expected in zip(proof["objects"], _objects(manifest), strict=True):
            if (type(observed) is not dict or set(observed) != {"key", "sha256", "bytes", "etag"}
                    or any(observed[key] != expected["commitment"][key] for key in ("sha256", "bytes"))
                    or observed["key"] != expected["key"]):
                raise VideoStageError("stored video source proof changed")
            _etag(observed["etag"])
    return row


async def _owned(session, account_id, quote_id, *, lock=False):
    quote, attempt = await attempts._owned(session, account_id, quote_id, lock=lock)
    query = sa.select(stages).where(stages.c.quote_id == quote["id"])
    row = (await session.execute(query.with_for_update() if lock else query)).mappings().one_or_none()
    if row is not None:
        row = dict(row)
        for key in ("created", "lease_until", "ready_at"):
            if row[key] is not None:
                row[key] = quotes._aware(row[key])
        _validate(row, quote)
    return quote, attempt, row


async def lookup(account_id, quote_id):
    """Owned recovery only; no uploads, writes, decoding or payment permission."""
    try:
        async with await new_session() as session:
            _quote, _attempt, row = await _owned(session, account_id, quote_id)
            return row
    except VideoStageError:
        raise
    except Exception:
        raise VideoStageError("video input staging unavailable") from None


async def _allocate(account_id, quote_id, plan, admission_until):
    plan.check_inputs()
    async with await new_session() as session:
        quote, attempt, row = await _owned(session, account_id, quote_id, lock=True)
        plan.check_inputs()
        if plan.snapshot != quote["snapshot"]["plan"] or plan.sha256 != quote["plan_hash"]:
            raise VideoStageError("inputs do not match the original video quote")
        if row is not None and row["status"] == "ready":
            return row, False
        now = _now()
        if (attempt is not None or quotes._aware(quote["expires"]) <= now
                or admission_until <= now):
            raise VideoStageError("new video staging is closed")
        if row is not None and quotes._aware(row["lease_until"]) > now:
            raise VideoStageBusy("video input staging is busy")
        token, until = uuid4(), now + timedelta(seconds=LEASE_SECONDS)
        if row is None:
            manifest = _manifest(quote)
            row = dict(quote_id=quote["id"], quote_commitment=quote["commitment"], manifest=manifest,
                       manifest_hash=quotes._hash(manifest), status="staging", lease_token=token, lease_until=until,
                       created=now, ready_at=None, proof=None, proof_hash=None)
            _validate(row, quote)
            await session.execute(stages.insert().values(**row))
        else:
            row.update(lease_token=token, lease_until=until)
            await session.execute(stages.update().where(stages.c.quote_id == quote["id"])
                                  .values(lease_token=token, lease_until=until))
        if min(quotes._aware(quote["expires"]), admission_until) <= _now():
            raise VideoStageError("video staging expired before commit")
        await session.commit()
        return row, True


def _upload(row, raw_objects):
    """Bounded S3 work; conditional writes and actual readback, never URL fetches."""
    manifest = row["manifest"]
    record = authority(manifest)
    client = retention._client(record["endpoint"]) if record else None
    deadline = monotonic() + STORAGE_SECONDS
    observed = []
    for item, raw in zip(_objects(manifest), raw_objects, strict=True):
        commitment = item["commitment"]
        if len(raw) != commitment["bytes"] or hashlib.sha256(raw).hexdigest() != commitment["sha256"]:
            raise VideoStageError("source bytes changed before upload")
        if monotonic() >= deadline:
            raise VideoStageError("video source storage deadline exceeded")
        try:
            client.put_object(Bucket=record["bucket"], Key=item["key"], Body=raw,
                              ContentType=storage.CONTENT_TYPES[_extension(commitment)], IfNoneMatch="*")
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
        result = client.get_object(Bucket=record["bucket"], Key=item["key"])
        body = result["Body"]
        try:
            if (result.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                    or type(result.get("ContentLength")) is not int or result["ContentLength"] != len(raw)):
                raise VideoStageError("stored source size is unproven")
            digest, size = hashlib.sha256(), 0
            while True:
                if monotonic() >= deadline:
                    raise VideoStageError("video source storage deadline exceeded")
                chunk = body.read(min(64 * 1024, len(raw) - size + 1))
                if type(chunk) is not bytes or size + len(chunk) > len(raw):
                    raise VideoStageError("stored source size is unproven")
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            if size != len(raw) or digest.hexdigest() != commitment["sha256"]:
                raise VideoStageError("stored source bytes are unproven")
            observed.append({"key": item["key"], "sha256": digest.hexdigest(), "bytes": size, "etag": _etag(result.get("ETag"))})
        finally:
            body.close()
    return {"schema": "aipg.video-source-proof.v1", "manifest_hash": row["manifest_hash"], "objects": observed}


async def _finish(account_id, quote_id, lease_token, proof):
    async with await new_session() as session:
        quote, _attempt, row = await _owned(session, account_id, quote_id, lock=True)
        if row is None or row["lease_token"] != lease_token:
            raise VideoStageError("video staging lease changed")
        if row["status"] == "ready":
            return row
        if quotes._aware(row["lease_until"]) <= _now():
            raise VideoStageError("video staging lease expired")
        row.update(status="ready", ready_at=_now(), proof=proof, proof_hash=quotes._hash(proof))
        _validate(row, quote)
        await session.execute(stages.update().where(stages.c.quote_id == row["quote_id"])
                              .values(**{key: row[key] for key in ("status", "ready_at", "proof", "proof_hash")}))
        await session.commit()
        return row


async def stage(account_id, quote_id, plan: video_plans.FrozenVideoPlan, *, admission_until: datetime):
    """Stage only the original in-memory plan; a replay never uploads again."""
    try:
        if (not isinstance(plan, video_plans.FrozenVideoPlan) or not isinstance(admission_until, datetime)
                or admission_until.tzinfo is None):
            raise VideoStageError("explicit video staging policy required")
        quote_id = quotes._uuid(quote_id)
        with _slot(quote_id):
            row, winner = await _allocate(account_id, quote_id, plan, admission_until)
            if not winner:
                return row
            plan.check_inputs()
            raw = [image[0] for image in plan.inputs.images] + [source.data for source in plan.inputs.sources]
            task = asyncio.create_task(asyncio.to_thread(_upload, row, raw))
            try:
                proof = await asyncio.shield(task)
            except asyncio.CancelledError:
                await asyncio.gather(task, return_exceptions=True)
                raise
            return await _finish(account_id, quote_id, row["lease_token"], proof)
    except VideoStageError:
        raise
    except Exception:
        raise VideoStageError("private video input staging failed") from None


async def require_ready_in_session(session, account_id, quote_id):
    """Future admission gate, not payment or dispatch authority by itself."""
    quote, _attempt, row = await _owned(session, account_id, quote_id)
    if row is None or row["status"] != "ready":
        raise VideoStageError("verified video inputs are unavailable")
    return quote, row


async def dispatch_payload(account_id, quote_id, *, expires=3600):
    """Refresh private GET locations without changing the frozen execution plan."""
    try:
        if type(expires) is not int or not 1 <= expires <= 3600:
            raise VideoStageError("bounded source URL expiry required")
        async with await new_session() as session:
            quote, row = await require_ready_in_session(session, account_id, quote_id)
        manifest, payload = row["manifest"], quote["snapshot"]["plan"]["payload"]
        record = authority(manifest)
        client = retention._client(record["endpoint"]) if record else None

        def url(item):
            value = client.generate_presigned_url("get_object", Params={"Bucket": record["bucket"], "Key": item["key"]}, ExpiresIn=expires)
            parsed = urlsplit(value)
            if (type(value) is not str or not 0 < len(value) <= 8192
                    or f"{parsed.scheme}://{parsed.netloc}" != record["endpoint"]
                    or parsed.path != f"/{record['bucket']}/{item['key']}" or not parsed.query or parsed.fragment):
                raise VideoStageError("private source URL is outside recorded storage")
            return value

        images = [url(item) for item in manifest["images"]]
        if images:
            payload["source_image_url"] = images[0]
            if "source_image_urls" in payload:
                payload["source_image_urls"] = images
        if manifest["sources"]:
            payload["source_media"] = [{**item["commitment"], "url": url(item)} for item in manifest["sources"]]
        return payload, record
    except VideoStageError:
        raise
    except Exception:
        raise VideoStageError("staged video recovery unavailable") from None

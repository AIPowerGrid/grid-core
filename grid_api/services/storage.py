# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""R2/S3 presigned uploads for media workers.

Workers never hold storage credentials: at dispatch time the server presigns
one PUT URL per expected output, the worker uploads directly to R2, and the
client gets the public URL. Presigning is pure local crypto (no network
round-trip), so calling boto3's sync API inline is safe in async handlers.
"""

import logging
import os
from functools import lru_cache
from uuid import uuid4

import boto3
from botocore.client import Config as BotoConfig

logger = logging.getLogger("grid_api.storage")

CONTENT_TYPES = {
    "webp": "image/webp",
    "png": "image/png",
    "jpg": "image/jpeg",
    "mp4": "video/mp4",
    "webm": "video/webm",
    # 3D mesh artifacts (TRELLIS etc.)
    "glb": "model/gltf-binary",
    "gltf": "model/gltf+json",
    "ply": "application/octet-stream",
    "obj": "text/plain",
}


@lru_cache
def _client():
    endpoint = os.getenv("R2_TRANSIENT_ACCOUNT", "")
    if not endpoint:
        raise RuntimeError("R2_TRANSIENT_ACCOUNT not configured — media uploads unavailable")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        config=BotoConfig(signature_version="s3v4"),
    )


def media_bucket() -> str:
    return os.getenv("R2_TRANSIENT_BUCKET", "aipg-transient")


def public_media_base() -> str:
    """Public base URL fronting the media bucket (Cloudflare-routed)."""
    return os.getenv("GRID_MEDIA_BASE_URL", "https://images.aipg.art").rstrip("/")


def upload_source(data: bytes, ext: str, expires: int = 3600) -> str:
    """Store a caller-supplied source image (img2img / img2video input) and return a
    presigned GET URL the worker fetches. Same bucket, `source/` prefix (so a
    lifecycle rule can reap inputs quickly). Network PUT — call via asyncio.to_thread
    from async handlers. Returns a presigned URL, not a public one (inputs aren't
    meant to be publicly addressable)."""
    key = f"source/{uuid4().hex}.{ext}"
    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
    _client().put_object(Bucket=media_bucket(), Key=key, Body=data, ContentType=content_type)
    return _client().generate_presigned_url(
        "get_object", Params={"Bucket": media_bucket(), "Key": key}, ExpiresIn=expires
    )


def presign_outputs(job_id: str, n: int, ext: str, expires: int = 900,
                    job_type: str = "image") -> list[dict]:
    """Presign PUT URLs for a job's expected outputs.

    Returns one slot per output: {key, put_url, content_type, public_url}.

    Keys are prefixed by media type (`image/…`, `video/…`, `3d/…`) so a single
    bucket can carry all types yet still take prefix-scoped R2 lifecycle rules —
    e.g. expire `video/` sooner (it's ~5× the storage/egress), keep `3d/` meshes
    longer (they're downloadable deliverables, not previews) — without a separate
    bucket or domain.
    """
    bucket = media_bucket()
    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
    prefix = {"video": "video", "3d": "3d"}.get(job_type, "image")
    slots = []
    for i in range(n):
        key = f"{prefix}/{job_id}/{i}.{ext}"
        put_url = _client().generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires,
        )
        slots.append(
            {
                "key": key,
                "put_url": put_url,
                "content_type": content_type,
                "public_url": f"{public_media_base()}/{key}",
            }
        )
    return slots

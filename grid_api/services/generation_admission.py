# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Operational admission for new public inference, independent of charging mode."""

from fastapi import HTTPException

from ..config import get_settings


def require_path(path: str) -> None:
    if path not in get_settings().generation_enabled_paths:
        raise HTTPException(status_code=503, detail="This generation path is temporarily unavailable.")


def require_media(job_type: str, payload: dict) -> None:
    require_path(job_type)
    if job_type == "image":
        if payload.get("source_image_url"):
            require_path("image-to-image")
        if int(payload.get("n", 1) or 1) > 1:
            require_path("image-batch")
    elif job_type == "video":
        if payload.get("source_image_url"):
            require_path("image-to-video")
        if payload.get("timeline") or (payload.get("recipe_inputs") or {}).get("timeline"):
            require_path("video-timeline")

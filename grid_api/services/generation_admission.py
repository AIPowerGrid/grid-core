# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Operational admission for new public inference, independent of charging mode."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException

from ..config import GridSettings, get_settings


def require_async_video(settings: GridSettings, account_id: UUID | str, *, model: str = "LTX-2.5 Distilled BF16") -> datetime | None:
    """New jobs only; return the captured pilot deadline for the hold transaction."""
    if settings.media_async_enabled:
        if settings.media_async_admission_mode == "open":
            if model != "LTX-2.5 Distilled BF16":
                raise HTTPException(503, detail="This video model is not enabled for public async admission")
            return None
        try:
            owner = UUID(str(account_id))
        except (ValueError, TypeError, AttributeError):
            owner = None
        until = settings.media_async_canary_until
        if (settings.media_async_admission_mode == "pilot" and owner is not None
                and owner in settings.media_async_canary_accounts and until is not None
                and until.tzinfo is not None and datetime.now(UTC) < until):
            return until
    raise HTTPException(503, detail="Async video admission is unavailable; recover any existing receipt")


def require_waiting_video(model: str) -> None:
    """Typed waiting/chat needs its own opt-in, not just public async admission."""
    from . import recipes

    if any(recipes.video_contract_for_model(model, has_source=source) is not None
           for source in (False, True)):
        settings = get_settings()
        if not (settings.media_async_enabled and settings.media_async_admission_mode == "open"
                and settings.typed_video_waiting_enabled):
            raise HTTPException(503, detail="This video model requires the controlled async video job endpoint")


def validate_rollout(charging_mode: str) -> None:
    """Global paid activation must deliberately select its verified surface."""
    if charging_mode == "on" and "generation_enabled_paths" not in get_settings().model_fields_set:
        raise RuntimeError("Global charging requires explicit GENERATION_ENABLED_PATHS; [] closes all paths")


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
        if payload.get("source_media"):
            require_path("audio-to-video")
            if any(source.get("role") == "reference_image" for source in payload["source_media"]):
                require_path("appearance-to-video")
            if any(source.get("role") == "reference_video" for source in payload["source_media"]):
                require_path("video-to-video")
        if payload.get("source_image_url"):
            require_path("image-to-video")
        if (payload.get("timeline") or (payload.get("recipe_inputs") or {}).get("timeline")
                or len((payload.get("recipe_inputs") or {}).get("keyframes") or []) > 1):
            require_path("video-timeline")

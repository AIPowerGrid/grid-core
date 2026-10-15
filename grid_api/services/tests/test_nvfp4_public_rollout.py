# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from uuid import uuid4

import pytest
from fastapi import HTTPException

from grid_api.config import GridSettings
from grid_api.services.generation_admission import require_async_video

MODEL = "LTX-2.5 Distilled NVFP4"


def test_public_nvfp4_is_explicit_and_does_not_open_sources_or_other_models():
    owner = uuid4()
    closed = GridSettings(_env_file=None, media_async_enabled=True, media_async_admission_mode="open")
    with pytest.raises(HTTPException):
        require_async_video(closed, owner, model=MODEL)
    opened = GridSettings(_env_file=None, media_async_enabled=True, media_async_admission_mode="open",
                          media_async_nvfp4_enabled=True, media_async_source_admission_mode="open")
    assert require_async_video(opened, owner, model=MODEL) is None
    assert require_async_video(opened, owner) is None
    for model, source in [(MODEL, True), ("LTX-2.5 Dev BF16", False), ("unknown-video", False)]:
        with pytest.raises(HTTPException):
            require_async_video(opened, owner, model=model, has_source=source)
    opened.media_async_enabled = False
    with pytest.raises(HTTPException):
        require_async_video(opened, owner, model=MODEL)


def test_nvfp4_flag_is_default_off_and_rejects_invalid_configuration(monkeypatch):
    monkeypatch.delenv("MEDIA_ASYNC_NVFP4_ENABLED", raising=False)
    assert GridSettings(_env_file=None).media_async_nvfp4_enabled is False
    monkeypatch.setenv("MEDIA_ASYNC_NVFP4_ENABLED", "1")
    assert GridSettings(_env_file=None).media_async_nvfp4_enabled is True
    monkeypatch.setenv("MEDIA_ASYNC_NVFP4_ENABLED", "not-a-boolean")
    with pytest.raises(ValueError):
        GridSettings(_env_file=None)

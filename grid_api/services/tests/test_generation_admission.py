# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from typing import get_args
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from grid_api.config import GenerationPath, GridSettings
from grid_api.models.openai import ChatCompletionRequest
from grid_api.routers import _passthrough, openai
from grid_api.services import credits, generation_admission, media


def configure(monkeypatch, paths):
    settings = GridSettings(_env_file=None, generation_enabled_paths=paths)
    monkeypatch.setattr(generation_admission, "get_settings", lambda: settings)


def test_default_preserves_known_paths():
    assert GridSettings(_env_file=None).generation_enabled_paths == frozenset(get_args(GenerationPath))


def test_unknown_path_configuration_rejected():
    with pytest.raises(ValidationError):
        GridSettings(_env_file=None, generation_enabled_paths=["image", "typo"])


def test_environment_json_allowlist_and_empty_deny_all(monkeypatch):
    monkeypatch.setenv("GENERATION_ENABLED_PATHS", '["image"]')
    assert GridSettings(_env_file=None).generation_enabled_paths == frozenset({"image"})
    monkeypatch.setenv("GENERATION_ENABLED_PATHS", '[]')
    assert not GridSettings(_env_file=None).generation_enabled_paths
    monkeypatch.setenv("GENERATION_ENABLED_PATHS", '["typo"]')
    with pytest.raises(ValidationError):
        GridSettings(_env_file=None)


@pytest.mark.parametrize("path", get_args(GenerationPath))
def test_empty_configuration_closes_every_path(monkeypatch, path):
    configure(monkeypatch, [])
    with pytest.raises(HTTPException) as error:
        generation_admission.require_path(path)
    assert error.value.status_code == 503


@pytest.mark.parametrize("path", get_args(GenerationPath))
def test_explicitly_selected_path_allowed(monkeypatch, path):
    configure(monkeypatch, [path])
    generation_admission.require_path(path)


@pytest.mark.parametrize("kind,payload,allowed", [
    ("image", {"n": 1}, ["openai-chat"]),
    ("image", {"n": 2}, ["image"]),
    ("image", {"source_image_url": "https://example.test/source"}, ["image"]),
    ("video", {"n": 1}, ["image"]),
    ("video", {"source_image_url": "https://example.test/source"}, ["video"]),
    ("video", {"recipe_inputs": {"timeline": "[]"}}, ["video"]),
    ("audio", {}, ["image"]),
    ("3d", {}, ["image"]),
])
@pytest.mark.asyncio
async def test_media_rejected_before_reserve_or_dispatch(monkeypatch, kind, payload, allowed):
    configure(monkeypatch, allowed)
    reserve = AsyncMock(side_effect=AssertionError("must not reserve"))
    dispatch = AsyncMock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(credits, "authorize_media", reserve)
    monkeypatch.setattr(media, "_submit_and_wait_inner", dispatch)
    with pytest.raises(HTTPException) as error:
        await media.submit_and_wait("test-model", kind, payload, 1, billing_user={})
    assert error.value.status_code == 503
    reserve.assert_not_awaited()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_rejected_before_worker_lookup_or_reserve(monkeypatch):
    configure(monkeypatch, ["image"])
    monkeypatch.setattr(openai, "_detect_media_model", AsyncMock(return_value=None))
    workers = AsyncMock(side_effect=AssertionError("must not look up workers"))
    reserve = AsyncMock(side_effect=AssertionError("must not reserve"))
    monkeypatch.setattr(openai, "get_available_models", workers)
    monkeypatch.setattr(credits, "authorize_request", reserve)
    request = ChatCompletionRequest(model="test", messages=[{"role": "user", "content": "hello"}])
    with pytest.raises(HTTPException) as error:
        await openai._handle_chat_completions_for_user(request, {})
    assert error.value.status_code == 503
    workers.assert_not_awaited()
    reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_media_shim_cannot_bypass_media_gate(monkeypatch):
    configure(monkeypatch, ["openai-chat"])
    monkeypatch.setattr(openai, "_detect_media_model", AsyncMock(return_value="image"))
    monkeypatch.setattr(openai.quota, "check_and_consume", AsyncMock())
    monkeypatch.setattr(media, "diffusion_params", lambda *_args: (4, 1, "euler"))
    reserve = AsyncMock(side_effect=AssertionError("must not reserve"))
    monkeypatch.setattr(credits, "authorize_media", reserve)
    request = ChatCompletionRequest(model="test-image", messages=[{"role": "user", "content": "hello"}])
    with pytest.raises(HTTPException) as error:
        await openai._handle_chat_completions_for_user(request, {})
    assert error.value.status_code == 503
    reserve.assert_not_awaited()


@pytest.mark.parametrize("api_format", ["openai-responses", "anthropic"])
@pytest.mark.asyncio
async def test_passthrough_rejected_before_token_count_or_reserve(monkeypatch, api_format):
    configure(monkeypatch, ["openai-chat"])
    reserve = AsyncMock(side_effect=AssertionError("must not reserve"))
    monkeypatch.setattr(credits, "authorize_request", reserve)
    with pytest.raises(HTTPException) as error:
        await _passthrough.authorize_passthrough({}, "test", api_format, {}, 8, "job")
    assert error.value.status_code == 503
    reserve.assert_not_awaited()


@pytest.mark.parametrize("kind,payload,paths", [
    ("image", {}, ["image"]),
    ("image", {"n": 2}, ["image", "image-batch"]),
    ("image", {"source_image_url": "source"}, ["image", "image-to-image"]),
    ("video", {"source_image_url": "source"}, ["video", "image-to-video"]),
    ("video", {"recipe_inputs": {"timeline": "[]"}}, ["video", "video-timeline"]),
    ("audio", {}, ["audio"]),
    ("3d", {}, ["3d"]),
])
def test_media_requires_base_and_optional_capabilities(monkeypatch, kind, payload, paths):
    configure(monkeypatch, paths)
    generation_admission.require_media(kind, payload)
    configure(monkeypatch, paths[1:])
    with pytest.raises(HTTPException):
        generation_admission.require_media(kind, payload)

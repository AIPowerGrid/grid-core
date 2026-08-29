# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from typing import Any

import pytest
from pydantic import ValidationError

from grid_api.models.openai import ChatCompletionRequest
from grid_api.routers.openai import _normalize_worker_request


def _request(**overrides: Any) -> ChatCompletionRequest:
    payload = {
        "model": "model-a",
        "messages": [{"role": "user", "content": "hello"}],
        **overrides,
    }
    return ChatCompletionRequest.model_validate(payload)


def test_current_openai_token_field_becomes_metered_worker_cap():
    request = _request(max_completion_tokens=32)

    assert request.max_tokens == 32
    dumped = request.model_dump(exclude_none=True)
    assert dumped["max_tokens"] == 32
    assert "max_completion_tokens" not in dumped


def test_matching_token_fields_normalize_once():
    request = _request(max_tokens=64, max_completion_tokens=64)

    assert request.max_tokens == 64
    assert "max_completion_tokens" not in request.model_extra


def test_conflicting_token_fields_are_rejected():
    with pytest.raises(ValidationError, match="must match"):
        _request(max_tokens=32, max_completion_tokens=64)


def test_current_openai_token_field_keeps_grid_bound():
    with pytest.raises(ValidationError):
        _request(max_completion_tokens=32769)


def test_empty_tools_are_omitted_from_worker_request():
    body = {
        "model": "model-a",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    normalized = _normalize_worker_request(body)

    assert "tools" not in normalized
    assert "tool_choice" not in normalized
    assert "parallel_tool_calls" not in normalized


def test_nonempty_tools_are_forwarded_unchanged():
    body = {
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    assert _normalize_worker_request(body) == body

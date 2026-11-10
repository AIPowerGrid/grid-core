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


# ── Reasoning effort is rewritten to a level every backend accepts ──────────
# vLLM rejects `high`, `max` and `minimal` with HTTP 400; Claude Code sends
# `high` on every request.


@pytest.mark.parametrize(
    ("sent", "forwarded"),
    [("low", "low"), ("medium", "medium"), ("MEDIUM", "medium"), ("minimal", "low"), ("none", "low"),
     ("high", None), ("xhigh", None), ("max", None), ("turbo", None), (3, None)],
)
def test_reasoning_effort_is_portable_on_every_api_format(sent, forwarded):
    from grid_api.routers._passthrough import normalize_reasoning_effort

    chat = _normalize_worker_request(_request(reasoning_effort=sent).model_dump(exclude_none=True))
    assert chat.get("reasoning_effort") == forwarded

    anthropic = normalize_reasoning_effort("anthropic", {"model": "m", "output_config": {"effort": sent}})
    assert (anthropic.get("output_config") or {}).get("effort") == forwarded
    assert ("output_config" in anthropic) is (forwarded is not None)

    responses = normalize_reasoning_effort(
        "openai-responses", {"model": "m", "reasoning": {"effort": sent, "summary": "auto"}},
    )
    assert responses["reasoning"].get("effort") == forwarded
    assert responses["reasoning"]["summary"] == "auto"  # sibling settings survive


def test_reasoning_effort_normalization_leaves_other_requests_alone():
    from grid_api.routers._passthrough import normalize_reasoning_effort

    claude_code = {
        "model": "m", "max_tokens": 32000, "stream": True,
        "thinking": {"type": "adaptive", "display": "omitted"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "output_config": {"effort": "high", "format": {"type": "json_schema"}},
    }
    original = {"type": "json_schema"}
    out = normalize_reasoning_effort("anthropic", claude_code)
    assert out["output_config"] == {"format": original}
    assert out["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert "context_management" in out

    for api_format in ("anthropic", "openai-responses", "openai-chat"):
        plain = {"model": "m", "reasoning": "not-a-dict", "output_config": None}
        assert normalize_reasoning_effort(api_format, dict(plain)) == plain
    assert "reasoning_effort" not in _normalize_worker_request(_request().model_dump(exclude_none=True))

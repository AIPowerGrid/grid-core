# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from grid_api.routers.openai import _normalize_worker_request


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

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Commit and meter assembled chat output, including reasoning and tool calls."""

from .den import count_tokens
from .ledger import canonical_hash, content_hash


def result_hash(content: str, reasoning: str = "", tool_calls: list | None = None) -> str | None:
    # Preserve the existing receipt for plain-text-only workers. A signature
    # over visible text alone must never authenticate a richer result.
    if not reasoning and not tool_calls:
        return content_hash(content)
    return canonical_hash({
        "schema": "aipg.chat-output.v2",
        "content": content,
        "reasoning_content": reasoning,
        "tool_calls": tool_calls or [],
    })


def completion_tokens(content: str, reasoning: str = "", tool_calls: list | None = None) -> int:
    """Grid tokenizer proxy over output fields, excluding transport IDs/indexes."""
    total = count_tokens(content) + count_tokens(reasoning)
    for call in tool_calls or []:
        function = call.get("function") or {}
        total += count_tokens(function.get("name") or "")
        total += count_tokens(function.get("arguments") or "")
    return total


def merge_tool_call_deltas(acc: dict, deltas: list):
    """Join function fragments by index, independent of stream chunk boundaries."""
    for tc in deltas or []:
        idx = tc.get("index", 0)
        slot = acc.setdefault(idx, {"index": idx, "id": None, "type": "function", "function": {"name": "", "arguments": ""}})
        if tc.get("id"):
            slot["id"] = tc["id"]
        if tc.get("type"):
            slot["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Response formatters for OpenAI and Anthropic API compatibility.

The streaming infrastructure is shared — workers stream tokens the same way.
These formatters just wrap tokens in the correct JSON envelope for each API.
"""

import time
import uuid


def _gen_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ── OpenAI format ──


def openai_chunk(content: str, model: str, completion_id: str, is_first: bool = False, is_last: bool = False, reasoning: str | None = None) -> dict:
    """Format a single token as an OpenAI streaming chunk.

    Reasoning is emitted in the standard `delta.reasoning_content` field
    (faithful passthrough from the worker), not mutated into inline <think> tags.
    """
    delta = {}
    if is_first:
        delta["role"] = "assistant"
    if reasoning:
        delta["reasoning_content"] = reasoning
    if content:
        delta["content"] = content

    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": "stop" if is_last else None,
            }
        ],
    }


def openai_response(content: str, model: str, prompt_tokens: int = 0, completion_tokens: int = 0, reasoning: str = "") -> dict:
    """Format a complete non-streaming OpenAI response."""
    message = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": _gen_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# ── Anthropic format ──


def anthropic_message_start(model: str, message_id: str) -> dict:
    """Anthropic `message_start` event."""
    return {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }


def anthropic_content_block_start(index: int = 0) -> dict:
    """Anthropic `content_block_start` event."""
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "text", "text": ""},
    }


def anthropic_content_block_delta(text: str, index: int = 0) -> dict:
    """Anthropic `content_block_delta` event — one token."""
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }


def anthropic_content_block_stop(index: int = 0) -> dict:
    """Anthropic `content_block_stop` event."""
    return {"type": "content_block_stop", "index": index}


def anthropic_message_delta(output_tokens: int = 0) -> dict:
    """Anthropic `message_delta` event (end of message)."""
    return {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }


def anthropic_message_stop() -> dict:
    """Anthropic `message_stop` event."""
    return {"type": "message_stop"}


def anthropic_response(content: str, model: str, input_tokens: int = 0, output_tokens: int = 0) -> dict:
    """Format a complete non-streaming Anthropic response."""
    return {
        "id": _gen_id("msg"),
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }

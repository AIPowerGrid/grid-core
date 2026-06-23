# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pydantic models for OpenAI-compatible API."""

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """One OpenAI chat message.

    Faithful-passthrough principle: validate only what we must, let everything
    else flow. `content` may be a string OR a list of content parts
    (multimodal: text + image_url). `role` includes "tool" so tool-result
    turns round-trip. `extra="allow"` keeps any field we don't model
    (e.g. `name`, `tool_calls`, `tool_call_id`, `refusal`) intact end-to-end."""

    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool", "developer"]
    # Optional: an assistant turn that only calls tools has content=null.
    content: Optional[Union[str, list[Any]]] = None


class ChatCompletionRequest(BaseModel):
    """OpenAI chat-completions request.

    `extra="allow"` is deliberate: a developer should be able to send any
    parameter their local vLLM accepts (logit_bias, seed, repetition_penalty,
    guided_json, …) and have it reach the worker unchanged. We only pin down
    the few fields the grid itself reads (model, messages, max_tokens, stream)
    plus light bounds on the common knobs."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=0.9, ge=0, le=1)
    max_tokens: Optional[int] = Field(default=512, ge=1, le=32768)
    stream: bool = False
    n: int = Field(default=1, ge=1, le=4)
    # Modeled so they're documented + validated; still forwarded verbatim.
    tools: Optional[list[Any]] = None
    tool_choice: Optional[Union[str, dict]] = None
    stop: Optional[Union[str, list[str]]] = None
    presence_penalty: Optional[float] = Field(default=None, ge=-2, le=2)
    frequency_penalty: Optional[float] = Field(default=None, ge=-2, le=2)
    seed: Optional[int] = None
    response_format: Optional[dict] = None
    stream_options: Optional[dict] = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: UsageInfo


class DeltaContent(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None


class StreamChoice(BaseModel):
    index: int = 0
    delta: DeltaContent
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[StreamChoice]


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "aipowergrid"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]

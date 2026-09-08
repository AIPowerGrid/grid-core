# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded native Responses observations, never worker-quality verdicts."""

import hashlib
import json
import math
from typing import Any

MAX_EVENT_BYTES = 65_536
MAX_POSITIONS = 32
MAX_ALTERNATIVES = 20
MAX_TOKEN_BYTES = 128
MAX_STREAM_BYTES = 1_048_576
MAX_STREAM_EVENTS = 2048
MAX_OUTPUT_BYTES = 16_384
MAX_DELTA_EVENTS = 128


class InvalidObservation(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidObservation
        result[key] = value
    return result


def _token(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidObservation
    token = value.get("token")
    number = value.get("logprob")
    if not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES:
        raise InvalidObservation
    if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise InvalidObservation
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        raise InvalidObservation
    if not -1_000_000 <= number <= 0 or not math.isfinite(number):
        raise InvalidObservation
    result = {"token": token, "logprob": number}
    if "bytes" in value:
        raw = value["bytes"]
        # Null byte representations are permitted by compatible APIs.
        if raw is not None and (
            not isinstance(raw, list) or len(raw) > MAX_TOKEN_BYTES or any(type(b) is not int or not 0 <= b <= 255 for b in raw)
        ):
            raise InvalidObservation
        result["bytes"] = list(raw) if raw is not None else None
    return result


def read_logprobs(data: str) -> dict[str, Any]:
    """Read only output-text delta logprobs; never double-count terminal copies.

    Keep token strings, numeric probabilities and byte sequences unchanged.
    Unknown fields are discarded instead of retaining arbitrary worker objects.
    This does not prove token/context alignment or the truth of worker reports.
    """
    empty = {"positions": []}
    if not isinstance(data, str) or len(data) > MAX_EVENT_BYTES:
        return {**empty, "status": "unavailable", "reason": "event_too_large_or_invalid"}
    try:
        if len(data.encode("utf-8")) > MAX_EVENT_BYTES:
            raise InvalidObservation
        event = json.loads(data, object_pairs_hook=_unique_object)
        if not isinstance(event, dict):
            raise InvalidObservation
        if event.get("type") != "response.output_text.delta":
            return {**empty, "status": "unavailable", "reason": "not_output_delta"}
        positions = event.get("logprobs")
        if positions is None or positions == []:
            return {**empty, "status": "unavailable", "reason": "missing_logprobs"}
        if not isinstance(positions, list) or len(positions) > MAX_POSITIONS:
            raise InvalidObservation
        clean = []
        for position in positions:
            selected = _token(position)
            alternatives = position.get("top_logprobs", [])
            if not isinstance(alternatives, list) or len(alternatives) > MAX_ALTERNATIVES:
                raise InvalidObservation
            selected["top_logprobs"] = [_token(item) for item in alternatives]
            clean.append(selected)
        return {"positions": clean, "status": "available", "reason": "native_logprobs"}
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        return {**empty, "status": "unavailable", "reason": "malformed_logprobs_or_event"}


class ObservationLimit(ValueError):
    """Stop the probe instead of retaining or draining an unbounded stream."""


class ResponsesObservation:
    """Collect one bounded stream with explicit gaps and visible-prefix hashes.

    Visible prefixes are NOT full model contexts: hidden reasoning, tokenizer and
    backend templates are not established here. This envelope deliberately cannot
    be mistaken for the existing chat first-distribution scoring contract.
    """

    def __init__(self):
        self.text = ""
        self.events = []
        self.terminal = None
        self.reason = None
        self._bytes = 0
        self._frames = 0
        self._positions = 0
        self._sequence = -1
        self._part = None

    def unavailable(self, reason: str):
        self.reason = self.reason or reason

    def add(self, data: str):
        self._frames += 1
        if self._frames > MAX_STREAM_EVENTS:
            raise ObservationLimit
        if not isinstance(data, str) or len(data) > MAX_EVENT_BYTES:
            raise ObservationLimit
        try:
            size = len(data.encode("utf-8"))
            self._bytes += size
            if size > MAX_EVENT_BYTES or self._bytes > MAX_STREAM_BYTES:
                raise ObservationLimit
            event = json.loads(data, object_pairs_hook=_unique_object)
            if not isinstance(event, dict):
                raise InvalidObservation
            kind = event.get("type")
            if self.terminal is not None:
                raise InvalidObservation
            if kind in {"response.completed", "response.incomplete", "response.failed", "error"}:
                self.terminal = kind
                if kind != "response.completed":
                    self.unavailable("backend_incomplete_or_error")
                return
            if kind != "response.output_text.delta":
                return
            sequence = event.get("sequence_number")
            indices = (event.get("output_index"), event.get("content_index"))
            item_id = event.get("item_id")
            if (
                type(sequence) is not int
                or not self._sequence < sequence < 2**53
                or any(type(i) is not int or not 0 <= i < 1024 for i in indices)
                or not isinstance(item_id, str)
                or not 0 < len(item_id) <= 128
                or len(item_id.encode("utf-8")) > 128
            ):
                raise InvalidObservation
            part = (item_id, *indices)
            if self._part is not None and part != self._part:
                self.unavailable("multiple_output_parts")
                return
            self._part = part
            self._sequence = sequence
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise InvalidObservation
            if len(delta) > MAX_OUTPUT_BYTES or len((self.text + delta).encode("utf-8")) > MAX_OUTPUT_BYTES:
                raise ObservationLimit
            observation = read_logprobs(data)
            self._positions += len(observation["positions"])
            if self._positions > MAX_POSITIONS or len(self.events) >= MAX_DELTA_EVENTS:
                raise ObservationLimit
            self.events.append(
                {
                    "sequence_number": sequence,
                    "item_id": item_id,
                    "output_index": indices[0],
                    "content_index": indices[1],
                    "visible_prefix_sha256": hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
                    "delta": delta,
                    **observation,
                },
            )
            self.text += delta
        except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError) as exc:
            if isinstance(exc, ObservationLimit):
                raise
            self.unavailable("malformed_event")

    def result(self) -> dict[str, Any]:
        reason = self.reason
        if self.terminal is None:
            reason = reason or "missing_backend_terminal"
        available = sum(event["status"] == "available" for event in self.events)
        status = "unavailable"
        if not reason and available:
            status = "available" if available == len(self.events) else "partial"
        return {
            "schema": "responses-logprobs-observation.v1",
            "api_format": "openai-responses",
            "status": status,
            "reason": reason or ("native_logprobs" if available else "missing_logprobs"),
            "terminal": self.terminal,
            "quality_eligible": False,
            "comparison_ready": False,
            "events": self.events,
        }

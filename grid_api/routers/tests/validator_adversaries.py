# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hostile worker actors for the validator anti-gaming contract tests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from grid_api.services.den import count_tokens


@dataclass(frozen=True)
class WorkerReply:
    text: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_text: str = ""
    finish_reason: str = "stop"


class PublicProbeClassifier:
    """A deliberately simple classifier built only from public prompt shapes."""

    _PROMPT_MARKERS = (
        re.compile(r"^Reply with exactly this token and nothing else:"),
        re.compile(r"^What is \d+ [*+] \d+\? Reply with only the number\.$"),
        re.compile(r"^Return exactly one valid JSON object and no markdown"),
        re.compile(r"^Read the record set below\. Find the record whose key"),
        re.compile(r"^Start with -?\d+\. In order,"),
        re.compile(r"^Write one Python function named transform_[0-9a-f]{8}"),
        re.compile(r"^Call (?:the )?(?:record|lookup)_[0-9a-f]{8}"),
        re.compile(r"^Output the following characters exactly,"),
        re.compile(r"^Repeat exactly [0-9A-F]+ separated only by single spaces\."),
    )

    def predicts_probe(self, request: dict[str, Any]) -> bool:
        prompt = _prompt(request)
        if any(pattern.search(prompt) for pattern in self._PROMPT_MARKERS):
            return True
        stop = request.get("stop")
        return isinstance(stop, str) and bool(re.fullmatch(r"<STOP_[0-9A-F]+>", stop))


class RegexTemplateWorker:
    """No model: solve the current public canary families with parsing alone."""

    def respond(self, request: dict[str, Any]) -> WorkerReply:
        prompt = _prompt(request)
        if request.get("tools"):
            return self._tool_reply(request, prompt)

        match = re.fullmatch(
            r"Reply with exactly this token and nothing else: ([0-9A-F]+)",
            prompt,
        )
        if match:
            return WorkerReply(text=match.group(1))

        match = re.fullmatch(
            r"What is (\d+) ([+*]) (\d+)\? Reply with only the number\.",
            prompt,
        )
        if match:
            left, operator, right = match.groups()
            value = int(left) + int(right) if operator == "+" else int(left) * int(right)
            return WorkerReply(text=str(value))

        match = re.fullmatch(
            r"Return exactly one valid JSON object and no markdown or explanation\. "
            r"Set '([^']+)' to (\d+), '([^']+)' to '([^']+)', and "
            r"'([^']+)' to (true|false)\.",
            prompt,
        )
        if match:
            number_key, number, token_key, token, flag_key, flag = match.groups()
            return WorkerReply(
                text=json.dumps(
                    {
                        number_key: int(number),
                        token_key: token,
                        flag_key: flag == "true",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

        match = re.search(r"key exactly equals ([0-9A-F]+)\.", prompt)
        if match:
            record = re.search(
                rf"key={re.escape(match.group(1))} value=([0-9A-F]+) checksum=",
                prompt,
            )
            return WorkerReply(text=record.group(1) if record else "")

        match = re.fullmatch(
            r"Start with (-?\d+)\. In order, (.+)\. Reply with only the final integer\.",
            prompt,
        )
        if match:
            value = int(match.group(1))
            for operation in match.group(2).split(", then "):
                operand = int(re.search(r"-?\d+", operation).group())
                if operation.startswith("add "):
                    value += operand
                elif operation.startswith("subtract "):
                    value -= operand
                else:
                    value *= operand
            return WorkerReply(text=str(value))

        match = re.fullmatch(
            r"Write one Python function named (transform_[0-9a-f]{8}) that accepts "
            r"exactly one integer argument named x\. It must multiply x by (\d+), add "
            r"(-?\d+), take the result modulo (\d+) using Python integer semantics, "
            r"then subtract (\d+)\. Return only the function definition with no "
            r"markdown, imports, calls, annotations, or explanation\.",
            prompt,
        )
        if match:
            name, multiplier, offset, modulus, adjustment = match.groups()
            return WorkerReply(
                text=(
                    f"def {name}(x):\n"
                    f"    return ((x * {multiplier} + {offset}) % {modulus}) - {adjustment}"
                )
            )

        stop = request.get("stop")
        if isinstance(stop, str):
            match = re.search(
                rf"explanation: ([0-9A-F]+){re.escape(stop)}[0-9A-F]+$",
                prompt,
            )
            if match:
                return WorkerReply(text=match.group(1))

        match = re.fullmatch(
            r"Repeat exactly ([0-9A-F]+) separated only by single spaces\. "
            r"Continue repeating it until the generation limit stops you\. Do not "
            r"stop early, add punctuation, or output any other visible text\.",
            prompt,
        )
        if match:
            token = match.group(1)
            target = max(2, int(request.get("max_tokens") or 0) // 2 + 2)
            pieces: list[str] = []
            while count_tokens(" ".join(pieces)) < target:
                pieces.append(token)
            return WorkerReply(text=" ".join(pieces), finish_reason="length")

        return WorkerReply(text="")

    def _tool_reply(self, request: dict[str, Any], prompt: str) -> WorkerReply:
        function = request["tools"][0]["function"]
        name = function["name"]
        required = function["parameters"]["required"]
        tool_message = next(
            (
                message
                for message in request.get("messages", [])
                if message.get("role") == "tool"
            ),
            None,
        )
        if tool_message:
            result = json.loads(tool_message["content"])
            arguments = {
                required[0]: int(result["left"]) + int(result["right"]),
                required[1]: result["token"],
            }
        else:
            arguments = {}
            for field in required:
                match = re.search(
                    rf"{re.escape(field)!r} (?:set )?to ('[^']+'|-?\d+)",
                    prompt,
                )
                if not match:
                    return WorkerReply(text="")
                raw = match.group(1)
                arguments[field] = raw[1:-1] if raw.startswith("'") else int(raw)
        return WorkerReply(
            tool_calls=[
                {
                    "id": "call_template_solver",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, separators=(",", ":")),
                    },
                }
            ]
        )


class ReplayCacheWorker:
    def __init__(self) -> None:
        self._cache: dict[str, WorkerReply] = {}

    def prime(self, request: dict[str, Any], reply: WorkerReply) -> None:
        self._cache[_request_key(request)] = reply

    def respond(self, request: dict[str, Any]) -> WorkerReply:
        return self._cache.get(_request_key(request), WorkerReply())


class ProbeAwareModelSwitchWorker:
    def __init__(
        self,
        classifier: PublicProbeClassifier,
        strong_backend: RegexTemplateWorker,
        weak_backend: Callable[[dict[str, Any]], WorkerReply],
    ) -> None:
        self.classifier = classifier
        self.strong_backend = strong_backend
        self.weak_backend = weak_backend
        self.strong_routes = 0
        self.weak_routes = 0

    def respond(self, request: dict[str, Any]) -> WorkerReply:
        if self.classifier.predicts_probe(request):
            self.strong_routes += 1
            return self.strong_backend.respond(request)
        self.weak_routes += 1
        return self.weak_backend(request)


def _prompt(request: dict[str, Any]) -> str:
    messages = request.get("messages") or []
    for message in messages:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _request_key(request: dict[str, Any]) -> str:
    return json.dumps(request, sort_keys=True, separators=(",", ":"))

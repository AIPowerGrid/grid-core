# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded contracts for the dark text-model fidelity witness lane."""

from __future__ import annotations

import json
import math
import re
import secrets
from typing import Any

SCHEMA = "aipg.validator.text.fidelity.challenge.v1"
POLICY_ID = "text.fidelity.v1"
KIND = "text.fidelity"
MAX_OUTPUT_BYTES = 8_192
MAX_PROMPT_BYTES = 4_096
MAX_DISTRIBUTION_TOKENS = 20
REFERENCE_AGREEMENT_MAX = 0.08
CANDIDATE_MATCH_MAX = 0.12
CANDIDATE_ANOMALY_MIN = 0.30

_SUBJECTS = (
    "the careful cartographer",
    "a tired compiler engineer",
    "the night archivist",
    "an unusually patient navigator",
    "the observatory technician",
    "a skeptical editor",
)
_OBJECTS = (
    "brass compass",
    "weathered notebook",
    "glass instrument",
    "sealed parcel",
    "unfinished map",
    "silent radio",
    "mechanical clock",
    "field report",
)
_SETTINGS = (
    "after the storm cleared",
    "before the final train arrived",
    "while the generators warmed",
    "as the room grew quiet",
    "during the last inspection",
    "when the signal returned",
)
_QUALITIES = (
    "unexpected",
    "reassuring",
    "fragile",
    "peculiar",
    "promising",
    "uncertain",
    "familiar",
    "precise",
)


class FidelityContractError(ValueError):
    """Raised when a challenge or witness crosses the bounded contract."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def make_challenge(
    reference_worker_ids: list[str],
    *,
    top_logprobs: int,
    max_tokens: int,
) -> dict[str, Any]:
    """Generate a high-entropy continuation probe without a static answer key."""
    if (
        not 1 <= len(reference_worker_ids) <= 2
        or len(set(reference_worker_ids)) != len(reference_worker_ids)
    ):
        raise FidelityContractError("text fidelity requires one or two distinct references")
    if not 2 <= top_logprobs <= MAX_DISTRIBUTION_TOKENS:
        raise FidelityContractError("top_logprobs is outside the supported range")
    if not 1 <= max_tokens <= 32:
        raise FidelityContractError("max_tokens is outside the supported range")
    nonce = secrets.token_hex(8).upper()
    prompt = (
        f"Evaluation note {nonce}. Complete the final sentence naturally. Reply with only "
        "one ordinary English word and no punctuation.\n\n"
        f"{secrets.choice(_SETTINGS).capitalize()}, {secrets.choice(_SUBJECTS)} placed "
        f"the {secrets.choice(_OBJECTS)} on the table. The outcome felt "
        f"{secrets.choice(_QUALITIES)}, yet the final written assessment called it"
    )
    return {
        "schema": SCHEMA,
        "kind": KIND,
        "prompt": prompt,
        "reference_worker_ids": list(reference_worker_ids),
        "scoring_policy_id": POLICY_ID,
        "request": {
            "max_tokens": max_tokens,
            "temperature": 0,
            "top_p": 1,
            "seed": secrets.randbelow(2**31),
            "reasoning_effort": "medium",
            "logprobs": True,
            "top_logprobs": top_logprobs,
        },
        "comparison": {
            "metric": "jensen_shannon_nats.v1",
            "reference_agreement_max": REFERENCE_AGREEMENT_MAX,
            "candidate_match_max": CANDIDATE_MATCH_MAX,
            "candidate_anomaly_min": CANDIDATE_ANOMALY_MIN,
            "negative_requires_references": 2,
        },
    }


def _finite_logprob(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    if not math.isfinite(result) or result > 0 or result < -1_000_000:
        return None
    return result


def first_distribution(raw: Any) -> list[dict[str, Any]]:
    """Reduce OpenAI logprobs to one bounded first-token distribution."""
    if not isinstance(raw, dict):
        return []
    content = raw.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        return []
    first = content[0]
    candidates: list[dict[str, Any]] = []
    selected_token = first.get("token")
    selected_logprob = _finite_logprob(first.get("logprob"))
    if (
        isinstance(selected_token, str)
        and len(selected_token.encode("utf-8")) <= 128
        and selected_logprob is not None
    ):
        candidates.append({"token": selected_token, "logprob": selected_logprob})
    top = first.get("top_logprobs")
    if isinstance(top, list):
        for item in top[:MAX_DISTRIBUTION_TOKENS]:
            if not isinstance(item, dict):
                continue
            token = item.get("token")
            logprob = _finite_logprob(item.get("logprob"))
            if (
                isinstance(token, str)
                and logprob is not None
                and len(token.encode("utf-8")) <= 128
            ):
                candidates.append({"token": token, "logprob": logprob})
    deduped: dict[str, float] = {}
    for item in candidates:
        token = item["token"]
        deduped[token] = max(deduped.get(token, -1_000_000.0), item["logprob"])
    return [
        {"token": token, "logprob": value}
        for token, value in sorted(
            deduped.items(), key=lambda item: (-item[1], item[0])
        )[:MAX_DISTRIBUTION_TOKENS]
    ]


def validate_challenge(challenge: Any) -> dict[str, Any]:
    if not isinstance(challenge, dict):
        raise FidelityContractError("text fidelity challenge is malformed")
    refs = challenge.get("reference_worker_ids")
    request = challenge.get("request")
    comparison = challenge.get("comparison")
    expected_challenge_keys = {
        "schema",
        "kind",
        "prompt",
        "reference_worker_ids",
        "scoring_policy_id",
        "request",
        "comparison",
    }
    expected_request_keys = {
        "max_tokens",
        "temperature",
        "top_p",
        "seed",
        "reasoning_effort",
        "logprobs",
        "top_logprobs",
    }
    valid_references = isinstance(refs, list) and all(
        isinstance(value, str) and 0 < len(value) <= 64 for value in refs
    )
    if (
        set(challenge) != expected_challenge_keys
        or challenge.get("schema") != SCHEMA
        or challenge.get("kind") != KIND
        or challenge.get("scoring_policy_id") != POLICY_ID
        or not isinstance(challenge.get("prompt"), str)
        or not challenge["prompt"]
        or len(challenge["prompt"].encode("utf-8")) > MAX_PROMPT_BYTES
        or not isinstance(refs, list)
        or not 1 <= len(refs) <= 2
        or len(set(refs)) != len(refs)
        or not valid_references
        or not isinstance(request, dict)
        or set(request) != expected_request_keys
        or isinstance(request.get("temperature"), bool)
        or request.get("temperature") != 0
        or isinstance(request.get("top_p"), bool)
        or request.get("top_p") != 1
        or request.get("logprobs") is not True
        or not isinstance(request.get("seed"), int)
        or isinstance(request.get("seed"), bool)
        or not 0 <= request["seed"] < 2**31
        or request.get("reasoning_effort") != "medium"
        or isinstance(request.get("max_tokens"), bool)
        or not isinstance(request.get("max_tokens"), int)
        or not 1 <= request["max_tokens"] <= 32
        or isinstance(request.get("top_logprobs"), bool)
        or not isinstance(request.get("top_logprobs"), int)
        or not 2 <= request["top_logprobs"] <= MAX_DISTRIBUTION_TOKENS
        or comparison
        != {
            "metric": "jensen_shannon_nats.v1",
            "reference_agreement_max": REFERENCE_AGREEMENT_MAX,
            "candidate_match_max": CANDIDATE_MATCH_MAX,
            "candidate_anomaly_min": CANDIDATE_ANOMALY_MIN,
            "negative_requires_references": 2,
        }
    ):
        raise FidelityContractError("text fidelity challenge is malformed")
    return challenge


def validate_witnesses(
    challenge: dict[str, Any],
    target_worker_id: str,
    raw_witnesses: Any,
) -> list[dict[str, Any]]:
    challenge = validate_challenge(challenge)
    references = challenge["reference_worker_ids"]
    expected = [
        ("candidate", target_worker_id),
        *(("reference", value) for value in references),
    ]
    if not isinstance(raw_witnesses, list) or len(raw_witnesses) != len(expected):
        raise FidelityContractError("text fidelity witness set is malformed")
    normalized: list[dict[str, Any]] = []
    for raw, (role, worker_id) in zip(raw_witnesses, expected, strict=True):
        if not isinstance(raw, dict):
            raise FidelityContractError("text fidelity witness set is malformed")
        output_hash = str(raw.get("output_hash") or "").lower()
        finish_reason = str(raw.get("finish_reason") or "")
        distribution = raw.get("distribution")
        latency_ms = raw.get("latency_ms")
        if (
            raw.get("role") != role
            or raw.get("worker_id") != worker_id
            or not re.fullmatch(r"[0-9a-f]{64}", output_hash)
            or len(finish_reason) > 32
            or not isinstance(latency_ms, int)
            or isinstance(latency_ms, bool)
            or not 0 <= latency_ms <= 3_600_000
            or not isinstance(distribution, list)
            or not distribution
            or len(distribution) > MAX_DISTRIBUTION_TOKENS
        ):
            raise FidelityContractError("text fidelity witness set is malformed")
        clean_distribution: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in distribution:
            if not isinstance(item, dict):
                raise FidelityContractError("text fidelity distribution is malformed")
            token = item.get("token")
            logprob = _finite_logprob(item.get("logprob"))
            if (
                not isinstance(token, str)
                or not token
                or len(token.encode("utf-8")) > 128
                or token in seen
                or logprob is None
            ):
                raise FidelityContractError("text fidelity distribution is malformed")
            seen.add(token)
            clean_distribution.append({"token": token, "logprob": logprob})
        normalized.append(
            {
                "role": role,
                "worker_id": worker_id,
                "output_hash": output_hash,
                "finish_reason": finish_reason,
                "distribution": clean_distribution,
                "latency_ms": latency_ms,
            }
        )
    return normalized


def response_commitment(witnesses: list[dict[str, Any]]) -> str:
    return canonical({"witnesses": witnesses})

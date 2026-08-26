# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Executable red-team contract for recognizable validator probes."""

import hashlib

import pytest

from grid_api.routers.tests.validator_adversaries import (
    ProbeAwareModelSwitchWorker,
    PublicProbeClassifier,
    RegexTemplateWorker,
    ReplayCacheWorker,
    WorkerReply,
)
from grid_api.services import validators as validators_svc


def _request(challenge: dict, *, second_stage: WorkerReply | None = None) -> dict:
    request = {
        "model": "adversarial-test-model",
        "messages": [{"role": "user", "content": challenge["prompt"]}],
        "max_tokens": challenge["max_tokens"],
        "temperature": challenge["temperature"],
        "stream": True,
    }
    if challenge["kind"] == "tool.chain":
        step = challenge["steps"][0] if second_stage is None else challenge["steps"][1]
        request.update({"tools": step["tools"], "tool_choice": step["tool_choice"]})
        if second_stage is not None:
            request["messages"].extend(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": second_stage.tool_calls,
                    },
                    {
                        "role": "tool",
                        "tool_call_id": second_stage.tool_calls[0]["id"],
                        "content": validators_svc._canonical(step["tool_result"]),
                    },
                ]
            )
    else:
        for key in ("tools", "tool_choice", "stop"):
            if key in challenge:
                request[key] = challenge[key]
    return request


def _score(challenge: dict, replies: list[WorkerReply]) -> str:
    final = replies[-1]
    kwargs = {
        "tool_calls": final.tool_calls,
        "reasoning_text": final.reasoning_text,
        "finish_reason": final.finish_reason,
    }
    if challenge["kind"] == "tool.chain":
        kwargs["tool_chain"] = [
            {
                "text": reply.text,
                "tool_calls": reply.tool_calls,
                "finish_reason": reply.finish_reason,
            }
            for reply in replies
        ]
    return validators_svc._score_text_challenge(
        challenge,
        final.text,
        10,
        **kwargs,
    )


def _run(worker, challenge: dict) -> tuple[str, list[dict]]:
    requests = [_request(challenge)]
    replies = [worker.respond(requests[0])]
    if challenge["kind"] == "tool.chain":
        requests.append(_request(challenge, second_stage=replies[0]))
        replies.append(worker.respond(requests[1]))
    return _score(challenge, replies), requests


@pytest.mark.parametrize("selector", validators_svc._TEXT_CHALLENGE_KINDS)
def test_regex_worker_can_pass_public_templates_but_never_quality(selector):
    challenge = validators_svc._make_text_challenge(selector)
    verdict, requests = _run(RegexTemplateWorker(), challenge)

    assert verdict == "healthy"
    assert all(PublicProbeClassifier().predicts_probe(request) for request in requests)
    assert validators_svc._score_dimension("text", challenge["capability"]) in {
        "protocol_conformance",
        "capability",
    }
    assert validators_svc._quality_eligible("text", challenge["capability"]) is False


def test_fresh_random_values_defeat_exact_replay_but_not_template_solving():
    first = validators_svc._make_text_challenge("echo")
    second = validators_svc._make_text_challenge("echo")
    assert first["prompt"] != second["prompt"]

    solver = RegexTemplateWorker()
    first_request = _request(first)
    cache = ReplayCacheWorker()
    cache.prime(first_request, solver.respond(first_request))

    assert _score(first, [cache.respond(first_request)]) == "healthy"
    assert _score(second, [cache.respond(_request(second))]) == "failed"
    assert _run(solver, second)[0] == "healthy"


def test_public_probe_classifier_exposes_current_model_switching_attack():
    classifier = PublicProbeClassifier()
    switcher = ProbeAwareModelSwitchWorker(
        classifier,
        RegexTemplateWorker(),
        lambda _request: WorkerReply(text="I am not sure."),
    )
    public_probe = validators_svc._make_text_challenge("math")
    public_verdict, _ = _run(switcher, public_probe)

    blind_prompt = (
        "A neighborhood library had 19 atlases and received 23 more. "
        "How many atlases does it have now?"
    )
    blind_request = {
        "model": "adversarial-test-model",
        "messages": [{"role": "user", "content": blind_prompt}],
        "max_tokens": 256,
        "temperature": 0,
        "stream": True,
    }
    blind_challenge = {
        "kind": "math.add",
        "prompt": blind_prompt,
        "expected_hash": hashlib.sha256(b"42").hexdigest(),
        "max_tokens": 256,
        "temperature": 0,
        "capability": "text.reasoning.v1",
    }
    blind_reply = switcher.respond(blind_request)

    assert public_verdict == "healthy"
    assert _score(blind_challenge, [blind_reply]) == "failed"
    assert switcher.strong_routes == 1
    assert switcher.weak_routes == 1
    assert validators_svc._quality_eligible("text", public_probe["capability"]) is False


def test_known_classifier_has_high_recall_and_zero_fixture_false_positives():
    classifier = PublicProbeClassifier()
    public_requests = [
        _request(validators_svc._make_text_challenge(selector))
        for selector in validators_svc._TEXT_CHALLENGE_KINDS
    ]
    ordinary_prompts = (
        "Summarize the tradeoffs between SQLite and PostgreSQL for a small API.",
        "Draft a friendly reply confirming Tuesday's meeting time.",
        "Why might a websocket reconnect without receiving a close frame?",
        "Write a short folk-rock chorus in A minor about rebuilding a town.",
        "Explain the difference between gross margin and operating margin.",
        "Suggest three names for a science-fiction strategy game.",
    )
    ordinary_requests = [
        {
            "model": "adversarial-test-model",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 256,
            "temperature": 0.7,
            "stream": True,
        }
        for prompt in ordinary_prompts
    ]

    recall = sum(classifier.predicts_probe(item) for item in public_requests) / len(
        public_requests
    )
    false_positive_rate = sum(
        classifier.predicts_probe(item) for item in ordinary_requests
    ) / len(ordinary_requests)

    # This is a red baseline, not a success claim. If it stops reproducing,
    # update the hostile actor before weakening the economic gate.
    assert recall >= 0.90
    assert false_positive_rate == 0

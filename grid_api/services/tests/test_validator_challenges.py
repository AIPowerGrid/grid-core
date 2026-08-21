# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib
import json

import pytest

from grid_api.services import validators


@pytest.mark.parametrize(
    ("family", "kind", "capability"),
    [
        ("echo", "echo", "text.instruction.v1"),
        ("math", None, "text.reasoning.v1"),
        ("json.object", "json.object", "text.structured.v1"),
        ("context.retrieve", "context.retrieve", "text.context.4k.v1"),
        ("logic.steps", "logic.steps", "text.reasoning.multistep.v1"),
        ("tool.call", "tool.call", "text.tool_call.v1"),
        ("tool.chain", "tool.chain", "text.tool_chain.v1"),
        ("stop.sequence", "stop.sequence", "text.stop_sequence.v1"),
    ],
)
def test_generated_challenge_families_hide_expected_answer(family, kind, capability):
    challenge = validators._make_text_challenge(family)

    assert challenge["kind"] == kind or challenge["kind"].startswith("math.")
    assert challenge["capability"] == capability
    assert "expected" not in challenge
    assert len(challenge["prompt"]) > 20
    assert len(challenge["expected_hash"]) == 64
    int(challenge["expected_hash"], 16)


def test_tool_call_challenge_is_dynamic_and_carries_a_strict_schema():
    challenge = validators._make_text_challenge("tool.call")
    function = challenge["tools"][0]["function"]

    assert function["name"] in challenge["prompt"]
    assert function["strict"] is True
    assert function["parameters"]["additionalProperties"] is False
    assert challenge["tool_choice"]["function"]["name"] == function["name"]


def test_tool_call_scoring_requires_one_exact_call_and_no_text():
    expected = json.dumps(
        {"arguments": {"count_a": 7, "token_b": "A1"}, "name": "record_c"},
        sort_keys=True, separators=(",", ":"),
    )
    challenge = _challenge("tool.call", expected)
    correct = [{
        "id": "call_opaque",
        "type": "function",
        "function": {"name": "record_c", "arguments": '{"token_b":"A1","count_a":7}'},
    }]

    assert validators._score_text_challenge(challenge, "", 10, tool_calls=correct) == "healthy"
    assert validators._score_text_challenge(
        challenge, "I called it.", 10, tool_calls=correct
    ) == "failed"
    assert validators._score_text_challenge(
        challenge, "", 10, tool_calls=correct + correct
    ) == "failed"
    wrong = [{
        **correct[0],
        "function": {"name": "record_c", "arguments": '{"count_a":8,"token_b":"A1"}'},
    }]
    assert validators._score_text_challenge(challenge, "", 10, tool_calls=wrong) == "failed"


def test_tool_chain_challenge_requires_two_exact_attributed_calls():
    challenge = validators._make_text_challenge("tool.chain")
    first_tool = challenge["steps"][0]["tools"][0]["function"]
    second_tool = challenge["steps"][1]["tools"][0]["function"]
    lookup_field = next(iter(first_tool["parameters"]["properties"]))
    lookup_value = challenge["prompt"].split(f"{lookup_field!r} set to '", 1)[1].split("'", 1)[0]
    result = challenge["steps"][1]["tool_result"]
    total_field, token_field = second_tool["parameters"]["properties"]
    chain = [
        {
            "text": "",
            "tool_calls": [{
                "id": "call_lookup",
                "type": "function",
                "function": {
                    "name": first_tool["name"],
                    "arguments": json.dumps({lookup_field: lookup_value}),
                },
            }],
        },
        {
            "text": "",
            "tool_calls": [{
                "id": "call_submit",
                "type": "function",
                "function": {
                    "name": second_tool["name"],
                    "arguments": json.dumps({
                        total_field: result["left"] + result["right"],
                        token_field: result["token"],
                    }),
                },
            }],
        },
    ]

    assert validators._score_text_challenge(
        challenge, "", 10, tool_chain=chain
    ) == "healthy"
    assert validators._score_text_challenge(
        challenge, "", 10, tool_chain=chain[:1]
    ) == "failed"
    wrong = json.loads(json.dumps(chain))
    wrong[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({
        total_field: result["left"] + result["right"] + 1,
        token_field: result["token"],
    })
    assert validators._score_text_challenge(
        challenge, "", 10, tool_chain=wrong
    ) == "failed"


def _challenge(kind: str, expected: str) -> dict:
    return {
        "kind": kind,
        "expected_hash": hashlib.sha256(expected.encode()).hexdigest(),
    }


def test_json_scoring_is_semantic_but_strict_about_json_only():
    expected = json.dumps({"alpha": "A1", "count": 7}, sort_keys=True, separators=(",", ":"))
    challenge = _challenge("json.object", expected)

    assert validators._score_text_challenge(challenge, '{"count": 7, "alpha": "A1"}', 10) == "healthy"
    assert validators._score_text_challenge(challenge, f"```json\n{expected}\n```", 10) == "failed"
    assert validators._score_text_challenge(challenge, '[{"alpha":"A1","count":7}]', 10) == "failed"


def test_context_scoring_requires_only_the_exact_retrieved_token():
    challenge = _challenge("context.retrieve", "A1B2C3D4")

    assert validators._score_text_challenge(challenge, "`A1B2C3D4`", 10) == "healthy"
    assert validators._score_text_challenge(challenge, "The value is A1B2C3D4", 10) == "failed"


def test_multistep_scoring_rejects_ambiguous_numeric_answers():
    challenge = _challenge("logic.steps", "42")

    assert validators._score_text_challenge(challenge, "The final result is 42.", 10) == "healthy"
    assert validators._score_text_challenge(challenge, "41 or 42", 10) == "failed"
    assert validators._score_text_challenge(challenge, "420", 10) == "failed"


def test_correct_answer_over_latency_budget_is_slow(monkeypatch):
    monkeypatch.setattr(validators, "PROBE_LATENCY_BUDGET_SECONDS", 1)
    challenge = _challenge("echo", "ABCDEF12")

    assert validators._score_text_challenge(challenge, "ABCDEF12", 1_001) == "slow"


def test_unknown_or_malformed_commitment_fails_closed():
    assert validators._score_text_challenge({"kind": "unknown", "expected_hash": "a" * 64}, "x", 1) == "failed"
    assert validators._score_text_challenge({"kind": "echo", "expected_hash": "bad"}, "x", 1) == "failed"


def test_legacy_validator_capability_is_limited_to_basic_families():
    kinds, capabilities = validators._supported_text_challenges(["text.basic.v1"])

    assert kinds == ("echo", "math")
    assert capabilities == {
        "text.basic.v1",
        "text.instruction.v1",
        "text.reasoning.v1",
    }


def test_richer_validator_capabilities_select_only_advertised_families():
    kinds, capabilities = validators._supported_text_challenges(
        ["text.structured.v1", "text.context.4k.v1"]
    )

    assert kinds == ("json.object", "context.retrieve")
    assert capabilities == {"text.structured.v1", "text.context.4k.v1"}


def test_tool_call_family_requires_its_exact_scorer_capability():
    kinds, capabilities = validators._supported_text_challenges(["text.tool_call.v1"])

    assert kinds == ("tool.call",)
    assert capabilities == {"text.tool_call.v1"}


def test_tool_chain_family_requires_its_exact_scorer_capability():
    kinds, capabilities = validators._supported_text_challenges(["text.tool_chain.v1"])

    assert kinds == ("tool.chain",)
    assert capabilities == {"text.tool_chain.v1"}


def test_stop_sequence_challenge_commits_only_the_pre_stop_output():
    challenge = validators._make_text_challenge("stop.sequence")
    prefix, remainder = challenge["prompt"].rsplit(": ", 1)[1].split(challenge["stop"])

    assert validators._score_text_challenge(challenge, prefix, 10) == "healthy"
    assert validators._score_text_challenge(challenge, prefix + remainder, 10) == "failed"
    assert challenge["stop"] not in prefix


def test_modern_basic_scorers_can_finish_legacy_basic_groups():
    kinds, capabilities = validators._supported_text_challenges(
        ["text.instruction.v1", "text.reasoning.v1"]
    )

    assert kinds == ("echo", "math")
    assert capabilities == {
        "text.basic.v1",
        "text.instruction.v1",
        "text.reasoning.v1",
    }

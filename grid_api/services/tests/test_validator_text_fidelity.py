# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from grid_api.services import validator_text_fidelity as fidelity
from grid_api.services import validators


@pytest.mark.parametrize("refs", [[{}], [[]], ["ref-1", {}]])
def test_unhashable_reference_ids_are_contract_errors(refs):
    challenge = fidelity.make_challenge(["ref-1"], top_logprobs=5, max_tokens=4)
    challenge["reference_worker_ids"] = refs
    with pytest.raises(fidelity.FidelityContractError):
        fidelity.validate_challenge(challenge)


def test_challenge_is_dynamic_and_bounded():
    first = fidelity.make_challenge(["ref-1", "ref-2"], top_logprobs=10, max_tokens=8)
    second = fidelity.make_challenge(["ref-1", "ref-2"], top_logprobs=10, max_tokens=8)

    assert fidelity.validate_challenge(first) == first
    assert first["prompt"] != second["prompt"]
    assert first["request"]["temperature"] == 0
    assert first["request"]["logprobs"] is True
    assert first["request"]["seed"] != second["request"]["seed"] or first["prompt"] != second["prompt"]


def test_challenge_rejects_request_key_injection_and_oversized_prompt():
    challenge = fidelity.make_challenge(["ref-1"], top_logprobs=5, max_tokens=4)
    challenge["request"]["model"] = "attacker-selected-model"
    with pytest.raises(fidelity.FidelityContractError):
        fidelity.validate_challenge(challenge)

    challenge = fidelity.make_challenge(["ref-1"], top_logprobs=5, max_tokens=4)
    challenge["prompt"] = "x" * (fidelity.MAX_PROMPT_BYTES + 1)
    with pytest.raises(fidelity.FidelityContractError):
        fidelity.validate_challenge(challenge)


def test_first_distribution_deduplicates_and_bounds_untrusted_logprobs():
    raw = {
        "content": [
            {
                "token": " alpha",
                "logprob": -0.1,
                "top_logprobs": [
                    {"token": " alpha", "logprob": -0.2},
                    {"token": " beta", "logprob": -1.2},
                    {"token": "bad", "logprob": 1.0},
                ],
            }
        ],
    }

    assert fidelity.first_distribution(raw) == [
        {"token": " alpha", "logprob": -0.1},
        {"token": " beta", "logprob": -1.2},
    ]

    raw["content"][0]["token"] = "x" * 129
    assert all(
        len(item["token"].encode("utf-8")) <= 128
        for item in fidelity.first_distribution(raw)
    )


def test_witnesses_are_bound_to_candidate_and_reference_order():
    challenge = fidelity.make_challenge(["ref-1"], top_logprobs=5, max_tokens=4)
    witnesses = [
        {
            "role": "candidate",
            "worker_id": "candidate",
            "output_hash": "a" * 64,
            "finish_reason": "stop",
            "distribution": [{"token": " yes", "logprob": -0.1}],
            "latency_ms": 20,
        },
        {
            "role": "reference",
            "worker_id": "ref-1",
            "output_hash": "b" * 64,
            "finish_reason": "stop",
            "distribution": [{"token": " yes", "logprob": -0.2}],
            "latency_ms": 30,
        },
    ]

    assert fidelity.validate_witnesses(challenge, "candidate", witnesses) == witnesses
    witnesses[1]["worker_id"] = "other"
    with pytest.raises(fidelity.FidelityContractError):
        fidelity.validate_witnesses(challenge, "candidate", witnesses)


@pytest.mark.parametrize(
    "bad",
    [None, {}, {"content": []}, {"content": [{"token": "x", "logprob": float("nan")}]}, {"content": [{"token": "x", "logprob": 0.1}]}],
)
def test_missing_or_invalid_logprobs_fail_closed(bad):
    assert fidelity.first_distribution(bad) == []


@pytest.mark.asyncio
async def test_targeted_stage_binds_worker_and_reduces_logprobs(monkeypatch):
    challenge = fidelity.make_challenge(["ref-1"], top_logprobs=5, max_tokens=4)
    settings = validators.get_settings()
    monkeypatch.setattr(settings, "validator_text_fidelity_probe_timeout_seconds", 333)
    row = {
        "model": "model-a",
        "grid_nonce": "nonce-1",
        "probe_group_id": "prg-1",
    }

    seen = {}

    async def completed(**kwargs):
        seen.update(kwargs)
        return {
            "status": "completed",
            "full_text": "natural",
            "finish_reason": "stop",
            "grid": {
                "worker_id": "candidate",
                "assignment_id": "asg-1",
                "grid_nonce": "nonce-1",
            },
            "logprobs": {
                "content": [
                    {
                        "token": " natural",
                        "logprob": -0.1,
                        "top_logprobs": [{"token": " natural", "logprob": -0.1}],
                    }
                ],
            },
        }

    monkeypatch.setattr(validators, "_run_targeted_text_stage", completed)
    result = await validators._run_targeted_text_fidelity_stage(
        row=row,
        assignment_id="asg-1",
        job_id="job-1",
        role="candidate",
        worker_id="candidate",
        worker_name="candidate-name",
        challenge=challenge,
    )

    assert result["status"] == "completed"
    assert result["witness"]["worker_id"] == "candidate"
    assert result["witness"]["distribution"] == [{"token": " natural", "logprob": -0.1}]
    assert seen["timeout_seconds"] == 333


@pytest.mark.asyncio
async def test_targeted_stage_missing_logprobs_is_inconclusive(monkeypatch):
    challenge = fidelity.make_challenge(["ref-1"], top_logprobs=5, max_tokens=4)

    async def completed(**_kwargs):
        return {
            "status": "completed",
            "full_text": "natural",
            "finish_reason": "stop",
            "grid": {
                "worker_id": "candidate",
                "assignment_id": "asg-1",
                "grid_nonce": "nonce-1",
            },
            "logprobs": None,
        }

    monkeypatch.setattr(validators, "_run_targeted_text_stage", completed)
    result = await validators._run_targeted_text_fidelity_stage(
        row={"model": "model-a", "grid_nonce": "nonce-1", "probe_group_id": "prg-1"},
        assignment_id="asg-1",
        job_id="job-1",
        role="candidate",
        worker_id="candidate",
        worker_name="candidate-name",
        challenge=challenge,
    )

    assert result["status"] == "error"
    assert result["code"] == 422

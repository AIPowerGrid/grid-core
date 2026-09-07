# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import ast
import inspect
import json
from unittest.mock import AsyncMock

import pytest

from grid_api.routers import worker_ws
from grid_api.services import validator_responses, validator_text_fidelity, validators


def delta(sequence=1, **changes):
    return {
        "type": "response.output_text.delta", "sequence_number": sequence,
        "item_id": "msg-test", "output_index": 0, "content_index": 0,
        "delta": "Blue", "logprobs": [{
            "token": "Blue", "logprob": -0.25, "bytes": [66, 108, 117, 101],
            "top_logprobs": [{"token": "Green", "logprob": -2.5}],
        }], **changes,
    }


def job():
    return {
        "job_id": "job-test", "job_type": "text", "hard_target_worker": "rig-test",
        "payload": {
            "api_format": "openai-responses", "request": {"input": "Name a color", "stream": True},
            "_validator_probe": True, "_validator_assignment_id": "asg-test",
            "_validator_probe_group_id": "prg-test", "_validator_grid_nonce": "nonce-test",
        },
    }


def socket(*events, final=True):
    frames = [{"type": "raw", "id": "job-test", "data": json.dumps(event)} for event in events]
    if final:
        frames += [{"type": "done", "id": "job-test"}]
    return type("Socket", (), {
        "send_json": AsyncMock(), "close": AsyncMock(),
        "receive_json": AsyncMock(side_effect=frames),
    })()


@pytest.fixture
def inert(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("probe touched ordinary inference/economics/health")

    for name in ["record_and_settle", "release_job"]:
        monkeypatch.setattr(worker_ws.credits, name, forbidden)
    for name in ["record_job_complete", "record_job_failed", "calculate_den", "_clear_strikes", "_handle_raw_passthrough"]:
        monkeypatch.setattr(worker_ws, name, forbidden)
    monkeypatch.setattr(worker_ws.ledger_svc, "record_completion", forbidden)
    monkeypatch.setattr(worker_ws.route_events, "capture_route", forbidden)
    monkeypatch.setattr(worker_ws.route_events, "capture_outcome", forbidden)
    published = AsyncMock()
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", published)
    monkeypatch.setattr(worker_ws.token_stream, "publish_error", AsyncMock())
    return published


@pytest.mark.asyncio
async def test_responses_probe_preserves_native_values_and_core_binding(inert):
    source = delta()
    ws = socket(source, {"type": "response.completed"})
    assert await worker_ws._maybe_handle_validator_text_probe(ws, job(), "model-a", "worker-a", {})
    sent = ws.send_json.call_args_list[0].args[0]
    assert not any(key.startswith("_validator_") for key in sent["payload"])
    assert sent["payload"]["api_format"] == "openai-responses"
    result = inert.call_args.kwargs
    assert result["logprobs"]["events"][0]["positions"] == source["logprobs"]
    assert result["logprobs"]["status"] == "available"
    assert result["logprobs"]["comparison_ready"] is False
    assert validator_text_fidelity.first_distribution(result["logprobs"]) == []
    assert result["grid"] == {
        "worker_id": "worker-a", "assignment_id": "asg-test", "grid_nonce": "nonce-test",
        "economic_effect": "none", "observation_status": "available",
    }
    assert ws.send_json.call_args.args[0] == {"type": "ack", "id": "job-test", "den": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("probabilities", [None, [], {}, [{"token": "x", "logprob": 1}]])
async def test_missing_or_malformed_not_worker_failure(inert, probabilities):
    ws = socket(delta(logprobs=probabilities), {"type": "response.completed"})
    await worker_ws._maybe_handle_validator_text_probe(ws, job(), "model-a", "worker-a", {})
    result = inert.call_args.kwargs
    assert result["logprobs"]["status"] == "unavailable"
    assert "probe_failed" not in result["grid"]
    assert result["logprobs"]["quality_eligible"] is False


@pytest.mark.asyncio
async def test_missing_first_logprob_is_recorded_as_gap(inert):
    ws = socket(delta(logprobs=[]), delta(2), {"type": "response.completed"})
    await worker_ws._maybe_handle_validator_text_probe(ws, job(), "model-a", "worker-a", {})
    result = inert.call_args.kwargs["logprobs"]
    assert result["status"] == "partial"
    assert result["events"][0]["positions"] == []
    assert len(result["events"][1]["positions"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [None, "response.incomplete", "response.failed", "error"])
async def test_unfinished_backend_is_not_successful_probability_evidence(inert, terminal):
    events = [delta()] + ([{"type": terminal}] if terminal else [])
    await worker_ws._maybe_handle_validator_text_probe(socket(*events), job(), "model-a", "worker-a", {})
    assert inert.call_args.kwargs["logprobs"]["status"] == "unavailable"
    assert inert.call_args.kwargs["finish_reason"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["size", "wrong_job", "timeout", "frames"])
async def test_bad_stream_closes_socket_without_economics(inert, monkeypatch, failure):
    ws = socket()
    if failure == "timeout":
        ws.receive_json.side_effect = TimeoutError
    elif failure == "wrong_job":
        ws.receive_json.side_effect = [{"type": "done", "id": "another-job"}]
    elif failure == "frames":
        monkeypatch.setattr(validator_responses, "MAX_STREAM_EVENTS", 2)
        ws = socket({"type": "response.created"}, {"type": "response.created"}, final=False)
    else:
        ws.receive_json.side_effect = [{"type": "raw", "id": "job-test", "data": "x" * 65_537}]
    assert await worker_ws._maybe_handle_validator_text_probe(ws, job(), "model-a", "worker-a", {})
    assert inert.call_args.kwargs["logprobs"]["status"] == "unavailable"
    ws.close.assert_awaited_once()
    assert ws.send_json.call_args.args[0] == {"type": "cancel", "id": "job-test"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["nonce", "hard_target", "probe_flag", "format"])
async def test_invalid_probe_cannot_fall_through_to_paid_handler(inert, change):
    value = job()
    if change == "nonce":
        value["payload"].pop("_validator_grid_nonce")
    elif change == "hard_target":
        value.pop("hard_target_worker")
    elif change == "probe_flag":
        value["payload"]["_validator_probe"] = False
    else:
        value["payload"]["api_format"] = "anthropic"
    ws = socket()
    assert await worker_ws._maybe_handle_validator_text_probe(ws, value, "model-a", "worker-a", {})
    ws.send_json.assert_not_awaited()
    worker_ws.token_stream.publish_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_responses_job_is_not_intercepted(inert):
    value = job()
    value["payload"] = {"api_format": "openai-responses", "request": {}}
    ws = socket()
    assert await worker_ws._maybe_handle_validator_text_probe(ws, value, "model-a", "worker-a", {}) is None
    ws.send_json.assert_not_awaited()
    inert.assert_not_awaited()


def test_probe_dispatch_precedes_paid_passthrough_in_websocket_loop():
    tree = ast.parse(inspect.getsource(worker_ws))
    calls = {
        name: [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Name) and node.func.id == name]
        for name in ["_maybe_handle_validator_text_probe", "_handle_raw_passthrough"]
    }
    assert len(calls["_maybe_handle_validator_text_probe"]) == len(calls["_handle_raw_passthrough"]) == 1
    assert calls["_maybe_handle_validator_text_probe"][0] < calls["_handle_raw_passthrough"][0]


@pytest.mark.asyncio
async def test_targeted_stage_dispatches_responses_without_chat_conversion(inert, monkeypatch):
    submit = AsyncMock()
    monkeypatch.setattr(worker_ws.job_queue, "submit_job", submit)
    witness = {"schema": "responses-logprobs-observation.v1", "events": [delta()]}

    async def subscribe(*_args, **_kwargs):
        yield {"text": "[DONE]", "logprobs": witness, "grid": {
            "assignment_id": "asg-test", "worker_id": "worker-test", "grid_nonce": "nonce-test", "economic_effect": "none",
        }}

    monkeypatch.setattr(worker_ws.token_stream, "subscribe_tokens", subscribe)
    request = {"input": "Name a color", "max_output_tokens": 128, "stream": True,
               "include": ["message.output_text.logprobs"], "top_logprobs": 5}
    result = await validators._run_targeted_text_stage(
        row={"model": "model-a", "target_worker_id": "worker-test", "target_worker_name": "rig-test",
             "probe_group_id": "prg-test", "grid_nonce": "nonce-test"},
        assignment_id="asg-test", job_id="job-test", prompt="Name a color", request=request,
        api_format="openai-responses", timeout_seconds=60,
    )
    payload = submit.call_args.args[1]
    assert payload["api_format"] == "openai-responses"
    assert payload["request"] == request
    assert payload["max_length"] == 128
    assert payload["_validator_timeout_seconds"] == 60
    assert submit.call_args.kwargs["hard_target_worker"] == "rig-test"
    assert result["logprobs"] == witness


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["worker_id", "assignment_id", "grid_nonce", "economic_effect"])
async def test_targeted_responses_receiver_rejects_unbound_evidence(inert, monkeypatch, field):
    monkeypatch.setattr(worker_ws.job_queue, "submit_job", AsyncMock())
    binding = {"assignment_id": "asg-test", "worker_id": "worker-test", "grid_nonce": "nonce-test", "economic_effect": "none"}
    binding[field] = "wrong"

    async def subscribe(*_args, **_kwargs):
        yield {"text": "[DONE]", "grid": binding, "logprobs": {"events": []}}

    monkeypatch.setattr(worker_ws.token_stream, "subscribe_tokens", subscribe)
    result = await validators._run_targeted_text_stage(
        row={"model": "model-a", "target_worker_id": "worker-test", "target_worker_name": "rig-test",
             "probe_group_id": "prg-test", "grid_nonce": "nonce-test"},
        assignment_id="asg-test", job_id="job-test", prompt="x",
        request={"input": "x", "max_output_tokens": 128, "stream": True}, api_format="openai-responses",
    )
    assert result["status"] == "error"
    assert result["message"] == "worker witness binding failed"
    assert "logprobs" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stream", "zero", "too_many", "bool", "timeout", "format", "target"])
async def test_invalid_qualification_request_never_dispatches(inert, monkeypatch, change):
    submit = AsyncMock()
    monkeypatch.setattr(worker_ws.job_queue, "submit_job", submit)
    request = {"input": "x", "max_output_tokens": 128, "stream": True}
    row = {"model": "model-a", "target_worker_id": "worker-test", "target_worker_name": "rig-test",
           "probe_group_id": "prg-test", "grid_nonce": "nonce-test"}
    if change == "stream":
        request["stream"] = False
    elif change in {"zero", "too_many", "bool"}:
        request["max_output_tokens"] = {"zero": 0, "too_many": 257, "bool": True}[change]
    elif change == "target":
        row.pop("target_worker_id")
    with pytest.raises(ValueError):
        await validators._run_targeted_text_stage(
            row=row, assignment_id="asg-test", job_id="job-test", prompt="x", request=request,
            api_format="anthropic" if change == "format" else "openai-responses",
            timeout_seconds=301 if change == "timeout" else 60,
        )
    submit.assert_not_awaited()

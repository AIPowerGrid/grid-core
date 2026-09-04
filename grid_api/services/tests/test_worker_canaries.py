# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from grid_api.services import token_stream, worker_canaries


class _Redis:
    def __init__(self, acquired=True, error=None):
        self.acquired = acquired
        self.error = error
        self.calls = []

    async def set(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.acquired


def _install_canary_transport(monkeypatch, event_factory):
    submitted = {}

    async def submit_job(job_id, payload, models, **kwargs):
        submitted.update(
            job_id=job_id,
            payload=payload,
            models=models,
            kwargs=kwargs,
        )
        return "stream-entry"

    async def subscribe_tokens(job_id, timeout):
        assert job_id == submitted["job_id"]
        assert timeout == worker_canaries.CANARY_TIMEOUT_SECONDS
        for event in event_factory(submitted):
            yield event

    monkeypatch.setattr(worker_canaries.job_queue, "submit_job", submit_job)
    monkeypatch.setattr(worker_canaries.token_stream, "subscribe_tokens", subscribe_tokens)
    return submitted


def _done_for(submitted, *, output=None, worker_id="worker-1", economic_effect="none"):
    payload = submitted["payload"]
    expected = payload["prompt"].rsplit(": ", 1)[1]
    return {
        "text": token_stream.DONE_SENTINEL,
        "full_text": expected if output is None else output,
        "grid": {
            "worker_id": worker_id,
            "canary_id": payload["_worker_self_canary_id"],
            "grid_nonce": payload["_worker_self_canary_nonce"],
            "economic_effect": economic_effect,
        },
    }


@pytest.mark.asyncio
async def test_text_canary_hard_targets_exact_worker_and_returns_no_challenge(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(worker_canaries, "get_redis", lambda: redis)
    submitted = _install_canary_transport(
        monkeypatch,
        lambda captured: [_done_for(captured)],
    )

    result = await worker_canaries.run_text_connectivity_canary(
        worker_id="worker-1",
        worker_name="rig-a",
        model="model-a",
    )

    assert result == {
        "schema": "aipg.worker.canary.v1",
        "status": "passed",
        "worker_name": "rig-a",
        "model": "model-a",
        "latency_ms": result["latency_ms"],
        "reason": "exact_output",
        "proof_scope": "hard_targeted_connectivity_and_exact_output",
        "quality_claim": "none",
        "economic_effect": "none",
    }
    assert isinstance(result["latency_ms"], int)
    assert "prompt" not in result
    assert "output" not in result
    assert submitted["models"] == ["model-a"]
    assert submitted["kwargs"] == {
        "job_type": "text",
        "preferred_worker": "rig-a",
        "hard_target_worker": "rig-a",
    }
    assert submitted["payload"]["_worker_self_canary"] is True
    assert submitted["payload"]["api_format"] == "openai-chat"
    assert redis.calls[0][1] == {
        "ex": worker_canaries.CANARY_COOLDOWN_SECONDS,
        "nx": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_factory", "reason"),
    [
        (lambda captured: [_done_for(captured, output="wrong")], "output_mismatch"),
        (
            lambda captured: [
                {"text": "x" * (worker_canaries.CANARY_MAX_OUTPUT_CHARS + 1)},
                _done_for(captured),
            ],
            "output_too_long",
        ),
        (lambda _captured: [{"text": ["not", "text"]}], "malformed_output"),
        (
            lambda captured: [{**_done_for(captured), "full_text": {"not": "text"}}],
            "malformed_output",
        ),
        (lambda captured: [_done_for(captured, worker_id="worker-2")], "binding_mismatch"),
        (lambda captured: [_done_for(captured, economic_effect="paid")], "binding_mismatch"),
        (lambda _captured: [{"text": token_stream.DONE_SENTINEL, "error": "bad"}], "worker_or_transport_error"),
        (lambda _captured: [], "timeout"),
    ],
)
async def test_text_canary_fails_closed(monkeypatch, event_factory, reason):
    monkeypatch.setattr(worker_canaries, "get_redis", lambda: _Redis())
    _install_canary_transport(monkeypatch, event_factory)

    result = await worker_canaries.run_text_connectivity_canary(
        worker_id="worker-1",
        worker_name="rig-a",
        model="model-a",
    )

    assert result["status"] == "failed"
    assert result["reason"] == reason
    assert result["economic_effect"] == "none"


@pytest.mark.asyncio
async def test_text_canary_rejects_repeated_run(monkeypatch):
    monkeypatch.setattr(worker_canaries, "get_redis", lambda: _Redis(acquired=False))

    with pytest.raises(worker_canaries.WorkerCanaryRateLimited):
        await worker_canaries.run_text_connectivity_canary(
            worker_id="worker-1",
            worker_name="rig-a",
            model="model-a",
        )


@pytest.mark.asyncio
async def test_text_canary_reports_gate_and_transport_unavailability(monkeypatch):
    monkeypatch.setattr(
        worker_canaries,
        "get_redis",
        lambda: _Redis(error=RuntimeError("redis down")),
    )
    with pytest.raises(worker_canaries.WorkerCanaryUnavailable, match="gate"):
        await worker_canaries.run_text_connectivity_canary(
            worker_id="worker-1",
            worker_name="rig-a",
            model="model-a",
        )

    monkeypatch.setattr(worker_canaries, "get_redis", lambda: _Redis())

    async def fail_submit(*_args, **_kwargs):
        raise RuntimeError("stream down")

    monkeypatch.setattr(worker_canaries.job_queue, "submit_job", fail_submit)
    with pytest.raises(worker_canaries.WorkerCanaryUnavailable, match="dispatch"):
        await worker_canaries.run_text_connectivity_canary(
            worker_id="worker-1",
            worker_name="rig-a",
            model="model-a",
        )


@pytest.mark.asyncio
async def test_text_canary_reports_observation_unavailability(monkeypatch):
    monkeypatch.setattr(worker_canaries, "get_redis", lambda: _Redis())

    async def submit_job(*_args, **_kwargs):
        return "stream-entry"

    async def fail_observation(*_args, **_kwargs):
        raise RuntimeError("pubsub down")
        yield

    monkeypatch.setattr(worker_canaries.job_queue, "submit_job", submit_job)
    monkeypatch.setattr(worker_canaries.token_stream, "subscribe_tokens", fail_observation)

    with pytest.raises(worker_canaries.WorkerCanaryUnavailable, match="observation"):
        await worker_canaries.run_text_connectivity_canary(
            worker_id="worker-1",
            worker_name="rig-a",
            model="model-a",
        )

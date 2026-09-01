# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from grid_api.config import GridSettings
from grid_api.services import route_commitments, route_events

ROOT = Path(__file__).resolve().parents[3]


class FakeRedis:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.registry_reads = 0
        self.workers = {
            "worker-a": {
                "worker_id": "worker-a",
                "models": ["model-a"],
                "job_types": ["text"],
                "api_formats": ["openai-chat"],
            },
            "worker-b": {
                "worker_id": "worker-b",
                "models": ["model-a"],
                "job_types": ["text"],
                "api_formats": ["openai-chat"],
            },
            "worker-wrong-format": {
                "worker_id": "worker-wrong-format",
                "models": ["model-a"],
                "job_types": ["text"],
                "api_formats": ["anthropic"],
            },
        }

    async def smembers(self, _key):
        self.registry_reads += 1
        return set(self.workers)

    async def mget(self, keys):
        rows = []
        for key in keys:
            worker_id = key.removeprefix("grid:worker:").removesuffix(":status")
            row = self.workers.get(worker_id)
            rows.append(json.dumps(row) if row else None)
        return rows

    async def xadd(self, stream, fields, **_kwargs):
        self.events.append((stream, dict(fields)))
        return f"{len(self.events)}-0"


class OversizedRegistryRedis(FakeRedis):
    async def smembers(self, _key):
        return {f"worker-{index}" for index in range(route_events.MAX_ACTIVE_WORKERS + 1)}


class HangingRegistryRedis(FakeRedis):
    async def smembers(self, _key):
        await route_events.asyncio.Event().wait()


class FailingRegistryRedis(FakeRedis):
    async def smembers(self, _key):
        self.registry_reads += 1
        raise ConnectionError("registry unavailable")


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    route_events._registry_cache = None
    route_events._registry_failure_until = 0.0
    route_events._registry_cache_lock = None
    route_events._registry_cache_loop = None


def _settings(enabled: bool = True):
    return SimpleNamespace(
        validator_shadow_observer_enabled=enabled,
        validator_shadow_route_hmac_secret=SecretStr("s" * 32),
    )


def _job():
    return {
        "job_id": "private-job-id",
        "stream": "grid:jobs:text",
        "stream_id": "123-0",
        "job_type": "text",
        "payload": {
            "prompt": "private customer prompt",
            "api_format": "openai-chat",
            "request": {"tools": [{"type": "function"}]},
        },
    }


@pytest.mark.asyncio
async def test_route_event_is_committed_bounded_and_contains_compatible_candidates(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(route_events, "get_settings", _settings)
    monkeypatch.setattr(route_events, "get_redis", lambda: redis)

    await route_events._emit_route(route_events._route_capture(_job()), "model-a", "worker-a")

    assert len(redis.events) == 1
    stream, event = redis.events[0]
    assert stream == route_events.STREAM_KEY
    assert event["kind"] == "route"
    assert event["candidate_basis"] == route_events.CANDIDATE_BASIS
    assert len(event["route_ref"]) == 64
    assert len(event["job_ref"]) == 64
    assert event["capability"] == "text.tool_call.v1"
    assert event["task_class"]
    candidates = json.loads(event["candidates"])
    assert candidates == [
        {"baseline_rank": 0, "model": "model-a", "worker_id": "worker-a"},
        {"baseline_rank": 1, "model": "model-a", "worker_id": "worker-b"},
    ]
    serialized = json.dumps(event)
    assert "private-job-id" not in serialized
    assert "private customer prompt" not in serialized
    assert "worker-wrong-format" not in serialized


@pytest.mark.asyncio
async def test_retry_delivery_gets_a_distinct_route_commitment(monkeypatch):
    monkeypatch.setattr(route_events, "get_settings", _settings)
    first = route_events._route_ref(_job())
    retry = route_events._route_ref({**_job(), "stream_id": "124-0"})
    assert first != retry
    assert route_events._job_ref(_job()) == route_events._job_ref({**_job(), "stream_id": "124-0"})


def test_commitment_domains_are_stable_and_separate():
    secret = "s" * 32
    job = route_commitments.job_ref("job-1", secret=secret)
    route = route_commitments.route_ref("job-1", "stream", "1-0", secret=secret)
    assert job == route_commitments.job_ref("job-1", secret=secret)
    assert job != route


@pytest.mark.asyncio
async def test_oversized_worker_registry_fails_capture_closed(monkeypatch):
    monkeypatch.setattr(route_events, "get_settings", _settings)
    monkeypatch.setattr(route_events, "get_redis", lambda: OversizedRegistryRedis())

    with pytest.raises(ValueError, match="snapshot exceeds"):
        await route_events._candidate_snapshot(
            job_type="text",
            api_format="openai-chat",
            selected_model="model-a",
            actual_worker_id="worker-a",
        )


@pytest.mark.asyncio
async def test_capture_timeout_is_bounded_and_never_escapes(monkeypatch):
    monkeypatch.setattr(route_events, "get_settings", _settings)
    monkeypatch.setattr(route_events, "get_redis", lambda: HangingRegistryRedis())
    monkeypatch.setattr(route_events, "CAPTURE_TIMEOUT_SECONDS", 0.01)

    await route_events._emit_route(route_events._route_capture(_job()), "model-a", "worker-a")


@pytest.mark.asyncio
async def test_burst_uses_one_single_flight_registry_read(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(route_events, "get_settings", _settings)
    monkeypatch.setattr(route_events, "get_redis", lambda: redis)
    capture = route_events._route_capture(_job())

    await asyncio.gather(*(route_events._emit_route(capture, "model-a", "worker-a") for _ in range(200)))

    assert redis.registry_reads == 1
    assert len(redis.events) == 200


@pytest.mark.asyncio
async def test_registry_cache_refreshes_after_window(monkeypatch):
    redis = FakeRedis()
    clock = iter((10.0, 10.0, 10.0, 13.0, 13.0, 13.0))
    monkeypatch.setattr(route_events, "get_redis", lambda: redis)
    monkeypatch.setattr(route_events, "_monotonic", lambda: next(clock))

    await route_events._worker_registry_snapshot()
    await route_events._worker_registry_snapshot()

    assert redis.registry_reads == 2


@pytest.mark.asyncio
async def test_registry_failure_backoff_prevents_a_burst_stampede(monkeypatch):
    redis = FailingRegistryRedis()
    monkeypatch.setattr(route_events, "get_redis", lambda: redis)

    results = await asyncio.gather(
        *(route_events._worker_registry_snapshot() for _ in range(200)),
        return_exceptions=True,
    )

    assert redis.registry_reads == 1
    assert sum(isinstance(result, ConnectionError) for result in results) == 1
    assert all(isinstance(result, Exception) for result in results)


def test_candidate_encoding_is_deterministic_and_wire_bounded(monkeypatch):
    monkeypatch.setattr(route_events, "MAX_CANDIDATE_BYTES", 150)
    candidates = [{"worker_id": f"worker-{index}", "model": "model-a", "baseline_rank": index} for index in range(10)]

    encoded = route_events._encoded_candidates(candidates)
    decoded = json.loads(encoded)

    assert len(encoded.encode("utf-8")) <= 150
    assert decoded[0] == candidates[0]
    assert decoded == candidates[: len(decoded)]


@pytest.mark.asyncio
async def test_outcome_contains_no_raw_job_identifier(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(route_events, "get_settings", _settings)
    monkeypatch.setattr(route_events, "get_redis", lambda: redis)

    await route_events._emit_outcome(route_events._route_ref(_job()), "worker-a", "succeeded", 1.25)

    event = redis.events[0][1]
    assert event["duration_ms"] == "1250"
    assert event["terminal_status"] == "succeeded"
    assert "private-job-id" not in json.dumps(event)


@pytest.mark.asyncio
async def test_capture_binds_time_before_background_execution(monkeypatch):
    redis = FakeRedis()
    times = iter(("2026-09-01T16:00:00+00:00", "2026-09-01T16:00:01+00:00"))
    monkeypatch.setattr(route_events, "get_settings", _settings)
    monkeypatch.setattr(route_events, "get_redis", lambda: redis)
    monkeypatch.setattr(route_events, "_iso_now", lambda: next(times))

    route_events.capture_route(job=_job(), selected_model="model-a", worker_id="worker-a")
    await route_events.drain()

    assert redis.events[0][1]["observed_at"] == "2026-09-01T16:00:00+00:00"
    assert next(times) == "2026-09-01T16:00:01+00:00"


def test_disabled_capture_creates_no_task(monkeypatch):
    monkeypatch.setattr(route_events, "get_settings", lambda: _settings(False))
    before = set(route_events._pending)
    route_events.capture_route(job=_job(), selected_model="model-a", worker_id="worker-a")
    assert route_events._pending == before


def test_scheduled_capture_envelope_retains_no_customer_payload(monkeypatch):
    monkeypatch.setattr(route_events, "get_settings", _settings)
    capture = route_events._route_capture(_job())
    serialized = json.dumps(capture)
    assert set(capture) == {"route_ref", "job_ref", "job_type", "api_format", "task_class", "capability"}
    assert "private-job-id" not in serialized
    assert "private customer prompt" not in serialized


def test_settings_failure_never_reaches_worker_path(monkeypatch):
    def broken_settings():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(route_events, "get_settings", broken_settings)
    route_events.capture_route(job=_job(), selected_model="model-a", worker_id="worker-a")
    route_events.capture_outcome(job=_job(), worker_id="worker-a", terminal_status="failed")


@pytest.mark.asyncio
async def test_scheduling_failure_never_reaches_the_worker_path(monkeypatch):
    monkeypatch.setattr(route_events, "get_settings", _settings)

    class BrokenLoop:
        def create_task(self, _coro):
            raise RuntimeError("scheduler unavailable")

    monkeypatch.setattr(route_events.asyncio, "get_running_loop", lambda: BrokenLoop())
    route_events.capture_route(job=_job(), selected_model="model-a", worker_id="worker-a")


def test_worker_transport_never_awaits_or_imports_shadow_state():
    path = ROOT / "grid_api/routers/worker_ws.py"
    source = path.read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        call = node.value.func
        assert not (isinstance(call, ast.Attribute) and isinstance(call.value, ast.Name) and call.value.id == "route_events")
    assert "validator_shadow" not in source
    assert "grid_validator_shadow_" not in source


def test_enabled_settings_require_a_private_route_secret():
    with pytest.raises(ValueError, match="route HMAC secret"):
        GridSettings(validator_shadow_observer_enabled=True)
    settings = GridSettings(
        validator_shadow_observer_enabled=True,
        validator_shadow_route_hmac_secret="x" * 32,
    )
    assert settings.validator_shadow_route_hmac_secret.get_secret_value() == "x" * 32

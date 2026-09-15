# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from grid_api.services import validator_shadow_collector as collector

NOW = datetime(2026, 9, 1, 16, 0, tzinfo=UTC)


def _route_event():
    return {
        "kind": "route",
        "route_ref": "a" * 64,
        "job_ref": "b" * 64,
        "observed_at": NOW.isoformat(),
        "task_class": "simple",
        "modality": "text",
        "capability": "text.instruction.v1",
        "candidate_basis": collector.CANDIDATE_BASIS,
        "candidates": json.dumps(
            [{"worker_id": "worker-a", "model": "model-a", "baseline_rank": 0}],
        ),
        "actual_model": "model-a",
        "actual_worker_id": "worker-a",
    }


def _outcome_event(*, finished_at: datetime = NOW + timedelta(seconds=2)):
    return {
        "kind": "outcome",
        "route_ref": "a" * 64,
        "finished_at": finished_at.isoformat(),
        "actual_worker_id": "worker-a",
        "terminal_status": "succeeded",
        "duration_ms": "2000",
    }


@pytest.mark.asyncio
async def test_route_event_uses_only_core_derived_shadow_writer(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        collector,
        "_run_for_time",
        lambda _at: _async_value(
            {"id": "run-1", "policy_config": {"candidate_basis": collector.CANDIDATE_BASIS}},
        ),
    )

    async def record_observation(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(collector.shadow, "record_observation", record_observation)
    result = await collector.process_event(_route_event(), now=NOW)
    assert result == "ack"
    assert seen["run_id"] == "run-1"
    assert seen["route_ref"] == "a" * 64
    assert seen["job_ref"] == "b" * 64
    assert seen["candidates"][0]["worker_id"] == "worker-a"


@pytest.mark.asyncio
async def test_route_event_rejects_an_unfrozen_candidate_basis(monkeypatch):
    monkeypatch.setattr(
        collector,
        "_run_for_time",
        lambda _at: _async_value(
            {"id": "run-1", "policy_config": {"candidate_basis": collector.CANDIDATE_BASIS}},
        ),
    )
    event = {**_route_event(), "candidate_basis": "exact_scheduler_candidates.v1"}
    with pytest.raises(ValueError, match="unknown candidate basis"):
        await collector.process_event(event, now=NOW)


@pytest.mark.asyncio
async def test_route_event_rejects_a_malformed_job_ref(monkeypatch):
    event = {**_route_event(), "job_ref": "raw-job-id"}
    with pytest.raises(ValueError, match="job_ref"):
        await collector.process_event(event, now=NOW)


@pytest.mark.asyncio
async def test_route_outside_a_run_is_discarded(monkeypatch):
    monkeypatch.setattr(collector, "_run_for_time", lambda _at: _async_value(None))
    assert await collector.process_event(_route_event(), now=NOW) == "discard"


@pytest.mark.asyncio
async def test_closed_run_is_not_selected_for_late_route_events(monkeypatch):
    class Result:
        def mappings(self):
            return self

        def all(self):
            return []

    class Session:
        async def execute(self, statement):
            assert "status =" in str(statement)
            return Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(collector, "new_session", lambda: _async_value(Session()))
    assert await collector._run_for_time(NOW) is None


@pytest.mark.asyncio
async def test_outcome_waits_for_route_then_records_once(monkeypatch):
    observed = {"id": 7, "actual_worker_id": "worker-a"}
    monkeypatch.setattr(collector, "_observation_for_route", lambda _ref: _async_value(observed))
    monkeypatch.setattr(collector, "_outcome_for_observation", lambda _oid: _async_value(None))
    seen = {}

    async def record_outcome(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(collector.shadow, "record_outcome", record_outcome)
    assert await collector.process_event(_outcome_event(), now=NOW + timedelta(seconds=3)) == "ack"
    assert seen == {
        "observation_id": 7,
        "actual_worker_id": "worker-a",
        "terminal_status": "succeeded",
        "duration_ms": 2000,
        "finished_at": NOW + timedelta(seconds=2),
    }


@pytest.mark.asyncio
async def test_duplicate_equivalent_outcome_is_acked_without_rewrite(monkeypatch):
    monkeypatch.setattr(
        collector,
        "_observation_for_route",
        lambda _ref: _async_value({"id": 7, "actual_worker_id": "worker-a"}),
    )
    monkeypatch.setattr(
        collector,
        "_outcome_for_observation",
        lambda _oid: _async_value(
            {"observation_id": 7, "actual_worker_id": "worker-a", "terminal_status": "succeeded"},
        ),
    )

    async def should_not_write(**_kwargs):
        raise AssertionError("duplicate outcome must not be rewritten")

    monkeypatch.setattr(collector.shadow, "record_outcome", should_not_write)
    assert await collector.process_event(_outcome_event(), now=NOW + timedelta(seconds=3)) == "ack"


@pytest.mark.asyncio
async def test_orphan_outcome_retries_then_becomes_a_bounded_error(monkeypatch):
    event = _outcome_event()
    monkeypatch.setattr(collector, "_observation_for_route", lambda _ref: _async_value(None))
    assert await collector.process_event(event, now=NOW + timedelta(seconds=5)) == "retry"

    monkeypatch.setattr(collector, "_run_for_time", lambda _at: _async_value({"id": "run-1"}))
    seen = {}

    async def record_error(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(collector.shadow, "record_error", record_error)
    result = await collector.process_event(event, now=NOW + timedelta(seconds=700))
    assert result == "discard"
    assert seen["error_code"] == "outcome_without_observation"


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_unknown_event_kind_is_not_silently_discarded():
    with pytest.raises(ValueError, match="unknown shadow event kind"):
        await collector.process_event({"kind": "unknown", "route_ref": "a" * 64})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("db unavailable"), collector.shadow.ShadowRuntimeMismatch("wrong release")])
async def test_rejected_event_remains_pending_when_error_cannot_be_recorded(monkeypatch, failure):
    async def read_batch(**_kwargs):
        return [("123-0", {"route_ref": "invalid"})]

    async def record_rejection(*_args):
        raise failure

    class Redis:
        async def xack(self, *_args):
            raise AssertionError("unrecorded evidence must stay pending")

        async def xdel(self, *_args):
            raise AssertionError("unrecorded evidence must not be deleted")

    monkeypatch.setattr(collector, "_read_batch", read_batch)
    monkeypatch.setattr(collector, "_record_invalid_event", record_rejection)
    monkeypatch.setattr(collector, "get_redis", Redis)
    assert await collector.collect_once(consumer="test") == {"acked": 0, "retried": 0, "failed": 1}


@pytest.mark.asyncio
async def test_late_conflict_records_error_against_original_observation(monkeypatch):
    observation = {"id": 7, "run_id": "original", "observed_at": NOW}
    monkeypatch.setattr(collector, "_observation_for_route", lambda _ref: _async_value(observation))
    recorded = []

    async def record_error(**kwargs):
        recorded.append(kwargs)

    async def no_time_lookup(_at):
        raise AssertionError("bound original observation determines the affected run")

    monkeypatch.setattr(collector.shadow, "record_error", record_error)
    monkeypatch.setattr(collector, "_run_for_time", no_time_lookup)
    late = NOW + timedelta(days=8)
    await collector._record_invalid_event(f"{int(late.timestamp() * 1000)}-0", _outcome_event(finished_at=late))
    assert recorded == [{
        "run_id": "original", "stage": "persist", "error_code": "invalid_outbox_event", "observed_at": NOW,
    }]


@pytest.mark.asyncio
async def test_invalid_event_outside_run_does_not_contaminate_later_run(monkeypatch):
    event_at = NOW - timedelta(days=8)
    seen = []

    async def run_for_time(at):
        seen.append(at)
        return None if at < NOW else {"id": "later-run"}

    async def no_error(**_kwargs):
        raise AssertionError("events from before this run must not be charged to it")

    monkeypatch.setattr(collector, "_run_for_time", run_for_time)
    monkeypatch.setattr(collector.shadow, "record_error", no_error)
    await collector._record_invalid_event(f"{int(event_at.timestamp() * 1000)}-0", {"kind": "route"})
    assert seen == [event_at]

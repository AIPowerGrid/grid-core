# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from grid_api.services import validator_shadow as shadow
from scripts import manage_validator_shadow_run as control

NOW = datetime(2026, 9, 8, 17, 0, tzinfo=UTC)


def _gate():
    snapshot = {
        "schema": "aipg.validator.shadow-live-start-gate.v1",
        "observed_at": NOW.isoformat(),
        "verified_independent_operators": 3,
        "participating_independent_operators": 3,
        "finalized_independent_probe_groups": 1,
        "cohort_monitor_status": "healthy",
        "unresolved_critical_incidents": False,
        "postgres_migration_verified": True,
        "postgres_concurrency_verified": True,
        "replay_verified": True,
        "no_side_effect_verified": True,
        "routing_effect": "none",
        "economic_effect": "none",
    }
    return {**snapshot, "evaluation": shadow.evaluate_start_gate(snapshot)}


def _run(status: str = "draft"):
    return {
        "id": "shadow_control_test",
        "status": status,
        "policy_version": shadow.POLICY_VERSION,
        "policy_config": shadow.frozen_policy_config(),
        "config_hash": "a" * 64,
        "implementation_commit": "b" * 40,
        "start_gate": _gate(),
        "start_gate_hash": shadow.commitment(_gate()),
        "started": NOW if status == "running" else None,
        "scheduled_end": NOW + timedelta(hours=168) if status == "running" else None,
        "ended": None,
    }


def test_verification_file_requires_exact_boolean_contract(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text(
        json.dumps({key: True for key in control.VERIFICATION_KEYS}),
        encoding="utf-8",
    )
    assert control._verification(str(valid)) == {key: True for key in control.VERIFICATION_KEYS}

    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        json.dumps({**{key: True for key in control.VERIFICATION_KEYS}, "extra": True}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exactly the four"):
        control._verification(str(invalid))


@pytest.mark.asyncio
async def test_start_refuses_missing_collector_leader(monkeypatch):
    row = _run()
    monkeypatch.setattr(control.shadow, "get_run", lambda _run_id: _async_value(row))
    monkeypatch.setattr(control.shadow, "live_start_gate_snapshot", lambda **_kwargs: _async_value(_gate()))
    monkeypatch.setattr(
        control,
        "_transport",
        lambda: _async_value(
            {
                "consumer_group_present": True,
                "leader_lease_ttl_seconds": -1,
                "drained": True,
            },
        ),
    )
    args = SimpleNamespace(run_id=row["id"], apply=False, expect_gate_hash=None)
    preview = await control._start(args, NOW)
    assert preview["eligible_to_apply"] is False
    assert "collector_leader_missing" in preview["blocking_reasons"]

    args.apply = True
    args.expect_gate_hash = preview["start_gate_hash"]
    with pytest.raises(shadow.ShadowStartGateError, match="collector_leader_missing"):
        await control._start(args, NOW)


@pytest.mark.asyncio
async def test_completion_refuses_undrained_outbox(monkeypatch):
    row = _run("running")
    end = row["scheduled_end"] + timedelta(minutes=5)
    monkeypatch.setattr(control.shadow, "get_run", lambda _run_id: _async_value(row))
    monkeypatch.setattr(
        control,
        "_transport",
        lambda: _async_value(
            {
                "consumer_group_present": True,
                "leader_lease_ttl_seconds": 60,
                "drained": False,
            },
        ),
    )
    args = SimpleNamespace(
        run_id=row["id"],
        status="completed",
        apply=False,
        expect_state_hash=None,
    )
    preview = await control._finish(args, end)
    assert preview["eligible_to_apply"] is False
    assert preview["blocking_reasons"] == ["observer_outbox_not_drained"]

    args.apply = True
    args.expect_state_hash = preview["current_run_state_hash"]
    with pytest.raises(shadow.ShadowConflict, match="observer_outbox_not_drained"):
        await control._finish(args, end)


@pytest.mark.asyncio
async def test_completion_requires_capture_grace_after_scheduled_end(monkeypatch):
    row = _run("running")
    end = row["scheduled_end"] + timedelta(seconds=control.CAPTURE_TIMEOUT_SECONDS + 1)
    monkeypatch.setattr(control.shadow, "get_run", lambda _run_id: _async_value(row))
    monkeypatch.setattr(
        control,
        "_transport",
        lambda: _async_value(
            {
                "consumer_group_present": True,
                "leader_lease_ttl_seconds": 60,
                "drained": True,
            },
        ),
    )
    args = SimpleNamespace(
        run_id=row["id"],
        status="completed",
        apply=False,
        expect_state_hash=None,
    )
    preview = await control._finish(args, end)
    assert preview["eligible_to_apply"] is False
    assert preview["blocking_reasons"] == ["observer_capture_grace_not_elapsed"]


async def _async_value(value):
    return value

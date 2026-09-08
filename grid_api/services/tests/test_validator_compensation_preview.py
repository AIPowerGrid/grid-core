# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import importlib.util
import json
import os
import random
import stat
from pathlib import Path

import pytest

from grid_api.services.validator_compensation_preview import (
    ATOMIC_PER_AIPG as AIPG,
)
from grid_api.services.validator_compensation_preview import (
    PreviewError,
    preview_allocation,
)

NOW = "2026-09-08T00:00:00Z"


def snapshot(groups=2, units=1):
    operators = [
        {
            "operator_group_id": f"opg_operator_{i:04}",
            "first_party": False,
            "review_status": "verified",
            "reviewed_at": "2026-09-01T00:00:00Z",
            "expires_at": "2026-10-01T00:00:00Z",
            "review_digest": "a" * 64,
        }
        for i in range(groups)
    ]
    return {
        "terms": {
            "campaign_id": "pilot-draft",
            "starts_at": "2026-09-01T00:00:00Z",
            "ends_at": NOW,
            "budget_atomic": str(10000 * AIPG),
            "operator_cap_atomic": str(2000 * AIPG),
            "daily_unit_cap": 100,
        },
        "operators": operators,
        "contributions": [
            {
                "assignment_id": f"asg_{i}_{j}",
                "operator_group_id": op["operator_group_id"],
                "probe_group_id": f"prg_{j}",
                "completed_at": "2026-09-02T12:00:00Z",
                "evidence_digest": "b" * 64,
            }
            for i, op in enumerate(operators)
            for j in range(units)
        ],
    }


def run(payload):
    return preview_allocation(payload, as_of=NOW)


def test_caps_and_unused_budget_are_exact_and_not_sendable():
    result = run(snapshot())
    assert result["allocated_atomic"] == str(4000 * AIPG)
    assert result["unallocated_atomic"] == str(6000 * AIPG)
    assert result["sendable"] is False
    assert result["input_authority"] == "unverified_reviewer_snapshot"


def test_replay_and_reordering_do_not_change_digest_or_allocation():
    payload = snapshot(3, 7)
    expected = run(payload)
    payload["contributions"] *= 3
    random.Random(17).shuffle(payload["contributions"])
    payload["operators"].reverse()
    assert run(payload) == expected


def test_conflicting_replay_rejected():
    payload = snapshot()
    bad = {**payload["contributions"][0], "evidence_digest": "c" * 64}
    payload["contributions"].append(bad)
    with pytest.raises(PreviewError, match="conflicting replay"):
        run(payload)


def test_multiple_assignments_same_operator_and_group_count_once():
    payload = snapshot(1)
    payload["contributions"].append(
        {**payload["contributions"][0], "assignment_id": "another_node_assignment", "evidence_digest": "d" * 64},
    )
    result = run(payload)
    assert result["reviewed_units"] == 1
    assert result["excluded"]["duplicate_operator_probe_group"] == 1


def test_daily_cap_and_campaign_wide_operator_cap():
    payload = snapshot(1, 101)
    result = run(payload)
    assert result["reviewed_units"] == 100
    assert result["excluded"]["daily_cap"] == 1
    assert result["allocated_atomic"] == str(2000 * AIPG)


def test_capped_group_cannot_reappear_on_another_day():
    payload = snapshot(1, 2)
    payload["terms"]["daily_unit_cap"] = 1
    payload["contributions"].append(
        {
            **payload["contributions"][1],
            "assignment_id": "next_day_assignment",
            "completed_at": "2026-09-03T12:00:00Z",
        },
    )
    result = run(payload)
    assert result["reviewed_units"] == 1
    assert result["excluded"] == {"daily_cap": 1, "duplicate_operator_probe_group": 1}


def test_empty_snapshot_allocates_nothing_without_mutating_input():
    payload = snapshot(0)
    before = copy.deepcopy(payload)
    result = run(payload)
    assert payload == before
    assert result["allocated_atomic"] == "0"
    assert result["unallocated_atomic"] == str(10000 * AIPG)
    assert result["allocations"] == []


def test_digest_binds_review_and_terms_but_normalizes_timezones():
    payload = snapshot(1)
    expected = run(payload)["simulation_digest"]
    payload["contributions"][0]["completed_at"] = "2026-09-02T08:00:00-04:00"
    assert run(payload)["simulation_digest"] == expected
    payload["operators"][0]["review_digest"] = "d" * 64
    changed_review = run(payload)["simulation_digest"]
    assert changed_review != expected
    payload["terms"]["budget_atomic"] = "1"
    assert run(payload)["simulation_digest"] != changed_review


@pytest.mark.parametrize(
    "change",
    [
        {"first_party": True},
        {"review_status": "unreviewed"},
        {"review_status": "rejected"},
        {"expires_at": NOW},
        {"reviewed_at": "2026-09-03T00:00:00Z"},
    ],
)
def test_ineligible_operators_never_receive_units(change):
    payload = snapshot(1)
    payload["operators"][0].update(change)
    assert run(payload)["allocations"] == []


@pytest.mark.parametrize("timestamp", ["2026-08-31T23:59:59Z", NOW, "2026-09-09T00:00:00Z"])
def test_window_is_half_open(timestamp):
    payload = snapshot(1)
    payload["contributions"][0]["completed_at"] = timestamp
    assert run(payload)["reviewed_units"] == 0


def test_future_work_cannot_count_in_partial_preview():
    result = preview_allocation(snapshot(1), as_of="2026-09-01T01:00:00Z")
    assert result["reviewed_units"] == 0
    assert result["window_complete"] is False


def test_flooring_never_exceeds_budget_and_does_not_redistribute_remainder():
    payload = snapshot(3)
    payload["terms"]["budget_atomic"] = "10"
    result = run(payload)
    assert [row["amount_atomic"] for row in result["allocations"]] == ["3"] * 3
    assert result["unallocated_atomic"] == "1"


@pytest.mark.parametrize("value", [True, 100, 1.2, "0", "-1", "NaN", str(10001 * AIPG)])
def test_invalid_or_oversized_budget_rejected(value):
    payload = snapshot()
    payload["terms"]["budget_atomic"] = value
    with pytest.raises(PreviewError):
        run(payload)


def test_unknown_operator_duplicate_operator_and_naive_time_rejected():
    for mutate in (
        lambda p: p["operators"].pop(),
        lambda p: p["operators"].append(copy.deepcopy(p["operators"][0])),
        lambda p: p["terms"].update(starts_at="2026-09-01T00:00:00"),
        lambda p: p["terms"].update(daily_unit_cap=True),
        lambda p: p["terms"].update(ends_at="2026-09-09T00:00:00Z"),
    ):
        payload = snapshot()
        mutate(payload)
        with pytest.raises(PreviewError):
            run(payload)


def test_budget_conservation_over_many_sizes():
    for groups in range(1, 31):
        payload = snapshot(groups, 3)
        result = run(payload)
        allocated = sum(int(row["amount_atomic"]) for row in result["allocations"])
        assert allocated <= 10000 * AIPG
        assert allocated + int(result["unallocated_atomic"]) == 10000 * AIPG
        assert all(int(row["amount_atomic"]) <= 2000 * AIPG for row in result["allocations"])


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[3] / "scripts" / "preview_validator_compensation.py"
    spec = importlib.util.spec_from_file_location("compensation_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only private file CLI")
def test_cli_private_input_output_and_no_overwrite(cli, tmp_path, monkeypatch, capsys):
    source, output = tmp_path / "review.json", tmp_path / "draft.json"
    source.write_text(json.dumps(snapshot()))
    source.chmod(0o600)
    monkeypatch.setattr("sys.argv", ["preview", "--input", str(source), "--output", str(output), "--as-of", NOW])
    assert cli.main() == 0
    terminal = capsys.readouterr().out
    assert "opg_" not in terminal and str(source) not in terminal
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    original = output.read_bytes()
    assert cli.main() == 1
    assert output.read_bytes() == original
    source.chmod(0o644)
    with pytest.raises(PreviewError):
        cli.read_private(source)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only private file CLI")
def test_cli_rejects_symlinks_duplicate_json_and_oversized_files(cli, tmp_path):
    source, link = tmp_path / "input", tmp_path / "link"
    source.write_text('{"terms": 1, "terms": 2}')
    source.chmod(0o600)
    with pytest.raises(PreviewError):
        cli.read_private(source)
    source.write_text('{"terms": NaN}')
    with pytest.raises(PreviewError):
        cli.read_private(source)
    link.symlink_to(source)
    with pytest.raises(OSError):
        cli.read_private(link)
    source.write_bytes(b" " * (cli.MAX_BYTES + 1))
    with pytest.raises(PreviewError):
        cli.read_private(source)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(PreviewError):
        cli.read_private(fifo)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only private file CLI")
def test_failed_sync_preserves_private_draft_and_reports_failure(cli, tmp_path, monkeypatch, capsys):
    source, output = tmp_path / "review.json", tmp_path / "draft.json"
    source.write_text(json.dumps(snapshot()))
    source.chmod(0o600)
    monkeypatch.setattr("sys.argv", ["preview", "--input", str(source), "--output", str(output), "--as-of", NOW])

    def fail_sync(_fd):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(cli.os, "fsync", fail_sync)
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "synthetic" not in captured.err
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

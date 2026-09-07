# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime, timedelta

import pytest

from grid_api.services import validator_operators as operators

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def row(**extra):
    return {
        "qualification_started_at": NOW - timedelta(days=20),
        "heartbeat_sample_count": 227,
        "heartbeat_window_started_at": NOW - timedelta(days=4),
        "heartbeat_window_samples": [],
        **extra,
    }


def full_window():
    end = int(NOW.timestamp()) // operators.RECOVERY_BUCKET_SECONDS
    return list(range(end - operators.RECOVERY_BUCKET_COUNT + 1, end + 1))


def test_recent_operation_recovers_without_rewriting_old_history():
    original = row(heartbeat_window_samples=full_window())
    before = dict(original)
    metrics = operators.qualification_metrics(original, now=NOW)
    assert metrics["coverage_basis"] == "recent_72h"
    assert metrics["sample_coverage"] == 1
    assert metrics["lifetime_sample_coverage"] < .05
    assert metrics["time_ready"] and metrics["coverage_ready"]
    assert original == before


def test_migration_and_warmup_preserve_existing_qualified_progress():
    for start in (None, NOW - timedelta(hours=71)):
        metrics = operators.qualification_metrics(row(
            heartbeat_window_started_at=start, heartbeat_sample_count=6000,
        ), now=NOW)
        assert metrics["coverage_basis"] == "since_enrollment"
        assert metrics["coverage_ready"]
        assert not metrics["recovery_window_ready"]


def test_one_returning_heartbeat_does_not_erase_a_recent_outage():
    metrics = operators.qualification_metrics(row(
        heartbeat_window_samples=[full_window()[-1]], heartbeat_sample_count=6000,
    ), now=NOW)
    assert metrics["coverage_basis"] == "recent_72h"
    assert metrics["heartbeat_samples"] == 1
    assert not metrics["coverage_ready"]


@pytest.mark.parametrize("samples", [None, {}, "bad", [True], [0], [999999999], [1] * 865])
def test_malformed_or_outside_window_samples_never_grant_coverage(samples):
    metrics = operators.qualification_metrics(row(heartbeat_window_samples=samples), now=NOW)
    assert not metrics["coverage_ready"]


def test_repeated_heartbeat_in_one_bucket_counts_once_and_prunes_only_recent_ring():
    original = row(heartbeat_window_samples=full_window())
    updates = operators.record_recent_heartbeat(original, now=NOW)
    assert len(updates["heartbeat_window_samples"]) == 864
    assert "heartbeat_sample_count" not in updates
    assert "qualification_started_at" not in updates
    later = NOW + timedelta(minutes=5)
    updates = operators.record_recent_heartbeat({**original, **updates}, now=later)
    assert len(updates["heartbeat_window_samples"]) == 864
    assert full_window()[0] not in updates["heartbeat_window_samples"]
    assert updates["heartbeat_window_started_at"] == original["heartbeat_window_started_at"]


def test_missing_timestamps_are_not_backfilled_from_cumulative_count():
    original = row(heartbeat_window_started_at=None, heartbeat_sample_count=6000)
    updates = operators.record_recent_heartbeat(original, now=NOW)
    assert updates["heartbeat_window_started_at"] == NOW
    assert updates["heartbeat_window_samples"] == [full_window()[-1]]


def test_duplicate_samples_and_exact_80_percent_boundary():
    required = 692  # ceil(864 * 0.8)
    for count in (required - 1, required):
        samples = full_window()[:count]
        metrics = operators.qualification_metrics(row(heartbeat_window_samples=samples), now=NOW)
        assert metrics["coverage_ready"] == (count == required)
    metrics = operators.qualification_metrics(row(heartbeat_window_samples=[full_window()[-1]] * 864), now=NOW)
    assert metrics["heartbeat_samples"] == 1

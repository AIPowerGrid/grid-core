# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from datetime import UTC, datetime, timedelta

from grid_api.services import validators


def _row():
    now = datetime.now(UTC)
    return {
        "id": "asg_test",
        "probe_group_id": "prg_secret",
        "grid_nonce": "nonce-secret",
        "target_worker_id": "worker-secret",
        "target_worker_name": "target-display-name",
        "model": "model-secret",
        "modality": "text",
        "capability": "text.reasoning.v1",
        "canary_kind": "math.add",
        "scoring_policy_id": "text.batch.unique.v8",
        "challenge": {
            "kind": "math.add",
            "prompt": "Private challenge prompt",
            "expected_hash": "a" * 64,
            "max_tokens": 64,
            "temperature": 0,
        },
        "status": "pending",
        "quorum_status": "pending",
        "quorum_outcome": None,
        "probe_status": "not_started",
        "probe_attempts": 0,
        "probe_job_id": None,
        "created": now,
        "expires": now + timedelta(minutes=15),
        "probed": None,
        "finalized": None,
    }


def test_sealed_assignment_poll_hides_target_nonce_model_and_challenge():
    row = _row()
    payload = validators._assignment_to_dict(row, sealed=True)
    rendered = json.dumps(payload, sort_keys=True)

    assert payload["sealed"] is True
    assert payload["assignment_seal"] == validators._assignment_seal(row)
    for secret in (
        row["probe_group_id"],
        row["grid_nonce"],
        row["target_worker_id"],
        row["target_worker_name"],
        row["model"],
        row["canary_kind"],
        row["challenge"]["prompt"],
    ):
        assert secret not in rendered


def test_terminal_disclosure_matches_seal_and_mutation_does_not():
    row = _row()
    disclosure = validators._assignment_disclosure(row)

    assert disclosure["assignment_seal"] == validators._assignment_seal(row)
    assert disclosure["challenge"] == row["challenge"]

    changed = {**row, "model": "swapped-model"}
    assert validators._assignment_seal(changed) != disclosure["assignment_seal"]


def test_legacy_full_assignment_keeps_pre_seal_compatibility():
    row = _row()
    payload = validators._assignment_to_dict(row, sealed=False)

    assert payload["sealed"] is False
    assert "assignment_seal" not in payload
    assert payload["grid_nonce"] == row["grid_nonce"]
    assert payload["challenge"]["prompt"] == row["challenge"]["prompt"]

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Economically inert validator shadow-authority policy and audit store.

This module is deliberately absent from the production router, worker-health,
settlement, credit, payout, reward, bond, strike, and slashing paths.  A future
background collector may call it only after the real route has completed its
normal selection.  The pure policy never performs I/O; persisted observations
are immutable comparisons and cannot be consumed by production decisions.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..database import new_session
from ..v2.schema import ledger as ledger_t
from ..v2.schema import validator_assignments as assignments_t
from ..v2.schema import validator_attestations as attestations_t
from ..v2.schema import validator_probe_groups as probe_groups_t
from ..v2.schema import validator_shadow_capacity_samples as capacity_t
from ..v2.schema import validator_shadow_errors as errors_t
from ..v2.schema import validator_shadow_observations as observations_t
from ..v2.schema import validator_shadow_outcomes as outcomes_t
from ..v2.schema import validator_shadow_runs as runs_t
from ..v2.schema import validators as validators_t
from .route_commitments import job_ref as committed_job_ref

POLICY_VERSION = "aipg.validator.shadow.protocol-capability.v4"
CANDIDATE_BASIS = "post_dispatch_connected_compatible_replicas.v1"
RUN_HOURS = 168
MIN_QUALIFICATION_SECONDS = 72 * 3600
TRANSITION_CLOCK_SKEW_SECONDS = 300
DEFAULT_POLICY_CONFIG: dict[str, Any] = {
    "policy_version": POLICY_VERSION,
    "candidate_basis": CANDIDATE_BASIS,
    "validator_baseline_version": "v0.1.0-preview.13",
    "validator_heartbeat_fresh_seconds": 900,
    "minimum_qualification_seconds": MIN_QUALIFICATION_SECONDS,
    "evidence_window_seconds": 24 * 3600,
    "quorum_min": 3,
    "allowed_dimensions": ["protocol_conformance", "capability", "fidelity"],
    "negative_outcomes": ["failed"],
    "positive_outcomes": ["healthy"],
    "required_sample_coverage": 0.80,
    "required_terminal_outcome_coverage": 0.80,
    "required_route_capture_coverage": 0.80,
    "maximum_quorum_gap_seconds": 3600,
    "sample_interval_seconds": 300,
    "run_hours": RUN_HOURS,
}

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_DECISIONS = {"same", "would_change", "would_exclude", "insufficient_evidence"}
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timeout"}
_OBJECTIVE_OUTCOMES = {"healthy", "slow", "failed"}
_VERIFICATION_KEYS = (
    "postgres_migration_verified",
    "postgres_concurrency_verified",
    "replay_verified",
    "no_side_effect_verified",
)
_TEXT_PROTOCOL_CAPABILITIES = {
    "text.instruction.v1",
    "text.structured.v1",
    "text.stop_sequence.v1",
    "text.token_limit.v1",
    "text.tool_call.v1",
}


class ShadowError(RuntimeError):
    """Base class for shadow-observer contract failures."""


class ShadowDisabled(ShadowError):
    """Raised when a caller attempts collection while the dark gate is off."""


class ShadowConflict(ShadowError):
    """Raised when an idempotency key is reused for different immutable data."""


class ShadowStartGateError(ShadowError):
    """Raised when a run is started without satisfying every frozen gate."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _fresh_transition_time(value: datetime, *, label: str) -> datetime:
    """Reject backdated or future-dated lifecycle transitions."""
    current = _now()
    candidate = _aware(value)
    if abs((candidate - current).total_seconds()) > TRANSITION_CLOCK_SKEW_SECONDS:
        raise ValueError(f"{label} must be within five minutes of server time")
    return candidate


def _iso(value: datetime) -> str:
    return _aware(value).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return the one canonical JSON representation used by every commitment."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_json_default,
    )


def commitment(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def run_state_hash(row: Mapping[str, Any]) -> str:
    """Commit the mutable run lifecycle fields used by operator transitions."""
    return commitment(
        {
            "id": str(row.get("id") or ""),
            "status": str(row.get("status") or ""),
            "policy_version": str(row.get("policy_version") or ""),
            "config_hash": str(row.get("config_hash") or ""),
            "implementation_commit": str(row.get("implementation_commit") or ""),
            "start_gate_hash": str(row.get("start_gate_hash") or ""),
            "started": row.get("started"),
            "scheduled_end": row.get("scheduled_end"),
            "ended": row.get("ended"),
        },
    )


def frozen_policy_config(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate and return a canonical JSON-safe policy configuration."""
    config = dict(DEFAULT_POLICY_CONFIG)
    if overrides:
        unknown = set(overrides) - set(config)
        if unknown:
            raise ValueError(f"unknown shadow policy fields: {sorted(unknown)}")
        config.update(dict(overrides))

    if config["policy_version"] != POLICY_VERSION:
        raise ValueError("unknown shadow policy version")
    if config["candidate_basis"] != CANDIDATE_BASIS:
        raise ValueError("unknown shadow candidate basis")
    if int(config["evidence_window_seconds"]) < 60:
        raise ValueError("evidence_window_seconds must be at least 60")
    baseline = str(config["validator_baseline_version"] or "").strip()
    if not baseline or len(baseline) > 64:
        raise ValueError("validator_baseline_version is required")
    config["validator_baseline_version"] = baseline
    heartbeat_fresh = int(config["validator_heartbeat_fresh_seconds"])
    if not 60 <= heartbeat_fresh <= 3600:
        raise ValueError("validator_heartbeat_fresh_seconds must be between 60 and 3600")
    config["validator_heartbeat_fresh_seconds"] = heartbeat_fresh
    qualification = int(config["minimum_qualification_seconds"])
    if qualification < MIN_QUALIFICATION_SECONDS:
        raise ValueError("shadow operators must qualify for at least 72 hours")
    config["minimum_qualification_seconds"] = qualification
    if int(config["quorum_min"]) < 3:
        raise ValueError("shadow quorum may not be below three")
    if int(config["run_hours"]) < RUN_HOURS:
        raise ValueError("shadow run may not be shorter than 168 hours")
    coverage = float(config["required_sample_coverage"])
    if not 0.80 <= coverage <= 1.0:
        raise ValueError("required_sample_coverage may not be below 0.80")
    if int(config["maximum_quorum_gap_seconds"]) > 3600:
        raise ValueError("maximum quorum gap may not exceed one hour")
    if not 60 <= int(config["sample_interval_seconds"]) <= 3600:
        raise ValueError("sample_interval_seconds must be between 60 and 3600")

    allowed = sorted({str(value) for value in config["allowed_dimensions"]})
    if not allowed or "quality" in allowed:
        raise ValueError("subjective quality is not an allowed shadow dimension")
    config["allowed_dimensions"] = allowed
    config["negative_outcomes"] = sorted({str(v) for v in config["negative_outcomes"]})
    config["positive_outcomes"] = sorted({str(v) for v in config["positive_outcomes"]})
    config["evidence_window_seconds"] = int(config["evidence_window_seconds"])
    config["quorum_min"] = int(config["quorum_min"])
    config["required_sample_coverage"] = coverage
    terminal_coverage = float(config["required_terminal_outcome_coverage"])
    if not 0.80 <= terminal_coverage <= 1.0:
        raise ValueError("required_terminal_outcome_coverage may not be below 0.80")
    config["required_terminal_outcome_coverage"] = terminal_coverage
    route_coverage = float(config["required_route_capture_coverage"])
    if not 0.80 <= route_coverage <= 1.0:
        raise ValueError("required_route_capture_coverage may not be below 0.80")
    config["required_route_capture_coverage"] = route_coverage
    config["maximum_quorum_gap_seconds"] = int(config["maximum_quorum_gap_seconds"])
    config["sample_interval_seconds"] = int(config["sample_interval_seconds"])
    config["run_hours"] = int(config["run_hours"])
    return json.loads(canonical_json(config))


def runtime_policy_config(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Freeze policy only when it matches this release's rollout controls."""
    config = frozen_policy_config(overrides)
    settings = get_settings()
    expected_baseline = str(settings.validator_cohort_baseline_version or "").removeprefix("v")
    actual_baseline = str(config["validator_baseline_version"]).removeprefix("v")
    if not expected_baseline or actual_baseline != expected_baseline:
        raise ValueError("shadow policy must use the configured cohort baseline")
    configured_interval = int(getattr(settings, "validator_shadow_sample_seconds", 300))
    if int(config["sample_interval_seconds"]) != configured_interval:
        raise ValueError("shadow policy sample interval must match deployment configuration")
    return config


def evaluate_start_gate(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate the explicit three-operator, zero-authority start contract."""
    checks = {
        "verified_independent_operators": int(snapshot.get("verified_independent_operators", 0)) >= 3,
        "participating_independent_operators": int(snapshot.get("participating_independent_operators", 0)) >= 3,
        "finalized_independent_probe_groups": int(snapshot.get("finalized_independent_probe_groups", 0)) >= 1,
        "cohort_monitor_clear": snapshot.get("cohort_monitor_status") in {"healthy", "degraded"}
        and not bool(snapshot.get("unresolved_critical_incidents")),
        "postgres_migration_verified": bool(snapshot.get("postgres_migration_verified")),
        "postgres_concurrency_verified": bool(snapshot.get("postgres_concurrency_verified")),
        "replay_verified": bool(snapshot.get("replay_verified")),
        "no_side_effect_verified": bool(snapshot.get("no_side_effect_verified")),
        "routing_effect_none": snapshot.get("routing_effect") == "none",
        "economic_effect_none": snapshot.get("economic_effect") == "none",
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "schema": "aipg.validator.shadow-start-gate.v1",
        "eligible": not failed,
        "checks": checks,
        "failed": failed,
        "verified_independent_operators": int(snapshot.get("verified_independent_operators", 0)),
        "participating_independent_operators": int(snapshot.get("participating_independent_operators", 0)),
        "finalized_independent_probe_groups": int(snapshot.get("finalized_independent_probe_groups", 0)),
        "cohort_monitor_status": str(snapshot.get("cohort_monitor_status") or "unknown"),
        "routing_effect": str(snapshot.get("routing_effect") or "unknown"),
        "economic_effect": str(snapshot.get("economic_effect") or "unknown"),
    }


def _normalize_candidates(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, candidate in enumerate(candidates):
        worker_id = str(candidate.get("worker_id") or "").strip()
        model = str(candidate.get("model") or "").strip()
        if not worker_id or not model:
            raise ValueError("every candidate requires worker_id and model")
        key = (worker_id, model)
        if key in seen:
            raise ValueError("candidate worker/model pairs must be unique")
        seen.add(key)
        normalized.append(
            {
                "worker_id": worker_id,
                "model": model,
                "baseline_rank": int(candidate.get("baseline_rank", index)),
            },
        )
    if not normalized:
        raise ValueError("at least one frozen candidate is required")
    return sorted(normalized, key=lambda item: (item["baseline_rank"], item["model"], item["worker_id"]))


def _evidence_dimension(modality: str, capability: str) -> str:
    if capability in _TEXT_PROTOCOL_CAPABILITIES:
        return "protocol_conformance"
    if capability.startswith("text."):
        return "capability"
    if "fidelity" in capability:
        return "fidelity"
    if modality in {"image", "video", "audio"}:
        return "protocol_conformance"
    return "availability"


def _eligible_operator_conditions(
    *,
    observed_at: datetime,
    config: Mapping[str, Any],
    bind_to_group: bool = True,
) -> tuple[Any, ...]:
    heartbeat_cutoff = observed_at - timedelta(
        seconds=int(config["validator_heartbeat_fresh_seconds"]),
    )
    qualification_cutoff = observed_at - timedelta(
        seconds=int(config["minimum_qualification_seconds"]),
    )
    baseline = str(config["validator_baseline_version"]).removeprefix("v")
    conditions: list[Any] = [
        validators_t.c.status == "active",
        validators_t.c.operator_group_id.isnot(None),
        validators_t.c.operator_group_id.like(r"opg\_%", escape="\\"),
        validators_t.c.independence_status == "verified",
        validators_t.c.independence_review_ref.isnot(None),
        validators_t.c.independence_reviewed_at.isnot(None),
        validators_t.c.independence_reviewed_at >= validators_t.c.qualification_started_at,
        validators_t.c.independence_reviewed_at <= observed_at,
        validators_t.c.independence_expires_at >= observed_at,
        validators_t.c.last_heartbeat >= heartbeat_cutoff,
        validators_t.c.last_heartbeat <= observed_at,
        validators_t.c.qualification_started_at.isnot(None),
        validators_t.c.qualification_started_at <= qualification_cutoff,
        sa.func.ltrim(sa.func.trim(validators_t.c.software_version), "v") == baseline,
    ]
    if bind_to_group:
        conditions.append(validators_t.c.qualification_started_at <= probe_groups_t.c.created)
    return tuple(conditions)


async def _authoritative_support_rows(
    session,
    *,
    observed_at: datetime,
    config: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]] | None = None,
    modality: str | None = None,
    capability: str | None = None,
) -> list[Mapping[str, Any]]:
    """Return only Core-reverified, independently controlled quorum support."""
    lower = observed_at - timedelta(seconds=int(config["evidence_window_seconds"]))
    conditions: list[Any] = [
        probe_groups_t.c.status == "finalized",
        probe_groups_t.c.probe_status == "completed",
        probe_groups_t.c.quorum_status == "finalized",
        probe_groups_t.c.quorum_outcome.in_(tuple(sorted(_OBJECTIVE_OUTCOMES))),
        probe_groups_t.c.finalized.isnot(None),
        probe_groups_t.c.finalized >= lower,
        probe_groups_t.c.finalized <= observed_at,
        attestations_t.c.authority == "authoritative",
        attestations_t.c.quorum_status == "finalized",
        attestations_t.c.signature_status == "verified",
        attestations_t.c.signature.isnot(None),
        attestations_t.c.created <= observed_at,
        attestations_t.c.created <= probe_groups_t.c.finalized,
        attestations_t.c.assignment_id == assignments_t.c.id,
        attestations_t.c.validator_id == assignments_t.c.validator_id,
        attestations_t.c.probe_group_id == assignments_t.c.probe_group_id,
        attestations_t.c.grid_nonce == assignments_t.c.grid_nonce,
        attestations_t.c.evidence_hash == assignments_t.c.probe_evidence_hash,
        attestations_t.c.verdict == assignments_t.c.probe_verdict,
        attestations_t.c.verdict == probe_groups_t.c.quorum_outcome,
        attestations_t.c.validator_wallet == validators_t.c.signing_wallet,
        assignments_t.c.validator_wallet == validators_t.c.signing_wallet,
        attestations_t.c.account_id == assignments_t.c.account_id,
        attestations_t.c.account_id == validators_t.c.account_id,
        assignments_t.c.account_id == validators_t.c.account_id,
        assignments_t.c.status == "finalized",
        assignments_t.c.quorum_status == "finalized",
        assignments_t.c.probe_status == "completed",
        assignments_t.c.finalized.isnot(None),
        assignments_t.c.finalized <= observed_at,
        assignments_t.c.probe_evidence_hash.isnot(None),
        sa.func.length(assignments_t.c.probe_evidence_hash) == 64,
        sa.func.length(attestations_t.c.attestation_hash) == 64,
        sa.func.length(probe_groups_t.c.challenge_hash) == 64,
        assignments_t.c.probe_verdict.in_(tuple(sorted(_OBJECTIVE_OUTCOMES))),
        assignments_t.c.target_worker_id == probe_groups_t.c.target_worker_id,
        assignments_t.c.model == probe_groups_t.c.model,
        assignments_t.c.modality == probe_groups_t.c.modality,
        assignments_t.c.capability == probe_groups_t.c.capability,
        assignments_t.c.canary_kind == probe_groups_t.c.canary_kind,
        assignments_t.c.scoring_policy_id == probe_groups_t.c.scoring_policy_id,
        attestations_t.c.worker_id == probe_groups_t.c.target_worker_id,
        attestations_t.c.model == probe_groups_t.c.model,
        attestations_t.c.modality == probe_groups_t.c.modality,
        attestations_t.c.capability == probe_groups_t.c.capability,
        attestations_t.c.canary_kind == probe_groups_t.c.canary_kind,
        *_eligible_operator_conditions(observed_at=observed_at, config=config),
    ]
    if candidates is not None:
        pairs = _normalize_candidates(candidates)
        conditions.append(
            sa.or_(
                *(
                    sa.and_(
                        probe_groups_t.c.target_worker_id == row["worker_id"],
                        probe_groups_t.c.model == row["model"],
                    )
                    for row in pairs
                ),
            ),
        )
    if modality is not None:
        conditions.append(probe_groups_t.c.modality == modality)
    if capability is not None:
        conditions.append(probe_groups_t.c.capability == capability)

    query = (
        sa.select(
            probe_groups_t.c.id.label("group_id"),
            probe_groups_t.c.target_worker_id.label("worker_id"),
            probe_groups_t.c.model,
            probe_groups_t.c.modality,
            probe_groups_t.c.capability,
            probe_groups_t.c.scoring_policy_id,
            probe_groups_t.c.challenge_hash,
            probe_groups_t.c.quorum_status,
            probe_groups_t.c.quorum_outcome.label("outcome"),
            probe_groups_t.c.finalized.label("finalized_at"),
            assignments_t.c.probe_evidence_hash.label("evidence_hash"),
            attestations_t.c.attestation_hash,
            validators_t.c.operator_group_id,
        )
        .select_from(
            probe_groups_t.join(
                assignments_t,
                assignments_t.c.probe_group_id == probe_groups_t.c.id,
            )
            .join(
                attestations_t,
                attestations_t.c.assignment_id == assignments_t.c.id,
            )
            .join(validators_t, validators_t.c.id == assignments_t.c.validator_id),
        )
        .where(*conditions)
        .order_by(probe_groups_t.c.finalized, probe_groups_t.c.id)
    )
    return (await session.execute(query)).mappings().all()


def _aggregate_authoritative_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        if any(not _HEX_64.fullmatch(str(row[field])) for field in ("challenge_hash", "evidence_hash", "attestation_hash")):
            continue
        group_id = str(row["group_id"])
        group = groups.setdefault(
            group_id,
            {
                "row": row,
                "operator_groups": set(),
                "attestation_hashes": set(),
            },
        )
        group["operator_groups"].add(str(row["operator_group_id"]))
        group["attestation_hashes"].add(str(row["attestation_hash"]))

    evidence: list[dict[str, Any]] = []
    for group_id, values in groups.items():
        row = values["row"]
        finalized_at = _aware(row["finalized_at"])
        operator_groups = sorted(values["operator_groups"])
        attestation_hashes = sorted(values["attestation_hashes"])
        group_commitment = commitment(
            {
                "schema": "aipg.validator.shadow-evidence-commitment.v1",
                "group_id": group_id,
                "challenge_hash": row["challenge_hash"],
                "outcome": row["outcome"],
                "finalized_at": finalized_at,
                "supporting_operator_groups": operator_groups,
                "supporting_attestations": attestation_hashes,
            },
        )
        evidence.append(
            {
                "group_commitment": group_commitment,
                "worker_id": str(row["worker_id"]),
                "model": str(row["model"]),
                "modality": str(row["modality"]),
                "capability": str(row["capability"]),
                "scoring_policy_id": str(row["scoring_policy_id"]),
                "evidence_dimension": _evidence_dimension(
                    str(row["modality"]),
                    str(row["capability"]),
                ),
                "quorum_status": str(row["quorum_status"]),
                "outcome": str(row["outcome"]),
                "distinct_operator_count": len(operator_groups),
                "bindings_valid": True,
                "finalized_at": finalized_at,
            },
        )
    return _normalize_evidence(evidence)


async def authoritative_evidence_snapshot(
    *,
    candidates: Sequence[Mapping[str, Any]],
    modality: str,
    capability: str,
    observed_at: datetime,
    policy_config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build a privacy-safe evidence snapshot from Core's authoritative rows."""
    current = _aware(observed_at)
    config = frozen_policy_config(policy_config)
    async with await new_session() as session:
        rows = await _authoritative_support_rows(
            session,
            observed_at=current,
            config=config,
            candidates=candidates,
            modality=str(modality),
            capability=str(capability),
        )
    return _aggregate_authoritative_rows(rows)


async def live_capacity_snapshot(
    *,
    observed_at: datetime | None = None,
    policy_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive independent capacity only from current Core authority rows."""
    current = _aware(observed_at or _now())
    config = frozen_policy_config(policy_config)
    operator_conditions = _eligible_operator_conditions(
        observed_at=current,
        config=config,
        bind_to_group=False,
    )
    async with await new_session() as session:
        verified = int(
            await session.scalar(
                sa.select(sa.func.count(sa.distinct(validators_t.c.operator_group_id))).where(
                    *operator_conditions,
                ),
            )
            or 0,
        )
        support_rows = await _authoritative_support_rows(
            session,
            observed_at=current,
            config=config,
        )

    operators_by_group: dict[str, set[str]] = {}
    participating: set[str] = set()
    for row in support_rows:
        group_id = str(row["group_id"])
        operator_group = str(row["operator_group_id"])
        operators_by_group.setdefault(group_id, set()).add(operator_group)
        participating.add(operator_group)
    finalized_groups = sum(1 for operator_groups in operators_by_group.values() if len(operator_groups) >= int(config["quorum_min"]))
    return {
        "schema": "aipg.validator.shadow-live-capacity.v1",
        "observed_at": _iso(current),
        "verified_independent_operators": verified,
        "participating_independent_operators": len(participating),
        "finalized_independent_probe_groups": finalized_groups,
    }


async def live_start_gate_snapshot(
    *,
    verification: Mapping[str, Any],
    observed_at: datetime | None = None,
    policy_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive live network gates; CI proof booleans remain explicit inputs."""
    from .validator_cohort_monitor import inspect_cohort_health

    current = _aware(observed_at or _now())
    config = frozen_policy_config(policy_config)
    capacity = await live_capacity_snapshot(
        observed_at=current,
        policy_config=config,
    )

    cohort = await inspect_cohort_health(
        window_hours=max(1, int(config["evidence_window_seconds"]) // 3600),
        baseline_version=str(config["validator_baseline_version"]),
        now=current,
    )
    critical = any(issue.get("severity") == "critical" for issue in cohort["issues"])
    cohort_status = "degraded" if cohort["status"] == "warning" else cohort["status"]
    snapshot = {
        "schema": "aipg.validator.shadow-live-start-gate.v1",
        "observed_at": _iso(current),
        "verified_independent_operators": capacity["verified_independent_operators"],
        "participating_independent_operators": capacity["participating_independent_operators"],
        "finalized_independent_probe_groups": capacity["finalized_independent_probe_groups"],
        "cohort_monitor_status": cohort_status,
        "unresolved_critical_incidents": critical,
        "postgres_migration_verified": bool(verification.get("postgres_migration_verified")),
        "postgres_concurrency_verified": bool(verification.get("postgres_concurrency_verified")),
        "replay_verified": bool(verification.get("replay_verified")),
        "no_side_effect_verified": bool(verification.get("no_side_effect_verified")),
        "routing_effect": "none",
        "economic_effect": "none",
    }
    return {**snapshot, "evaluation": evaluate_start_gate(snapshot)}


def _normalize_evidence(evidence: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in evidence:
        finalized_at = item.get("finalized_at")
        if isinstance(finalized_at, datetime):
            finalized_at = _iso(finalized_at)
        row = {
            "group_commitment": str(item.get("group_commitment") or "").lower(),
            "worker_id": str(item.get("worker_id") or ""),
            "model": str(item.get("model") or ""),
            "modality": str(item.get("modality") or ""),
            "capability": str(item.get("capability") or ""),
            "scoring_policy_id": str(item.get("scoring_policy_id") or ""),
            "evidence_dimension": str(item.get("evidence_dimension") or ""),
            "quorum_status": str(item.get("quorum_status") or ""),
            "outcome": str(item.get("outcome") or ""),
            "distinct_operator_count": int(item.get("distinct_operator_count") or 0),
            "bindings_valid": bool(item.get("bindings_valid")),
            "finalized_at": finalized_at,
        }
        normalized.append(row)
    return sorted(
        normalized,
        key=lambda row: (
            row["worker_id"],
            row["model"],
            row["capability"],
            row["finalized_at"] or "",
            row["group_commitment"],
        ),
    )


def _eligible_evidence(
    row: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    modality: str,
    capability: str,
    observed_at: datetime,
    config: Mapping[str, Any],
) -> bool:
    if row["worker_id"] != candidate["worker_id"] or row["model"] != candidate["model"]:
        return False
    if row["modality"] != modality or row["capability"] != capability:
        return False
    if row["quorum_status"] != "finalized" or not row["bindings_valid"]:
        return False
    if row["evidence_dimension"] not in set(config["allowed_dimensions"]):
        return False
    if row["distinct_operator_count"] < int(config["quorum_min"]):
        return False
    if not _HEX_64.fullmatch(row["group_commitment"]):
        return False
    try:
        finalized_at = _aware(datetime.fromisoformat(str(row["finalized_at"])))
    except (TypeError, ValueError):
        return False
    lower = observed_at - timedelta(seconds=int(config["evidence_window_seconds"]))
    return lower <= finalized_at <= observed_at


def evaluate_advisory(
    *,
    candidates: Sequence[Mapping[str, Any]],
    evidence: Iterable[Mapping[str, Any]],
    actual_model: str,
    actual_worker_id: str | None,
    modality: str,
    requested_capability: str,
    observed_at: datetime,
    policy_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute a replayable hypothetical route without I/O or mutations.

    A change requires both fresh finalized objective failure evidence for the
    actual worker and fresh finalized healthy evidence for an alternative.
    Slow, disputed, stale, unsigned, under-quorum, or missing evidence remains
    visible in the snapshot but produces no opinion.
    """
    now = _aware(observed_at)
    config = frozen_policy_config(policy_config)
    frozen_candidates = _normalize_candidates(candidates)
    frozen_evidence = _normalize_evidence(evidence)
    actual_model = str(actual_model or "").strip()
    actual_worker_id = str(actual_worker_id).strip() if actual_worker_id else None
    modality = str(modality or "").strip()
    requested_capability = str(requested_capability or "").strip()
    if not actual_model or not modality or not requested_capability:
        raise ValueError("actual_model, modality, and requested_capability are required")

    actual_matches = [
        candidate
        for candidate in frozen_candidates
        if candidate["model"] == actual_model and (actual_worker_id is None or candidate["worker_id"] == actual_worker_id)
    ]
    if len(actual_matches) != 1:
        reason = "actual_worker_unknown" if actual_worker_id is None else "actual_route_not_in_candidate_set"
        return _decision(
            decision_class="insufficient_evidence",
            reason_code=reason,
            hypothetical=None,
            used_evidence=[],
            candidate_set=frozen_candidates,
            evidence=frozen_evidence,
            observed_at=now,
            config=config,
        )
    actual = actual_matches[0]

    eligible: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in frozen_candidates:
        rows = [
            row
            for row in frozen_evidence
            if _eligible_evidence(
                row,
                candidate=candidate,
                modality=modality,
                capability=requested_capability,
                observed_at=now,
                config=config,
            )
        ]
        if rows:
            eligible[(candidate["worker_id"], candidate["model"])] = max(
                rows,
                key=lambda row: (row["finalized_at"], row["group_commitment"]),
            )

    actual_evidence = eligible.get((actual["worker_id"], actual["model"]))
    if not actual_evidence:
        return _decision(
            decision_class="insufficient_evidence",
            reason_code="actual_evidence_insufficient",
            hypothetical=None,
            used_evidence=[],
            candidate_set=frozen_candidates,
            evidence=frozen_evidence,
            observed_at=now,
            config=config,
        )

    if actual_evidence["outcome"] in set(config["positive_outcomes"]):
        return _decision(
            decision_class="same",
            reason_code="actual_objectively_healthy",
            hypothetical=actual,
            used_evidence=[actual_evidence],
            candidate_set=frozen_candidates,
            evidence=frozen_evidence,
            observed_at=now,
            config=config,
        )
    if actual_evidence["outcome"] not in set(config["negative_outcomes"]):
        return _decision(
            decision_class="insufficient_evidence",
            reason_code="actual_outcome_nonnegative_or_unknown",
            hypothetical=None,
            used_evidence=[actual_evidence],
            candidate_set=frozen_candidates,
            evidence=frozen_evidence,
            observed_at=now,
            config=config,
        )

    for alternative in frozen_candidates:
        if alternative is actual:
            continue
        alt_evidence = eligible.get((alternative["worker_id"], alternative["model"]))
        if alt_evidence and alt_evidence["outcome"] in set(config["positive_outcomes"]):
            return _decision(
                decision_class="would_change",
                reason_code="actual_failed_alternative_healthy",
                hypothetical=alternative,
                used_evidence=[actual_evidence, alt_evidence],
                candidate_set=frozen_candidates,
                evidence=frozen_evidence,
                observed_at=now,
                config=config,
            )

    alternatives = [candidate for candidate in frozen_candidates if candidate is not actual]
    if alternatives and all(
        (row := eligible.get((candidate["worker_id"], candidate["model"]))) is not None
        and row["outcome"] in set(config["negative_outcomes"])
        for candidate in alternatives
    ):
        used = [actual_evidence] + [eligible[(candidate["worker_id"], candidate["model"])] for candidate in alternatives]
        return _decision(
            decision_class="would_exclude",
            reason_code="all_candidates_objectively_failed",
            hypothetical=None,
            used_evidence=used,
            candidate_set=frozen_candidates,
            evidence=frozen_evidence,
            observed_at=now,
            config=config,
        )

    return _decision(
        decision_class="insufficient_evidence",
        reason_code="no_healthy_alternative",
        hypothetical=None,
        used_evidence=[actual_evidence],
        candidate_set=frozen_candidates,
        evidence=frozen_evidence,
        observed_at=now,
        config=config,
    )


def _decision(
    *,
    decision_class: str,
    reason_code: str,
    hypothetical: Mapping[str, Any] | None,
    used_evidence: Sequence[Mapping[str, Any]],
    candidate_set: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    observed_at: datetime,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    if decision_class not in _DECISIONS:
        raise ValueError("invalid shadow decision")
    commitments = sorted({str(row["group_commitment"]) for row in used_evidence})
    operator_count = min(
        (int(row["distinct_operator_count"]) for row in used_evidence),
        default=0,
    )
    result = {
        "schema": "aipg.validator.shadow-decision.v1",
        "policy_version": config["policy_version"],
        "candidate_basis": config["candidate_basis"],
        "config_hash": commitment(config),
        "candidate_set_hash": commitment(candidate_set),
        "candidate_snapshot": list(candidate_set),
        "evidence_snapshot": list(evidence),
        "decision_class": decision_class,
        "reason_code": reason_code,
        "hypothetical_model": hypothetical["model"] if hypothetical else None,
        "hypothetical_worker_id": hypothetical["worker_id"] if hypothetical else None,
        "evidence_window_start": _iso(
            observed_at - timedelta(seconds=int(config["evidence_window_seconds"])),
        ),
        "evidence_window_end": _iso(observed_at),
        "evidence_commitments": commitments,
        "eligible_operator_count": operator_count,
        "mutation_attempted": False,
    }
    result["decision_hash"] = commitment(result)
    return result


def _require_enabled() -> None:
    if not get_settings().validator_shadow_observer_enabled:
        raise ShadowDisabled("validator shadow collection is disabled")


def _require_run_window(run: Mapping[str, Any], value: datetime, *, label: str) -> None:
    started = _aware(run["started"]) if run.get("started") else None
    scheduled_end = _aware(run["scheduled_end"]) if run.get("scheduled_end") else None
    current = _aware(value)
    if started is None or scheduled_end is None or not started <= current <= scheduled_end:
        raise ShadowError(f"{label} must fall inside the frozen shadow run window")


async def create_run(
    *,
    run_id: str,
    policy_config: Mapping[str, Any] | None,
    implementation_commit: str,
    verification_ref: str,
    verification: Mapping[str, Any],
    observed_at: datetime | None = None,
    expected_start_gate_hash: str | None = None,
) -> dict[str, Any]:
    """Create a draft from a Core-derived gate. Creation enables nothing."""
    config = runtime_policy_config(policy_config)
    commit = implementation_commit.lower().strip()
    if not _HEX_40.fullmatch(commit):
        raise ValueError("implementation_commit must be a full lowercase git SHA")
    if not run_id or len(run_id) > 96 or not verification_ref.strip():
        raise ValueError("run_id and verification_ref are required")
    gate = await live_start_gate_snapshot(
        verification=verification,
        observed_at=observed_at,
        policy_config=config,
    )
    gate_hash = commitment(gate)
    if expected_start_gate_hash is not None and expected_start_gate_hash != gate_hash:
        raise ShadowStartGateError("shadow creation gate changed; preview again")
    values = {
        "id": run_id,
        "status": "draft",
        "policy_version": POLICY_VERSION,
        "policy_config": config,
        "config_hash": commitment(config),
        "implementation_commit": commit,
        "verification_ref": verification_ref.strip(),
        "start_gate": gate,
        "start_gate_hash": gate_hash,
        "created": _now(),
        "started": None,
        "scheduled_end": None,
        "ended": None,
    }
    async with await new_session() as session:
        try:
            await session.execute(sa.insert(runs_t).values(**values))
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = (await session.execute(sa.select(runs_t).where(runs_t.c.id == run_id))).mappings().first()
            immutable_keys = (
                "policy_version",
                "policy_config",
                "config_hash",
                "implementation_commit",
                "verification_ref",
            )
            changed = not existing or any(existing[key] != values[key] for key in immutable_keys)
            if existing and existing["status"] == "draft":
                changed = changed or existing["start_gate"] != values["start_gate"]
            if changed:
                raise ShadowConflict("run id is already bound to different frozen inputs")
            return dict(existing)
    return values


async def get_run(run_id: str) -> dict[str, Any]:
    """Return one private run row for an authenticated operator tool."""
    async with await new_session() as session:
        row = (await session.execute(sa.select(runs_t).where(runs_t.c.id == run_id))).mappings().first()
    if not row:
        raise ShadowError("shadow run does not exist")
    return dict(row)


async def start_run(
    run_id: str,
    *,
    started_at: datetime | None = None,
    expected_start_gate_hash: str | None = None,
) -> dict[str, Any]:
    """Start a previously frozen run only when collection and every gate are live."""
    _require_enabled()
    started = _fresh_transition_time(started_at or _now(), label="shadow start time")
    async with await new_session() as session:
        try:
            async with session.begin():
                row = (
                    (
                        await session.execute(
                            sa.select(runs_t).where(runs_t.c.id == run_id).with_for_update(),
                        )
                    )
                    .mappings()
                    .first()
                )
                if not row:
                    raise ShadowError("shadow run does not exist")
                if row["status"] == "running":
                    return dict(row)
                if row["status"] != "draft":
                    raise ShadowConflict(f"cannot start a {row['status']} shadow run")
                if started < _aware(row["created"]):
                    raise ValueError("shadow start time cannot precede draft creation")
                other_running = await session.scalar(
                    sa.select(runs_t.c.id)
                    .where(runs_t.c.status == "running", runs_t.c.id != run_id)
                    .limit(1),
                )
                if other_running:
                    raise ShadowConflict("another shadow run is already running")
                config = runtime_policy_config(row["policy_config"])
                verification = {key: bool(row["start_gate"].get(key)) for key in _VERIFICATION_KEYS}
                live_gate = await live_start_gate_snapshot(
                    verification=verification,
                    observed_at=started,
                    policy_config=config,
                )
                evaluation = live_gate["evaluation"]
                if not evaluation.get("eligible"):
                    raise ShadowStartGateError(
                        "shadow start gate failed: " + ", ".join(evaluation.get("failed", [])),
                    )
                gate_hash = commitment(live_gate)
                if expected_start_gate_hash is not None and expected_start_gate_hash != gate_hash:
                    raise ShadowStartGateError("shadow start gate changed; preview again")
                scheduled_end = started + timedelta(hours=int(row["policy_config"]["run_hours"]))
                await session.execute(
                    sa.update(runs_t)
                    .where(runs_t.c.id == run_id, runs_t.c.status == "draft")
                    .values(
                        status="running",
                        start_gate=live_gate,
                        start_gate_hash=gate_hash,
                        started=started,
                        scheduled_end=scheduled_end,
                    ),
                )
        except IntegrityError as exc:
            raise ShadowConflict("another shadow run is already running") from exc
    return {
        **dict(row),
        "status": "running",
        "start_gate": live_gate,
        "start_gate_hash": gate_hash,
        "started": started,
        "scheduled_end": scheduled_end,
    }


async def _record_observation(
    *,
    run_id: str,
    route_ref: str,
    job_ref: str,
    task_class: str,
    modality: str,
    requested_capability: str,
    candidates: Sequence[Mapping[str, Any]],
    evidence: Iterable[Mapping[str, Any]],
    actual_model: str,
    actual_worker_id: str | None,
    observed_at: datetime,
) -> dict[str, Any]:
    """Persist a prebuilt evidence snapshot; tests and Core wrapper only."""
    _require_enabled()
    if not _HEX_64.fullmatch(route_ref):
        raise ValueError("route_ref must be a 64-character lowercase commitment")
    if not _HEX_64.fullmatch(job_ref):
        raise ValueError("job_ref must be a 64-character lowercase commitment")
    observed = _aware(observed_at)
    async with await new_session() as session:
        run = (await session.execute(sa.select(runs_t).where(runs_t.c.id == run_id))).mappings().first()
        if not run or run["status"] != "running":
            raise ShadowError("observations require a running shadow run")
        _require_run_window(run, observed, label="observation time")
        decision = evaluate_advisory(
            candidates=candidates,
            evidence=evidence,
            actual_model=actual_model,
            actual_worker_id=actual_worker_id,
            modality=modality,
            requested_capability=requested_capability,
            observed_at=observed,
            policy_config=run["policy_config"],
        )
        values = {
            "run_id": run_id,
            "route_ref": route_ref,
            "job_ref": job_ref,
            "observed_at": observed,
            "policy_version": decision["policy_version"],
            "config_hash": decision["config_hash"],
            "task_class": str(task_class)[:64],
            "modality": str(modality)[:16],
            "requested_capability": str(requested_capability)[:128],
            "candidate_set_hash": decision["candidate_set_hash"],
            "candidate_snapshot": decision["candidate_snapshot"],
            "evidence_snapshot": decision["evidence_snapshot"],
            "actual_model": str(actual_model)[:255],
            "actual_worker_id": str(actual_worker_id)[:64] if actual_worker_id else None,
            "hypothetical_model": decision["hypothetical_model"],
            "hypothetical_worker_id": decision["hypothetical_worker_id"],
            "decision_class": decision["decision_class"],
            "reason_code": decision["reason_code"],
            "evidence_window_start": datetime.fromisoformat(decision["evidence_window_start"]),
            "evidence_window_end": datetime.fromisoformat(decision["evidence_window_end"]),
            "evidence_commitments": decision["evidence_commitments"],
            "eligible_operator_count": decision["eligible_operator_count"],
            "mutation_attempted": False,
        }
        values["payload_hash"] = commitment(values)
        values["created"] = _now()
        try:
            result = await session.execute(
                sa.insert(observations_t).values(**values).returning(observations_t.c.id),
            )
            observation_id = result.scalar_one()
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = (
                (
                    await session.execute(
                        sa.select(observations_t).where(
                            observations_t.c.run_id == run_id,
                            observations_t.c.route_ref == route_ref,
                        ),
                    )
                )
                .mappings()
                .first()
            )
            if not existing or existing["payload_hash"] != values["payload_hash"]:
                raise ShadowConflict("route_ref is already bound to a different observation")
            return dict(existing)
    return {"id": observation_id, **values}


async def record_observation(
    *,
    run_id: str,
    route_ref: str,
    job_ref: str,
    task_class: str,
    modality: str,
    requested_capability: str,
    candidates: Sequence[Mapping[str, Any]],
    actual_model: str,
    actual_worker_id: str | None,
    observed_at: datetime,
) -> dict[str, Any]:
    """Derive Core-authoritative evidence, then append one comparison.

    No caller can assert that a signature, nonce, assignment, or operator
    binding is valid. The durable snapshot is reconstructed from Core's own
    finalized assignment and attestation rows immediately before persistence.
    """
    _require_enabled()
    async with await new_session() as session:
        run = (await session.execute(sa.select(runs_t).where(runs_t.c.id == run_id))).mappings().first()
    if not run or run["status"] != "running":
        raise ShadowError("observations require a running shadow run")
    evidence = await authoritative_evidence_snapshot(
        candidates=candidates,
        modality=modality,
        capability=requested_capability,
        observed_at=observed_at,
        policy_config=run["policy_config"],
    )
    return await _record_observation(
        run_id=run_id,
        route_ref=route_ref,
        job_ref=job_ref,
        task_class=task_class,
        modality=modality,
        requested_capability=requested_capability,
        candidates=candidates,
        evidence=evidence,
        actual_model=actual_model,
        actual_worker_id=actual_worker_id,
        observed_at=observed_at,
    )


async def record_outcome(
    *,
    observation_id: int,
    actual_worker_id: str | None,
    terminal_status: str,
    duration_ms: int | None,
    finished_at: datetime,
) -> dict[str, Any]:
    """Append the one terminal production outcome associated with an observation."""
    _require_enabled()
    if terminal_status not in _TERMINAL_STATUSES:
        raise ValueError("invalid terminal_status")
    if duration_ms is not None and int(duration_ms) < 0:
        raise ValueError("duration_ms may not be negative")
    finished = _aware(finished_at)
    async with await new_session() as session:
        observation = (
            (
                await session.execute(
                    sa.select(observations_t).where(observations_t.c.id == observation_id),
                )
            )
            .mappings()
            .first()
        )
        if not observation:
            raise ShadowError("shadow observation does not exist")
        if finished < _aware(observation["observed_at"]):
            raise ShadowError("terminal outcome cannot precede its observation")
        bound_worker = str(observation["actual_worker_id"] or "")
        supplied_worker = str(actual_worker_id or "")
        if bound_worker and supplied_worker and bound_worker != supplied_worker:
            raise ShadowConflict("terminal worker does not match the observed route")
        values = {
            "observation_id": int(observation_id),
            "actual_worker_id": supplied_worker[:64] or bound_worker[:64] or None,
            "terminal_status": terminal_status,
            "duration_ms": int(duration_ms) if duration_ms is not None else None,
            "finished_at": finished,
        }
        values["payload_hash"] = commitment(values)
        values["created"] = _now()
        try:
            result = await session.execute(
                sa.insert(outcomes_t).values(**values).returning(outcomes_t.c.id),
            )
            outcome_id = result.scalar_one()
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = (
                (
                    await session.execute(
                        sa.select(outcomes_t).where(outcomes_t.c.observation_id == observation_id),
                    )
                )
                .mappings()
                .first()
            )
            if not existing or existing["payload_hash"] != values["payload_hash"]:
                raise ShadowConflict("observation already has a different terminal outcome")
            return dict(existing)
    return {"id": outcome_id, **values}


async def _record_capacity_sample(
    *,
    run_id: str,
    sampled_at: datetime,
    verified_independent: int,
    participating_independent: int,
    finalized_independent_groups: int,
) -> dict[str, Any]:
    """Persist pre-derived capacity counts; tests and Core wrapper only."""
    _require_enabled()
    counts = [
        int(verified_independent),
        int(participating_independent),
        int(finalized_independent_groups),
    ]
    if any(value < 0 for value in counts):
        raise ValueError("capacity counts may not be negative")
    async with await new_session() as session:
        run = (await session.execute(sa.select(runs_t).where(runs_t.c.id == run_id))).mappings().first()
        if not run or run["status"] != "running":
            raise ShadowError("capacity samples require a running shadow run")
        _require_run_window(run, sampled_at, label="capacity sample time")
        quorum_min = int(run["policy_config"]["quorum_min"])
        values = {
            "run_id": run_id,
            "sampled_at": _aware(sampled_at),
            "verified_independent": counts[0],
            "participating_independent": counts[1],
            "finalized_independent_groups": counts[2],
            "quorum_available": counts[1] >= quorum_min and counts[2] > 0,
        }
        values["payload_hash"] = commitment(values)
        values["created"] = _now()
        try:
            result = await session.execute(
                sa.insert(capacity_t).values(**values).returning(capacity_t.c.id),
            )
            sample_id = result.scalar_one()
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = (
                (
                    await session.execute(
                        sa.select(capacity_t).where(
                            capacity_t.c.run_id == run_id,
                            capacity_t.c.sampled_at == values["sampled_at"],
                        ),
                    )
                )
                .mappings()
                .first()
            )
            if not existing or existing["payload_hash"] != values["payload_hash"]:
                raise ShadowConflict("sample timestamp is already bound to different counts")
            return dict(existing)
    return {"id": sample_id, **values}


async def record_capacity_sample(
    *,
    run_id: str,
    sampled_at: datetime,
) -> dict[str, Any]:
    """Append one Core-derived independent-capacity sample."""
    _require_enabled()
    async with await new_session() as session:
        run = (await session.execute(sa.select(runs_t).where(runs_t.c.id == run_id))).mappings().first()
    if not run or run["status"] != "running":
        raise ShadowError("capacity samples require a running shadow run")
    _require_run_window(run, sampled_at, label="capacity sample time")
    snapshot = await live_capacity_snapshot(
        observed_at=sampled_at,
        policy_config=run["policy_config"],
    )
    return await _record_capacity_sample(
        run_id=run_id,
        sampled_at=sampled_at,
        verified_independent=int(snapshot["verified_independent_operators"]),
        participating_independent=int(snapshot["participating_independent_operators"]),
        finalized_independent_groups=int(snapshot["finalized_independent_probe_groups"]),
    )


async def record_error(
    *,
    run_id: str,
    stage: str,
    error_code: str,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Append a bounded observer failure without exception text or inputs."""
    _require_enabled()
    allowed_stages = {"capture", "evidence", "policy", "persist", "outcome", "sample"}
    if stage not in allowed_stages:
        raise ValueError("invalid observer error stage")
    code = str(error_code or "").strip()
    if not code or len(code) > 64 or not re.fullmatch(r"[a-z0-9_]+", code):
        raise ValueError("error_code must be a bounded lowercase identifier")
    observed = _aware(observed_at or _now())
    values = {
        "run_id": run_id,
        "observed_at": observed,
        "stage": stage,
        "error_code": code,
        "created": _now(),
    }
    async with await new_session() as session:
        run = (await session.execute(sa.select(runs_t).where(runs_t.c.id == run_id))).mappings().first()
        if not run or run["status"] != "running":
            raise ShadowError("observer errors require a running shadow run")
        _require_run_window(run, observed, label="observer error time")
        result = await session.execute(
            sa.insert(errors_t).values(**values).returning(errors_t.c.id),
        )
        error_id = result.scalar_one()
        await session.commit()
    return {"id": error_id, **values}


async def finish_run(
    run_id: str,
    *,
    status: str,
    ended_at: datetime | None = None,
    expected_run_state_hash: str | None = None,
) -> dict[str, Any]:
    """Close a run without interpreting or promoting its observations."""
    _require_enabled()
    if status not in {"completed", "failed", "cancelled"}:
        raise ValueError("terminal run status must be completed, failed, or cancelled")
    ended = _fresh_transition_time(ended_at or _now(), label="shadow finish time")
    async with await new_session() as session:
        async with session.begin():
            row = (
                (
                    await session.execute(
                        sa.select(runs_t).where(runs_t.c.id == run_id).with_for_update(),
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise ShadowError("shadow run does not exist")
            if expected_run_state_hash is not None and expected_run_state_hash != run_state_hash(row):
                raise ShadowConflict("shadow run state changed; preview again")
            if row["status"] == status:
                return dict(row)
            if row["status"] != "running":
                raise ShadowConflict(f"cannot close a {row['status']} shadow run")
            if status == "completed" and (not row["scheduled_end"] or ended < _aware(row["scheduled_end"])):
                raise ShadowStartGateError("a shadow run cannot complete before 168 hours")
            await session.execute(
                sa.update(runs_t).where(runs_t.c.id == run_id, runs_t.c.status == "running").values(status=status, ended=ended),
            )
    return {**dict(row), "status": status, "ended": ended}


def replay_payload(row: Mapping[str, Any], policy_config: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute one stored decision from only its frozen bounded inputs."""
    replayed = evaluate_advisory(
        candidates=row["candidate_snapshot"],
        evidence=row["evidence_snapshot"],
        actual_model=row["actual_model"],
        actual_worker_id=row["actual_worker_id"],
        modality=row["modality"],
        requested_capability=row["requested_capability"],
        observed_at=_aware(row["observed_at"]),
        policy_config=policy_config,
    )
    expected = {
        "policy_version": row["policy_version"],
        "config_hash": row["config_hash"],
        "candidate_set_hash": row["candidate_set_hash"],
        "decision_class": row["decision_class"],
        "reason_code": row["reason_code"],
        "hypothetical_model": row["hypothetical_model"],
        "hypothetical_worker_id": row["hypothetical_worker_id"],
        "evidence_commitments": row["evidence_commitments"],
        "eligible_operator_count": row["eligible_operator_count"],
        "mutation_attempted": row["mutation_attempted"],
    }
    actual = {key: replayed[key] for key in expected}
    return {
        "ok": actual == expected,
        "expected": expected,
        "actual": actual,
        "decision_hash": replayed["decision_hash"],
    }


async def replay_observation(observation_id: int) -> dict[str, Any]:
    async with await new_session() as session:
        row = (
            (
                await session.execute(
                    sa.select(observations_t, runs_t.c.policy_config)
                    .join(runs_t, runs_t.c.id == observations_t.c.run_id)
                    .where(observations_t.c.id == observation_id),
                )
            )
            .mappings()
            .first()
        )
    if not row:
        raise ShadowError("shadow observation does not exist")
    return replay_payload(row, row["policy_config"])


def _max_quorum_gap(
    samples: Sequence[Mapping[str, Any]],
    *,
    start: datetime,
    end: datetime,
    sample_interval_seconds: int,
) -> float:
    if not samples:
        return max(0.0, (end - start).total_seconds())
    maximum = 0.0
    interval = float(sample_interval_seconds)
    first = _aware(samples[0]["sampled_at"])
    if (first - start).total_seconds() > interval:
        maximum = max(maximum, (first - start).total_seconds())
    gap_start: datetime | None = first if not samples[0]["quorum_available"] else None
    previous = first
    for row in samples:
        sampled = _aware(row["sampled_at"])
        if sampled != first and (sampled - previous).total_seconds() > interval:
            maximum = max(maximum, (sampled - previous).total_seconds())
        if not row["quorum_available"] and gap_start is None:
            gap_start = sampled
        elif row["quorum_available"] and gap_start is not None:
            maximum = max(maximum, (sampled - gap_start).total_seconds())
            gap_start = None
        previous = sampled
    if gap_start is not None:
        maximum = max(maximum, (end - gap_start).total_seconds())
    if (end - previous).total_seconds() > interval:
        maximum = max(maximum, (end - previous).total_seconds())
    return max(0.0, maximum)


def _capacity_coverage(
    samples: Sequence[Mapping[str, Any]],
    *,
    start: datetime,
    end: datetime,
    sample_interval_seconds: int,
) -> dict[str, Any]:
    """Count expected time slots, so sparse or bursty samples cannot inflate coverage."""
    interval = max(1, int(sample_interval_seconds))
    duration = max(0.0, (end - start).total_seconds())
    expected_slots = int(duration // interval) + 1
    slot_state: dict[int, bool] = {}
    for row in samples:
        sampled = _aware(row["sampled_at"])
        if sampled < start or sampled > end:
            continue
        slot = min(expected_slots - 1, int((sampled - start).total_seconds() // interval))
        available = bool(row["quorum_available"])
        slot_state[slot] = slot_state.get(slot, True) and available
    quorum_slots = sum(1 for available in slot_state.values() if available)
    return {
        "expected_slots": expected_slots,
        "recorded_slots": len(slot_state),
        "quorum_slots": quorum_slots,
        "coverage": quorum_slots / expected_slots if expected_slots else 0.0,
    }


async def run_report(run_id: str, *, at: datetime | None = None) -> dict[str, Any]:
    """Return a privacy-safe review report; never promote or mutate the run."""
    current = _aware(at or _now())
    async with await new_session() as session:
        run = (await session.execute(sa.select(runs_t).where(runs_t.c.id == run_id))).mappings().first()
        if not run:
            raise ShadowError("shadow run does not exist")
        started_at = _aware(run["started"]) if run["started"] else None
        bounded_end = _aware(run["ended"]) if run["ended"] else _aware(run["scheduled_end"]) if run["scheduled_end"] else current
        report_end = min(current, bounded_end)
        observations = (
            (
                await session.execute(
                    sa.select(observations_t).where(
                        observations_t.c.run_id == run_id,
                        observations_t.c.observed_at <= report_end,
                    ),
                )
            )
            .mappings()
            .all()
        )
        samples = (
            (
                await session.execute(
                    sa.select(capacity_t)
                    .where(
                        capacity_t.c.run_id == run_id,
                        capacity_t.c.sampled_at <= report_end,
                    )
                    .order_by(capacity_t.c.sampled_at),
                )
            )
            .mappings()
            .all()
        )
        outcome_rows = (
            (
                await session.execute(
                    sa.select(
                        outcomes_t.c.observation_id,
                        outcomes_t.c.terminal_status,
                        observations_t.c.job_ref,
                        observations_t.c.actual_model,
                        observations_t.c.requested_capability,
                    )
                    .select_from(outcomes_t)
                    .join(observations_t, observations_t.c.id == outcomes_t.c.observation_id)
                    .where(
                        observations_t.c.run_id == run_id,
                        outcomes_t.c.finished_at <= report_end,
                    ),
                )
            )
            .mappings()
            .all()
        )
        error_counts = (
            (
                await session.execute(
                    sa.select(
                        errors_t.c.stage,
                        errors_t.c.error_code,
                        sa.func.count().label("count"),
                    )
                    .where(
                        errors_t.c.run_id == run_id,
                        errors_t.c.observed_at <= report_end,
                    )
                    .group_by(errors_t.c.stage, errors_t.c.error_code),
                )
            )
            .mappings()
            .all()
        )
        successful_job_ids: list[str] = []
        if started_at:
            successful_job_ids = [
                str(value)
                for value in (
                    await session.execute(
                        sa.select(ledger_t.c.job_id).where(
                            ledger_t.c.created >= started_at,
                            ledger_t.c.created <= report_end,
                        ),
                    )
                ).scalars()
            ]

    decisions = {name: 0 for name in sorted(_DECISIONS)}
    reasons: dict[str, int] = {}
    route_counts: dict[tuple[str, str, str, str, str], int] = {}
    replay_failures = 0
    mutation_attempts = 0
    for row in observations:
        decisions[row["decision_class"]] += 1
        reasons[row["reason_code"]] = reasons.get(row["reason_code"], 0) + 1
        route_key = (
            str(row["actual_model"]),
            str(row["hypothetical_model"] or ""),
            str(row["requested_capability"]),
            str(row["decision_class"]),
            str(row["reason_code"]),
        )
        route_counts[route_key] = route_counts.get(route_key, 0) + 1
        mutation_attempts += int(bool(row["mutation_attempted"]))
        if not replay_payload(row, run["policy_config"])["ok"]:
            replay_failures += 1

    terminal_counts: dict[str, int] = {}
    terminal_breakdown: dict[tuple[str, str, str], int] = {}
    for row in outcome_rows:
        status = str(row["terminal_status"])
        terminal_counts[status] = terminal_counts.get(status, 0) + 1
        outcome_key = (
            str(row["actual_model"]),
            str(row["requested_capability"]),
            status,
        )
        terminal_breakdown[outcome_key] = terminal_breakdown.get(outcome_key, 0) + 1

    started = started_at
    duration = max(0.0, (report_end - started).total_seconds()) if started else 0.0
    config = run["policy_config"]
    capacity = _capacity_coverage(
        samples,
        start=started or current,
        end=report_end,
        sample_interval_seconds=int(config["sample_interval_seconds"]),
    )
    max_gap = _max_quorum_gap(
        samples,
        start=started or current,
        end=report_end,
        sample_interval_seconds=int(config["sample_interval_seconds"]),
    )
    observation_count = len(observations)
    terminal_coverage = len(outcome_rows) / observation_count if observation_count else 0.0
    captured_successes = int(terminal_counts.get("succeeded", 0))
    route_secret_value = get_settings().validator_shadow_route_hmac_secret
    route_secret = route_secret_value.get_secret_value() if route_secret_value is not None else ""
    if not route_secret:
        raise ShadowError("exact route coverage requires the shadow route HMAC secret")
    expected_success_refs = {
        committed_job_ref(job_id, secret=route_secret)
        for job_id in successful_job_ids
    }
    captured_success_refs = {
        str(row["job_ref"])
        for row in outcome_rows
        if row["terminal_status"] == "succeeded"
    }
    matched_success_refs = expected_success_refs & captured_success_refs
    unmatched_captured_refs = captured_success_refs - expected_success_refs
    successful_completions = len(expected_success_refs)
    route_capture_coverage = (
        len(matched_success_refs) / successful_completions
        if successful_completions
        else (1.0 if not captured_success_refs else 0.0)
    )
    gates = {
        "run_completed": run["status"] == "completed",
        "duration_complete": duration >= int(config["run_hours"]) * 3600,
        "observations_present": observation_count > 0,
        "terminal_outcome_coverage": terminal_coverage >= float(config["required_terminal_outcome_coverage"]),
        "route_capture_coverage": route_capture_coverage >= float(config["required_route_capture_coverage"]),
        "independent_sample_coverage": capacity["coverage"] >= float(config["required_sample_coverage"]),
        "maximum_quorum_gap": max_gap <= int(config["maximum_quorum_gap_seconds"]),
        "all_decisions_replay": replay_failures == 0,
        "zero_mutation_attempts": mutation_attempts == 0,
    }
    return {
        "schema": "aipg.validator.shadow-report.v2",
        "run_id": run_id,
        "status": run["status"],
        "policy_version": run["policy_version"],
        "candidate_basis": config["candidate_basis"],
        "counterfactual_scope": "same-model replica preference, not exact production scheduler replay",
        "config_hash": run["config_hash"],
        "implementation_commit": run["implementation_commit"],
        "start_gate_hash": run["start_gate_hash"],
        "started": _iso(started) if started else None,
        "scheduled_end": _iso(run["scheduled_end"]) if run["scheduled_end"] else None,
        "ended": _iso(run["ended"]) if run["ended"] else None,
        "observations": observation_count,
        "decisions": decisions,
        "reason_counts": dict(sorted(reasons.items())),
        "decision_rates": {name: (count / observation_count if observation_count else 0.0) for name, count in decisions.items()},
        "route_breakdown": [
            {
                "actual_model": key[0],
                "hypothetical_model": key[1] or None,
                "capability": key[2],
                "decision_class": key[3],
                "reason_code": key[4],
                "count": count,
            }
            for key, count in sorted(route_counts.items())
        ],
        "terminal_outcomes": dict(sorted(terminal_counts.items())),
        "terminal_outcome_coverage": terminal_coverage,
        "production_successful_completions": successful_completions,
        "captured_successful_routes": captured_successes,
        "captured_successful_jobs": len(captured_success_refs),
        "matched_successful_jobs": len(matched_success_refs),
        "unmatched_captured_successful_jobs": len(unmatched_captured_refs),
        "route_capture_coverage": route_capture_coverage,
        "terminal_breakdown": [
            {
                "actual_model": key[0],
                "capability": key[1],
                "terminal_status": key[2],
                "count": count,
            }
            for key, count in sorted(terminal_breakdown.items())
        ],
        "observer_errors": [
            {
                "stage": str(row["stage"]),
                "error_code": str(row["error_code"]),
                "count": int(row["count"]),
            }
            for row in error_counts
        ],
        "capacity": {
            "samples": len(samples),
            **capacity,
            "maximum_gap_seconds": max_gap,
        },
        "replay_failures": replay_failures,
        "mutation_attempts": mutation_attempts,
        "gates": gates,
        "review_eligible": all(gates.values()),
        "routing_effect": "none",
        "economic_effect": "none",
        "automatic_promotion": False,
    }


def _parse_cli_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


async def _cli(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "report":
        return await run_report(args.run_id, at=_parse_cli_time(args.at))
    with open(args.verification_json, encoding="utf-8") as handle:
        verification = json.load(handle)
    return await live_start_gate_snapshot(
        verification=verification,
        observed_at=_parse_cli_time(args.at),
    )


def main() -> None:
    """Read-only operator CLI for gate inspection and aggregate reports."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    report_parser = subparsers.add_parser("report", help="print an aggregate run report")
    report_parser.add_argument("--run-id", required=True)
    report_parser.add_argument("--at", help="optional ISO-8601 report time")
    gate_parser = subparsers.add_parser("gate", help="evaluate the current dark start gate")
    gate_parser.add_argument("--verification-json", required=True)
    gate_parser.add_argument("--at", help="optional ISO-8601 observation time")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_cli(args)), sort_keys=True, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

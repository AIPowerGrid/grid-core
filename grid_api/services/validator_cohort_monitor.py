# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only operational health for the non-economic validator cohort."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from ..database import new_session
from ..v2.schema import validator_assignments as assignments_t
from ..v2.schema import validator_attestations as attestations_t
from ..v2.schema import validators as validators_t
from . import validator_operators
from .validators import VALIDATOR_HEARTBEAT_FRESH_SECONDS

MIN_SAMPLE_SIZE = 10
MIN_COMPLETION_RATE = 0.80
MIN_EVIDENCE_RATE = 0.80
MAX_PROBE_ERROR_RATE = 0.10
MAX_DISPUTED_RATE = 0.20


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def evaluate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Classify one aggregate snapshot without exposing operator identities."""
    assignments = dict(snapshot["assignments"])
    validators = snapshot["validators"]
    network = snapshot["network"]
    issues: list[dict[str, Any]] = []

    def add(code: str, severity: str, summary: str, **fields: Any) -> None:
        issues.append(
            {
                "code": code,
                "severity": severity,
                "summary": summary,
                "fields": fields,
            },
        )

    matured = int(assignments["matured"])
    completed = int(assignments["completed"])
    terminal_failures = int(assignments["terminal_failures"])
    authoritative = int(assignments["authoritative_evidence"])
    completion_rate = _ratio(completed, matured)
    evidence_rate = _ratio(authoritative, completed)
    error_rate = _ratio(terminal_failures, matured)

    assignments.update(
        {
            "completion_rate": completion_rate,
            "evidence_rate": evidence_rate,
            "probe_error_rate": error_rate,
        },
    )

    if matured < MIN_SAMPLE_SIZE:
        add(
            "insufficient_matured_sample",
            "info",
            "The observation window does not yet contain enough expired assignments.",
            matured=matured,
            minimum=MIN_SAMPLE_SIZE,
        )
    else:
        if completion_rate is not None and completion_rate < MIN_COMPLETION_RATE:
            add(
                "low_assignment_completion",
                "warning",
                "Expired validator assignments are completing below the cohort floor.",
                completed=completed,
                matured=matured,
                rate=round(completion_rate, 4),
                minimum=MIN_COMPLETION_RATE,
            )
        if evidence_rate is not None and evidence_rate < MIN_EVIDENCE_RATE:
            add(
                "low_evidence_submission",
                "warning",
                "Completed probes are producing authoritative evidence below the cohort floor.",
                evidence=authoritative,
                completed=completed,
                rate=round(evidence_rate, 4),
                minimum=MIN_EVIDENCE_RATE,
            )
        if error_rate is not None and error_rate > MAX_PROBE_ERROR_RATE:
            add(
                "high_probe_error_rate",
                "warning",
                "Terminal probe failures exceed the cohort warning threshold.",
                terminal_failures=terminal_failures,
                matured=matured,
                rate=round(error_rate, 4),
                maximum=MAX_PROBE_ERROR_RATE,
            )

    if int(validators["stale_candidates"]):
        add(
            "candidate_heartbeat_stale",
            "critical",
            "One or more qualifying validators have lost heartbeat freshness.",
            stale_candidates=int(validators["stale_candidates"]),
            candidates=int(validators["candidates"]),
        )
    if int(validators["candidates_ready"]):
        add(
            "candidate_ready_for_review",
            "info",
            "One or more candidates have satisfied the automatic qualification gates.",
            ready=int(validators["candidates_ready"]),
            candidates=int(validators["candidates"]),
        )
    if int(validators["stale_active"]):
        add(
            "active_validators_stale",
            "warning",
            "Some active validator registrations no longer have a fresh heartbeat.",
            stale=int(validators["stale_active"]),
            active=int(validators["active"]),
        )
    if int(validators["fresh_outdated"]):
        add(
            "validator_version_drift",
            "warning",
            "Fresh validators are running outside the frozen cohort baseline.",
            outdated=int(validators["fresh_outdated"]),
            fresh=int(validators["fresh"]),
            baseline=str(snapshot["baseline_version"]),
        )
    if int(validators["duplicate_control_groups"]):
        add(
            "duplicate_control_groups",
            "info",
            "One or more reviewed control groups own multiple active registrations.",
            groups=int(validators["duplicate_control_groups"]),
        )

    disputed_rate = network.get("disputed_rate")
    if disputed_rate is not None and float(disputed_rate) > MAX_DISPUTED_RATE:
        add(
            "high_validator_disagreement",
            "warning",
            "Validator disagreement exceeds the cohort warning threshold.",
            rate=round(float(disputed_rate), 4),
            maximum=MAX_DISPUTED_RATE,
            groups=int(network["groups_with_evidence"]),
        )

    severities = {issue["severity"] for issue in issues}
    status = "critical" if "critical" in severities else "warning" if "warning" in severities else "healthy"
    return {
        **snapshot,
        "assignments": assignments,
        "status": status,
        "ok": status == "healthy",
        "issues": issues,
        "economic_effect": "none",
    }


async def inspect_cohort_health(
    *,
    window_hours: int = 24,
    baseline_version: str = "v0.1.0-preview.13",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect one privacy-safe cohort snapshot from PostgreSQL."""
    current = now or datetime.now(UTC)
    safe_window = max(1, min(int(window_hours), 24 * 30))
    cutoff = current - timedelta(hours=safe_window)
    fresh_cutoff = current - timedelta(seconds=VALIDATOR_HEARTBEAT_FRESH_SECONDS)
    matured_filter = sa.and_(
        assignments_t.c.created >= cutoff,
        assignments_t.c.expires < current,
    )

    async with await new_session() as session:
        assignment_row = (
            (
                await session.execute(
                    sa.select(
                        sa.func.count().label("matured"),
                        sa.func.count().filter(assignments_t.c.probe_status == "completed").label("completed"),
                        sa.func.count().filter(assignments_t.c.probe_status.in_(("failed", "timeout"))).label("terminal_failures"),
                    ).where(matured_filter),
                )
            )
            .mappings()
            .one()
        )
        authoritative = int(
            await session.scalar(
                sa.select(sa.func.count(sa.distinct(attestations_t.c.assignment_id)))
                .select_from(
                    attestations_t.join(
                        assignments_t,
                        assignments_t.c.id == attestations_t.c.assignment_id,
                    ),
                )
                .where(matured_filter, attestations_t.c.authority == "authoritative"),
            )
            or 0,
        )
        validator_row = (
            (
                await session.execute(
                    sa.select(
                        sa.func.count().filter(validators_t.c.status == "active").label("active"),
                        sa.func.count()
                        .filter(
                            validators_t.c.status == "active",
                            validators_t.c.last_heartbeat >= fresh_cutoff,
                        )
                        .label("fresh"),
                        sa.func.count()
                        .filter(
                            validators_t.c.status == "active",
                            validators_t.c.independence_status == "candidate",
                        )
                        .label("candidates"),
                        sa.func.count()
                        .filter(
                            validators_t.c.status == "active",
                            validators_t.c.independence_status == "candidate",
                            validators_t.c.last_heartbeat < fresh_cutoff,
                        )
                        .label("stale_candidates"),
                        sa.func.count()
                        .filter(
                            validators_t.c.status == "active",
                            validators_t.c.independence_status == "verified",
                            validators_t.c.last_heartbeat >= fresh_cutoff,
                        )
                        .label("fresh_verified"),
                        sa.func.count()
                        .filter(
                            validators_t.c.status == "active",
                            validators_t.c.last_heartbeat >= fresh_cutoff,
                            validators_t.c.software_version != baseline_version,
                        )
                        .label("fresh_outdated"),
                    ),
                )
            )
            .mappings()
            .one()
        )
        duplicate_groups = int(
            await session.scalar(
                sa.select(sa.func.count()).select_from(
                    sa.select(validators_t.c.operator_group_id)
                    .where(
                        validators_t.c.status == "active",
                        validators_t.c.operator_group_id.isnot(None),
                    )
                    .group_by(validators_t.c.operator_group_id)
                    .having(sa.func.count() > 1)
                    .subquery(),
                ),
            )
            or 0,
        )
        version_rows = (
            (
                await session.execute(
                    sa.select(validators_t.c.software_version, sa.func.count().label("count"))
                    .where(
                        validators_t.c.status == "active",
                        validators_t.c.last_heartbeat >= fresh_cutoff,
                    )
                    .group_by(validators_t.c.software_version)
                    .order_by(validators_t.c.software_version),
                )
            )
            .mappings()
            .all()
        )
        candidate_rows = (
            (
                await session.execute(
                    sa.select(validators_t).where(
                        validators_t.c.status == "active",
                        validators_t.c.independence_status == "candidate",
                    ),
                )
            )
            .mappings()
            .all()
        )
        candidates_ready = 0
        for candidate in candidate_rows:
            state = dict(candidate)
            metrics = validator_operators.qualification_metrics(state, now=current)
            activity = await validator_operators.qualification_activity(
                session,
                str(state["id"]),
                since=_aware(state["qualification_started_at"]),
            )
            last_heartbeat = _aware(state["last_heartbeat"])
            heartbeat_fresh = bool(
                last_heartbeat
                and last_heartbeat
                >= current - timedelta(seconds=validator_operators.SAMPLE_INTERVAL_SECONDS * 2),
            )
            if (
                metrics["time_ready"]
                and metrics["coverage_ready"]
                and heartbeat_fresh
                and activity["completed"] >= 1
                and activity["attested"] >= 1
            ):
                candidates_ready += 1
        vote_rows = (
            (
                await session.execute(
                    sa.select(
                        attestations_t.c.probe_group_id,
                        attestations_t.c.verdict,
                        sa.func.count().label("count"),
                    )
                    .where(
                        attestations_t.c.authority == "authoritative",
                        attestations_t.c.probe_group_id.isnot(None),
                        attestations_t.c.created >= cutoff,
                    )
                    .group_by(attestations_t.c.probe_group_id, attestations_t.c.verdict),
                )
            )
            .mappings()
            .all()
        )

    votes_by_group: dict[str, dict[str, int]] = {}
    for row in vote_rows:
        votes_by_group.setdefault(str(row["probe_group_id"]), {})[str(row["verdict"])] = int(row["count"])
    total_votes = sum(sum(votes.values()) for votes in votes_by_group.values())
    plurality_votes = sum(max(votes.values()) for votes in votes_by_group.values())
    disputed_groups = sum(1 for votes in votes_by_group.values() if len(votes) > 1)
    groups = len(votes_by_group)
    active = int(validator_row["active"] or 0)
    fresh = int(validator_row["fresh"] or 0)

    return evaluate_snapshot(
        {
            "schema": "aipg.validator.cohort-monitor.v1",
            "generated_at": current.isoformat(),
            "window_hours": safe_window,
            "baseline_version": baseline_version,
            "assignments": {
                "matured": int(assignment_row["matured"] or 0),
                "completed": int(assignment_row["completed"] or 0),
                "terminal_failures": int(assignment_row["terminal_failures"] or 0),
                "authoritative_evidence": authoritative,
            },
            "validators": {
                "active": active,
                "fresh": fresh,
                "stale_active": active - fresh,
                "candidates": int(validator_row["candidates"] or 0),
                "stale_candidates": int(validator_row["stale_candidates"] or 0),
                "candidates_ready": candidates_ready,
                "fresh_verified": int(validator_row["fresh_verified"] or 0),
                "fresh_outdated": int(validator_row["fresh_outdated"] or 0),
                "duplicate_control_groups": duplicate_groups,
                "software_versions": [{"version": str(row["software_version"]), "validators": int(row["count"])} for row in version_rows],
            },
            "network": {
                "groups_with_evidence": groups,
                "authoritative_votes": total_votes,
                "agreement_rate": _ratio(plurality_votes, total_votes),
                "disputed_groups": disputed_groups,
                "disputed_rate": _ratio(disputed_groups, groups),
            },
        },
    )

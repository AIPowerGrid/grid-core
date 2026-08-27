# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Maintainer-reviewed validator operator-independence qualification.

Operator groups are opaque internal correlation identifiers. They prevent one
organization from occupying several quorum seats, but are never exposed by the
public validator APIs. Registration alone is deliberately insufficient.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from ..database import new_session
from ..v2.schema import validator_assignments as assignments_t
from ..v2.schema import validator_attestations as attestations_t
from ..v2.schema import validators as validators_t

SAMPLE_INTERVAL_SECONDS = max(
    60,
    int(os.getenv("VALIDATOR_OPERATOR_SAMPLE_INTERVAL_SECONDS", "300") or 300),
)
MIN_QUALIFICATION_SECONDS = max(
    3600,
    int(os.getenv("VALIDATOR_OPERATOR_QUALIFICATION_SECONDS", "259200") or 259200),
)
MIN_SAMPLE_COVERAGE = min(
    1.0,
    max(0.5, float(os.getenv("VALIDATOR_OPERATOR_MIN_SAMPLE_COVERAGE", "0.80") or 0.80)),
)
DEFAULT_REVIEW_DAYS = max(
    1,
    int(os.getenv("VALIDATOR_OPERATOR_REVIEW_DAYS", "30") or 30),
)
MAX_REVIEW_DAYS = 90

GROUP_RE = re.compile(r"^opg_[A-Za-z0-9_-]{8,88}$")
_ACTIONS = {"candidate", "verify", "reject"}


class OperatorReviewError(ValueError):
    """Raised when an operator-independence transition is unsafe or stale."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _digest(row: dict[str, Any]) -> str:
    state = {
        "validator_id": row["id"],
        "status": row["status"],
        "operator_group_id": row["operator_group_id"],
        "independence_status": row["independence_status"],
        "qualification_started_at": str(row["qualification_started_at"] or ""),
        "heartbeat_sample_count": int(row["heartbeat_sample_count"] or 0),
        "last_heartbeat_sampled_at": str(row["last_heartbeat_sampled_at"] or ""),
        "independence_reviewed_at": str(row["independence_reviewed_at"] or ""),
        "independence_expires_at": str(row["independence_expires_at"] or ""),
        "independence_review_ref": row["independence_review_ref"],
    }
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def qualification_metrics(row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    started = _aware(row.get("qualification_started_at"))
    elapsed = max(0, int((current - started).total_seconds())) if started else 0
    expected = (elapsed // SAMPLE_INTERVAL_SECONDS) + 1 if started else 0
    samples = int(row.get("heartbeat_sample_count") or 0)
    coverage = min(1.0, samples / expected) if expected else 0.0
    return {
        "elapsed_seconds": elapsed,
        "minimum_seconds": MIN_QUALIFICATION_SECONDS,
        "heartbeat_samples": samples,
        "expected_samples": expected,
        "sample_coverage": coverage,
        "minimum_sample_coverage": MIN_SAMPLE_COVERAGE,
        "time_ready": elapsed >= MIN_QUALIFICATION_SECONDS,
        "coverage_ready": coverage >= MIN_SAMPLE_COVERAGE,
    }


async def _activity(session, validator_id: str) -> dict[str, int]:
    assigned = int(
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(assignments_t)
            .where(
                assignments_t.c.validator_id == validator_id,
            ),
        )
        or 0,
    )
    completed = int(
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(assignments_t)
            .where(
                assignments_t.c.validator_id == validator_id,
                assignments_t.c.probe_status == "completed",
            ),
        )
        or 0,
    )
    attested = int(
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(attestations_t)
            .where(
                attestations_t.c.validator_id == validator_id,
                attestations_t.c.authority == "authoritative",
            ),
        )
        or 0,
    )
    return {"assigned": assigned, "completed": completed, "attested": attested}


async def review_operator(
    validator_id: str,
    *,
    action: str,
    operator_group_id: str | None = None,
    review_ref: str | None = None,
    review_days: int = DEFAULT_REVIEW_DAYS,
    expected_digest: str | None = None,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Preview or atomically apply one governed independence-state transition."""
    action = action.strip().lower()
    if action not in _ACTIONS:
        raise OperatorReviewError("action must be candidate, verify, or reject")
    if operator_group_id is not None and not GROUP_RE.fullmatch(operator_group_id):
        raise OperatorReviewError("operator_group_id must be an opaque opg_* identifier")
    if not review_ref or len(review_ref) > 128:
        raise OperatorReviewError("review_ref is required and must be at most 128 characters")
    if review_days < 1 or review_days > MAX_REVIEW_DAYS:
        raise OperatorReviewError(f"review_days must be between 1 and {MAX_REVIEW_DAYS}")
    current = now or _now()

    async with await new_session() as session:
        query = sa.select(validators_t).where(validators_t.c.id == validator_id)
        if apply:
            query = query.with_for_update()
        row = (await session.execute(query)).mappings().first()
        if not row:
            raise OperatorReviewError("validator does not exist")
        state = dict(row)
        if state["status"] == "revoked":
            raise OperatorReviewError("revoked validator cannot be qualified")
        current_digest = _digest(state)
        if apply and expected_digest != current_digest:
            raise OperatorReviewError("validator review state changed; preview again")

        metrics = qualification_metrics(state, now=current)
        blocking_reasons: list[str] = []
        values: dict[str, Any]
        if action == "candidate":
            group_id = operator_group_id or state["operator_group_id"]
            if not group_id or not GROUP_RE.fullmatch(group_id):
                raise OperatorReviewError("candidate transition requires operator_group_id")
            values = {
                "operator_group_id": group_id,
                "independence_status": "candidate",
                "qualification_started_at": current,
                "heartbeat_sample_count": 0,
                "last_heartbeat_sampled_at": None,
                "independence_reviewed_at": None,
                "independence_expires_at": None,
                "independence_review_ref": review_ref,
                "updated": current,
            }
        elif action == "verify":
            if state["independence_status"] != "candidate":
                raise OperatorReviewError("only a candidate can be verified")
            if not metrics["time_ready"]:
                blocking_reasons.append("minimum qualification time has not elapsed")
            if not metrics["coverage_ready"]:
                blocking_reasons.append("heartbeat sample coverage is below minimum")
            if not _aware(state["last_heartbeat"]) or _aware(state["last_heartbeat"]) < (
                current - timedelta(seconds=SAMPLE_INTERVAL_SECONDS * 2)
            ):
                blocking_reasons.append("candidate heartbeat is not fresh")
            if apply and blocking_reasons:
                raise OperatorReviewError("; ".join(blocking_reasons))
            values = {
                "independence_status": "verified",
                "independence_reviewed_at": current,
                "independence_expires_at": current + timedelta(days=review_days),
                "independence_review_ref": review_ref,
                "updated": current,
            }
        else:
            values = {
                "independence_status": "rejected",
                "independence_reviewed_at": current,
                "independence_expires_at": None,
                "independence_review_ref": review_ref,
                "updated": current,
            }

        activity = await _activity(session, validator_id)
        result = {
            "validator_id": validator_id,
            "action": action,
            "apply": apply,
            "current_digest": current_digest,
            "current_status": state["independence_status"],
            "proposed_status": values["independence_status"],
            "operator_group_id": values.get("operator_group_id", state["operator_group_id"]),
            "qualification": metrics,
            "eligible_to_apply": not blocking_reasons,
            "blocking_reasons": blocking_reasons,
            "activity": activity,
            "review_ref": review_ref,
            "economic_effect": "none",
        }
        if apply:
            await session.execute(
                sa.update(validators_t)
                .where(
                    validators_t.c.id == validator_id,
                    validators_t.c.independence_status == state["independence_status"],
                )
                .values(**values),
            )
            await session.commit()
            result["applied_at"] = current.isoformat()
        return result

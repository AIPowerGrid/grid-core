# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Privacy-safe calibration aggregates for non-economic text probes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..v2.schema import validator_assignments as assignments_t
from .validators import _quality_eligible, _score_dimension

DEFAULT_WINDOW_HOURS = 24 * 7
MAX_WINDOW_HOURS = 24 * 90


def _json_text(column: Any, key: str) -> Any:
    return column[key].as_string()


async def inspect_text_calibration(
    session: AsyncSession,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return bounded aggregates without exposing assignments or evidence."""
    safe_window = max(1, min(int(window_hours), MAX_WINDOW_HOURS))
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(hours=safe_window)

    reason = sa.func.coalesce(
        _json_text(assignments_t.c.probe_result, "score_reason"),
        "legacy_unclassified",
    ).label("score_reason")
    finish_reason = sa.func.coalesce(
        _json_text(assignments_t.c.probe_result, "finish_reason"),
        "not_reported",
    ).label("finish_reason")
    observations = (
        (
            await session.execute(
                sa.select(
                    assignments_t.c.capability,
                    assignments_t.c.canary_kind,
                    assignments_t.c.model,
                    assignments_t.c.probe_verdict,
                    reason,
                    finish_reason,
                    sa.func.count().label("count"),
                    sa.func.avg(assignments_t.c.probe_latency_ms).label(
                        "avg_latency_ms",
                    ),
                )
                .where(
                    assignments_t.c.created >= cutoff,
                    assignments_t.c.modality == "text",
                    assignments_t.c.probe_status == "completed",
                )
                .group_by(
                    assignments_t.c.capability,
                    assignments_t.c.canary_kind,
                    assignments_t.c.model,
                    assignments_t.c.probe_verdict,
                    reason,
                    finish_reason,
                )
                .order_by(
                    assignments_t.c.canary_kind,
                    assignments_t.c.model,
                    assignments_t.c.probe_verdict,
                    reason,
                    finish_reason,
                ),
            )
        )
        .mappings()
        .all()
    )
    statuses = (
        (
            await session.execute(
                sa.select(
                    assignments_t.c.canary_kind,
                    assignments_t.c.model,
                    assignments_t.c.probe_status,
                    sa.func.count().label("count"),
                )
                .where(
                    assignments_t.c.created >= cutoff,
                    assignments_t.c.modality == "text",
                )
                .group_by(
                    assignments_t.c.canary_kind,
                    assignments_t.c.model,
                    assignments_t.c.probe_status,
                )
                .order_by(
                    assignments_t.c.canary_kind,
                    assignments_t.c.model,
                    assignments_t.c.probe_status,
                ),
            )
        )
        .mappings()
        .all()
    )

    return {
        "schema": "aipg.validator.text-calibration.v1",
        "generated_at": current.isoformat(),
        "window_hours": safe_window,
        "policy": {
            "advisory_only": True,
            "economic_effect": "none",
            "routing_effect": "none",
            "quality_authority": "none",
        },
        "observations": [
            {
                "capability": str(row["capability"]),
                "canary_kind": str(row["canary_kind"]),
                "model": str(row["model"]),
                "score_dimension": _score_dimension("text", row["capability"]),
                "quality_eligible": _quality_eligible("text", row["capability"]),
                "verdict": str(row["probe_verdict"] or "not_reported"),
                "score_reason": str(row["score_reason"]),
                "finish_reason": str(row["finish_reason"]),
                "count": int(row["count"]),
                "avg_latency_ms": (
                    round(float(row["avg_latency_ms"]), 1)
                    if row["avg_latency_ms"] is not None
                    else None
                ),
            }
            for row in observations
        ],
        "transport": [
            {
                "canary_kind": str(row["canary_kind"]),
                "model": str(row["model"]),
                "probe_status": str(row["probe_status"]),
                "count": int(row["count"]),
            }
            for row in statuses
        ],
    }

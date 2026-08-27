# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fail-closed rotating reference selection for future media validation.

This module does not dispatch probes or change worker economics. Its table is
fed only by a future finalized-block bond sync plus non-economic quality review.
Until that sync exists, the pool is empty and selection always fails closed.
"""

from __future__ import annotations

import secrets
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..v2.schema import validator_reference_workers as references_t
from ..v2.schema import worker_control_reviews as controls_t
from ..v2.schema import workers as workers_t
from .worker_control_reviews import GROUP_RE, fresh_review_reasons

VALID_MODALITIES = frozenset({"image", "video"})
DEFAULT_BOND_MAX_AGE = timedelta(minutes=30)
DEFAULT_QUALITY_MAX_AGE = timedelta(days=7)
DEFAULT_WORKER_MAX_AGE = timedelta(minutes=5)
MAX_CANDIDATE_ROWS = 64


class ReferencePoolUnavailable(RuntimeError):
    """The pool cannot provide enough fresh, independent references."""


@dataclass(frozen=True)
class SelectedReference:
    worker_id: UUID
    account_id: UUID
    payout_wallet: str
    model: str
    modality: str
    bond_finalized_block: int
    bond_verified_at: datetime
    quality_reviewed_at: datetime


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_worker_ids(values: Collection[str | UUID]) -> set[UUID]:
    result: set[UUID] = set()
    for value in values:
        try:
            result.add(value if isinstance(value, UUID) else UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
    return result


def _recency_weight(last_selected: datetime | None, *, now: datetime) -> float:
    if last_selected is None:
        return 8.0
    age = now - _aware(last_selected)
    if age >= timedelta(days=1):
        return 6.0
    if age >= timedelta(hours=6):
        return 4.0
    if age >= timedelta(hours=1):
        return 2.0
    return 1.0


def _weighted_choice(rows: list[dict], *, now: datetime) -> dict:
    weights = [_recency_weight(row["last_selected"], now=now) / max(1.0, float(row["selection_count"] or 0) ** 0.5) for row in rows]
    point = secrets.SystemRandom().uniform(0.0, sum(weights))
    for row, weight in zip(rows, weights, strict=True):
        point -= weight
        if point <= 0:
            return row
    return rows[-1]


async def select_reference_workers(
    session: AsyncSession,
    *,
    model: str,
    modality: str,
    candidate_worker_id: str | UUID,
    online_model_worker_ids: Collection[str | UUID],
    expected_chain_id: int,
    expected_bond_contract: str,
    expected_verifier_version: str,
    expected_facet_runtime_hash: str,
    minimum_bond_raw: int,
    minimum_quality_pass_rate: float,
    count: int = 2,
    now: datetime | None = None,
    bond_max_age: timedelta = DEFAULT_BOND_MAX_AGE,
    quality_max_age: timedelta = DEFAULT_QUALITY_MAX_AGE,
    worker_max_age: timedelta = DEFAULT_WORKER_MAX_AGE,
) -> list[SelectedReference]:
    """Select and reserve a rotating, independent reference set.

    The caller owns the transaction. It must persist the returned worker IDs in
    the immutable probe-group challenge before committing. PostgreSQL locks
    candidate rows so concurrent groups do not silently reuse the same pair.
    """
    normalized_model = model.strip()
    normalized_modality = modality.strip().lower()
    if not normalized_model:
        raise ValueError("model is required")
    if normalized_modality not in VALID_MODALITIES:
        raise ValueError("modality must be image or video")
    if count < 2 or count > 5:
        raise ValueError("reference count must be between 2 and 5")
    normalized_contract = expected_bond_contract.strip().lower()
    normalized_verifier = expected_verifier_version.strip()
    normalized_runtime = expected_facet_runtime_hash.strip().lower()
    if expected_chain_id <= 0:
        raise ValueError("expected_chain_id must be positive")
    if len(normalized_contract) != 42 or not normalized_contract.startswith("0x"):
        raise ValueError("expected_bond_contract must be a 20-byte address")
    try:
        int(normalized_contract[2:], 16)
    except ValueError as exc:
        raise ValueError("expected_bond_contract must be a 20-byte address") from exc
    if not normalized_verifier:
        raise ValueError("expected_verifier_version is required")
    if len(normalized_runtime) != 66 or not normalized_runtime.startswith("0x"):
        raise ValueError("expected_facet_runtime_hash must be a 32-byte hash")
    try:
        int(normalized_runtime[2:], 16)
    except ValueError as exc:
        raise ValueError("expected_facet_runtime_hash must be a 32-byte hash") from exc
    if minimum_bond_raw <= 0:
        raise ValueError("minimum_bond_raw must be positive")
    if not 0.0 <= minimum_quality_pass_rate <= 1.0:
        raise ValueError("minimum_quality_pass_rate must be between 0 and 1")
    try:
        candidate_id = candidate_worker_id if isinstance(candidate_worker_id, UUID) else UUID(str(candidate_worker_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ReferencePoolUnavailable("candidate worker is not registered") from exc

    online_ids = _normalize_worker_ids(online_model_worker_ids)
    current = _aware(now or _now())
    if candidate_id not in online_ids:
        raise ReferencePoolUnavailable("candidate worker is not online")

    candidate = (
        (
            await session.execute(
                sa.select(workers_t.c.account_id, workers_t.c.wallet).where(
                    workers_t.c.id == candidate_id,
                    workers_t.c.maintenance.is_(False),
                    workers_t.c.last_seen >= current - worker_max_age,
                ),
            )
        )
        .mappings()
        .one_or_none()
    )
    candidate_wallet = str(candidate["wallet"] or "").strip().lower() if candidate else ""
    if not candidate or candidate["account_id"] is None or not candidate_wallet:
        raise ReferencePoolUnavailable("candidate identity is incomplete")

    control_query = sa.select(controls_t).where(controls_t.c.worker_id == candidate_id)
    if session.bind and session.bind.dialect.name == "postgresql":
        # Shared locks let concurrent validator groups inspect the same target
        # while preventing a control-review rewrite until each assignment
        # transaction has committed its frozen witness set.
        control_query = control_query.with_for_update(of=controls_t, read=True)
    candidate_control = (await session.execute(control_query)).mappings().one_or_none()
    candidate_control_reasons = fresh_review_reasons(
        dict(candidate_control) if candidate_control else None,
        worker_account_id=candidate["account_id"],
        worker_wallet=candidate_wallet,
        now=current,
    )
    if candidate_control_reasons:
        raise ReferencePoolUnavailable("candidate worker control review is unavailable")
    candidate_operator_group = str(candidate_control["operator_group_id"])

    eligibility = (
        references_t.c.model == normalized_model,
        references_t.c.modality == normalized_modality,
        references_t.c.status == "active",
        references_t.c.worker_id != candidate_id,
        references_t.c.account_id != candidate["account_id"],
        sa.func.lower(references_t.c.payout_wallet) != candidate_wallet,
        references_t.c.bond_active.is_(True),
        references_t.c.bond_slashed.is_(False),
        references_t.c.bond_status_reason == "active",
        references_t.c.bond_contract.isnot(None),
        sa.func.lower(references_t.c.bond_contract) == normalized_contract,
        references_t.c.bond_chain_id == expected_chain_id,
        references_t.c.bond_finalized_block.isnot(None),
        references_t.c.bond_finalized_block_hash.isnot(None),
        references_t.c.bond_facet_address.isnot(None),
        sa.func.lower(references_t.c.bond_facet_runtime_hash) == normalized_runtime,
        references_t.c.bond_amount_raw >= minimum_bond_raw,
        references_t.c.bond_verifier_version == normalized_verifier,
        references_t.c.bond_verified_at >= current - bond_max_age,
        references_t.c.quality_window_start.isnot(None),
        references_t.c.quality_window_end.isnot(None),
        references_t.c.quality_window_start <= references_t.c.quality_window_end,
        references_t.c.quality_window_end <= current,
        references_t.c.quality_pass_rate >= minimum_quality_pass_rate,
        references_t.c.quality_reviewed_at >= current - quality_max_age,
        controls_t.c.status == "verified",
        controls_t.c.operator_group_id.isnot(None),
        controls_t.c.operator_group_id != candidate_operator_group,
        controls_t.c.reviewed_at <= current,
        controls_t.c.expires_at >= current,
        controls_t.c.account_id == references_t.c.account_id,
        sa.func.lower(controls_t.c.payout_wallet) == sa.func.lower(references_t.c.payout_wallet),
        workers_t.c.account_id == references_t.c.account_id,
        sa.func.lower(workers_t.c.wallet) == sa.func.lower(references_t.c.payout_wallet),
        workers_t.c.maintenance.is_(False),
        workers_t.c.last_seen >= current - worker_max_age,
        references_t.c.worker_id.in_(online_ids),
    )
    query = (
        sa.select(references_t, controls_t.c.operator_group_id)
        .join(workers_t, workers_t.c.id == references_t.c.worker_id)
        .join(controls_t, controls_t.c.worker_id == references_t.c.worker_id)
        .where(*eligibility)
        .order_by(
            references_t.c.last_selected.asc().nullsfirst(),
            references_t.c.selection_count.asc(),
            references_t.c.worker_id.asc(),
        )
        .limit(MAX_CANDIDATE_ROWS)
    )
    rows = [
        dict(row)
        for row in (await session.execute(query)).mappings().all()
        if GROUP_RE.fullmatch(str(row["operator_group_id"] or ""))
    ]

    selected: list[dict] = []
    remaining = rows
    while remaining and len(selected) < count:
        allowed = [
            row
            for row in remaining
            if all(
                row["account_id"] != picked["account_id"] and str(row["payout_wallet"]).lower() != str(picked["payout_wallet"]).lower()
                and row["operator_group_id"] != picked["operator_group_id"]
                for picked in selected
            )
        ]
        if not allowed:
            break
        candidate_row = _weighted_choice(allowed, now=current)
        locked = (
            (
                await session.execute(
                    sa.select(references_t, controls_t.c.operator_group_id)
                    .join(workers_t, workers_t.c.id == references_t.c.worker_id)
                    .join(controls_t, controls_t.c.worker_id == references_t.c.worker_id)
                    .where(
                        *eligibility,
                        references_t.c.worker_id == candidate_row["worker_id"],
                    )
                    .with_for_update(of=[references_t, controls_t], skip_locked=True),
                )
            )
            .mappings()
            .one_or_none()
        )
        remaining = [row for row in remaining if row["worker_id"] != candidate_row["worker_id"]]
        if locked is None:
            continue
        locked_row = dict(locked)
        if not GROUP_RE.fullmatch(str(locked_row["operator_group_id"] or "")):
            continue
        selected.append(locked_row)

    if len(selected) != count:
        raise ReferencePoolUnavailable(
            f"need {count} fresh independent references; found {len(selected)}",
        )

    selected_ids = [row["worker_id"] for row in selected]
    await session.execute(
        sa.update(references_t)
        .where(
            references_t.c.worker_id.in_(selected_ids),
            references_t.c.model == normalized_model,
            references_t.c.modality == normalized_modality,
        )
        .values(
            last_selected=current,
            selection_count=references_t.c.selection_count + 1,
            updated=current,
        ),
    )
    return [
        SelectedReference(
            worker_id=row["worker_id"],
            account_id=row["account_id"],
            payout_wallet=str(row["payout_wallet"]).lower(),
            model=normalized_model,
            modality=normalized_modality,
            bond_finalized_block=int(row["bond_finalized_block"]),
            bond_verified_at=_aware(row["bond_verified_at"]),
            quality_reviewed_at=_aware(row["quality_reviewed_at"]),
        )
        for row in selected
    ]

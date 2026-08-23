# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded worker compensation for assignment-bound validator text audits.

This rail pays workers, not validators. It deliberately has no routing,
reputation, strike, slash, or validator-reward side effect. A per-day row is
locked before every reserve/settle/release so concurrent validators cannot
overspend the network audit budget.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..database import new_session
from ..safe_logging import error_type, opaque_id
from ..v2.schema import validator_audit_budgets as budgets_t
from ..v2.schema import validator_audit_reservations as reservations_t
from . import ledger as ledger_svc

logger = logging.getLogger("grid_api.validator_audits")

_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_DEN_QUANTUM = Decimal("0.00000001")


class AuditBudgetError(RuntimeError):
    """Paid audit cannot proceed without violating a configured invariant."""


def _den(value: Any) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(_DEN_QUANTUM, rounding=ROUND_DOWN)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AuditBudgetError("validator audit den configuration is invalid") from exc
    if not result.is_finite():
        raise AuditBudgetError("validator audit den configuration is invalid")
    return result


def policy() -> dict[str, Any]:
    """Return a public, secret-free paid-audit readiness contract."""
    settings = get_settings()
    requested = bool(settings.validator_paid_audit_enabled)
    wallets = {
        value.strip().lower()
        for value in settings.validator_paid_audit_wallets.split(",")
        if value.strip()
    }
    reasons: list[str] = []
    if requested and (not wallets or any(not _ADDRESS_RE.fullmatch(w) for w in wallets)):
        reasons.append("reviewed validator wallet allowlist is missing or invalid")
    try:
        daily = _den(settings.validator_paid_audit_daily_den)
        per_job = _den(settings.validator_paid_audit_max_den_per_job)
    except AuditBudgetError:
        daily = Decimal(0)
        per_job = Decimal(0)
        if requested:
            reasons.append("audit den budget is invalid")
    if requested and daily <= 0:
        reasons.append("daily audit den budget must be positive")
    if requested and per_job <= 0:
        reasons.append("per-job audit den cap must be positive")
    if requested and daily > 0 and per_job > daily:
        reasons.append("per-job audit den cap exceeds daily budget")
    if requested and int(settings.validator_paid_audit_stale_seconds) < 300:
        reasons.append("audit reservation stale threshold must be at least 300 seconds")
    return {
        "requested": requested,
        "enabled": requested and not reasons,
        "reasons": reasons,
        "reviewed_wallet_count": len(wallets),
        "daily_den": float(daily),
        "max_den_per_job": float(per_job),
        "worker_compensation": "audit_budget" if requested and not reasons else "none",
        "validator_rewards": False,
        "evidence_economic_authority": False,
        "_wallets": wallets,
        "_daily_decimal": daily,
        "_per_job_decimal": per_job,
    }


def public_policy() -> dict[str, Any]:
    return {key: value for key, value in policy().items() if not key.startswith("_")}


def assignment_compensation(validator_wallet: str | None) -> str:
    """Return the snapshotted compensation mode or fail closed in paid mode."""
    current = policy()
    if not current["requested"]:
        return "none"
    if not current["enabled"]:
        raise AuditBudgetError("paid validator audit mode is not fully configured")
    wallet = str(validator_wallet or "").strip().lower()
    if wallet not in current["_wallets"]:
        raise AuditBudgetError("validator is not approved for paid audit assignments")
    return "audit_budget"


async def _lock_budget_day(session, budget_day: date) -> None:
    if session.bind and session.bind.dialect.name == "postgresql":
        lock_key = int.from_bytes(
            hashlib.sha256(f"validator-audit-budget:{budget_day.isoformat()}".encode()).digest()[:8],
            byteorder="big",
            signed=True,
        )
        await session.execute(
            sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )


async def reserve(
    *,
    job_id: str,
    assignment_id: str,
    probe_group_id: str,
    worker_id: str,
    validator_wallet: str,
) -> str:
    """Reserve the configured per-job cap before GPU dispatch.

    Returns ``held`` (including an idempotent retry). Raises AuditBudgetError when paid mode,
    allowlist, or budget constraints are not satisfied.
    """
    current = policy()
    if not current["enabled"]:
        raise AuditBudgetError("paid validator audit mode is not ready")
    wallet = str(validator_wallet or "").strip().lower()
    if wallet not in current["_wallets"]:
        raise AuditBudgetError("validator is not approved for paid audit assignments")
    today = datetime.now(UTC).date()
    reserve_den = current["_per_job_decimal"]
    daily_den = current["_daily_decimal"]

    async with await new_session() as session:
        try:
            await _lock_budget_day(session, today)
            existing = (
                await session.execute(
                    sa.select(reservations_t.c.status).where(
                        reservations_t.c.job_id == ledger_svc.as_uuid(job_id),
                    ),
                )
            ).scalar_one_or_none()
            if existing is not None:
                await session.rollback()
                if existing == "held":
                    return "held"
                raise AuditBudgetError(
                    "validator audit reservation is already terminal for this job",
                )

            budget = (
                await session.execute(
                    sa.select(budgets_t).where(budgets_t.c.budget_day == today).with_for_update(),
                )
            ).mappings().first()
            now = datetime.now(UTC)
            if budget is None:
                await session.execute(
                    sa.insert(budgets_t).values(
                        budget_day=today,
                        limit_den=daily_den,
                        held_den=Decimal(0),
                        spent_den=Decimal(0),
                        created=now,
                        updated=now,
                    ),
                )
                budget_limit = daily_den
            else:
                # The first reserve snapshots the day's operator-approved cap.
                # Mid-day env changes apply on the next UTC day.
                budget_limit = _den(budget["limit_den"])

            moved = await session.execute(
                sa.update(budgets_t)
                .where(
                    budgets_t.c.budget_day == today,
                    budgets_t.c.held_den + budgets_t.c.spent_den + reserve_den
                    <= budget_limit,
                )
                .values(
                    held_den=budgets_t.c.held_den + reserve_den,
                    updated=now,
                ),
            )
            if moved.rowcount != 1:
                await session.rollback()
                raise AuditBudgetError("validator audit daily den budget exhausted")
            await session.execute(
                sa.insert(reservations_t).values(
                    job_id=ledger_svc.as_uuid(job_id),
                    assignment_id=assignment_id,
                    probe_group_id=probe_group_id,
                    worker_id=ledger_svc.as_uuid(worker_id),
                    validator_wallet=wallet,
                    budget_day=today,
                    reserved_den=reserve_den,
                    settled_den=None,
                    status="held",
                    created=now,
                    updated=now,
                ),
            )
            await session.commit()
            return "held"
        except AuditBudgetError:
            raise
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                sa.select(reservations_t.c.status).where(
                    reservations_t.c.job_id == ledger_svc.as_uuid(job_id),
                ),
            )
            if existing is not None:
                if existing == "held":
                    return "held"
                raise AuditBudgetError(
                    "validator audit reservation is already terminal for this job",
                )
            raise AuditBudgetError("validator audit budget reservation conflicted")


async def record_and_settle(*, job_id: str, ledger_values: dict[str, Any]) -> tuple[str, float]:
    """Atomically append the payout ledger row and settle its audit hold."""
    try:
        if str(ledger_values.get("job_id") or "") != str(job_id):
            raise AuditBudgetError("audit ledger job does not match its reservation")
        if ledger_values.get("job_type") != "text":
            raise AuditBudgetError("audit ledger job type must be text")
        async with await new_session() as session:
            reservation = (
                await session.execute(
                    sa.select(reservations_t)
                    .where(reservations_t.c.job_id == ledger_svc.as_uuid(job_id))
                    .with_for_update(),
                )
            ).mappings().first()
            if not reservation:
                await session.rollback()
                return "no_reservation", 0.0
            if reservation["status"] == "released":
                await session.rollback()
                return "stale_no_payout", 0.0
            if reservation["status"] == "settled":
                await session.rollback()
                return "duplicate", float(reservation["settled_den"] or 0)
            if ledger_svc.as_uuid(ledger_values.get("worker_id")) != reservation["worker_id"]:
                await session.rollback()
                return "worker_mismatch", 0.0

            actual_den = min(
                max(_den(ledger_values.get("den", 0)), Decimal(0)),
                _den(reservation["reserved_den"]),
            )
            ledger_values = {**ledger_values, "den": float(actual_den)}
            # The locked reservation serializes genuine duplicate terminals: a
            # later caller observes status=settled above. Do not turn an
            # unrelated ledger constraint failure into a false "duplicate" ACK.
            await ledger_svc.record_completion_in_session(session, **ledger_values)

            now = datetime.now(UTC)
            moved = await session.execute(
                sa.update(reservations_t)
                .where(
                    reservations_t.c.job_id == ledger_svc.as_uuid(job_id),
                    reservations_t.c.status == "held",
                )
                .values(status="settled", settled_den=actual_den, updated=now),
            )
            if moved.rowcount != 1:
                await session.rollback()
                return "stale_no_payout", 0.0
            budget_moved = await session.execute(
                sa.update(budgets_t)
                .where(
                    budgets_t.c.budget_day == reservation["budget_day"],
                    budgets_t.c.held_den >= reservation["reserved_den"],
                )
                .values(
                    held_den=budgets_t.c.held_den - reservation["reserved_den"],
                    spent_den=budgets_t.c.spent_den + actual_den,
                    updated=now,
                ),
            )
            if budget_moved.rowcount != 1:
                await session.rollback()
                return "error", 0.0
            await session.commit()
            return "settled", float(actual_den)
    except Exception as exc:
        logger.error(
            "validator audit settlement failed job=%s error_type=%s",
            opaque_id(job_id),
            error_type(exc),
        )
        return "error", 0.0


async def release(job_id: str) -> str:
    """Release one unspent hold. Settled/released rows are strict no-ops."""
    async with await new_session() as session:
        reservation = (
            await session.execute(
                sa.select(reservations_t)
                .where(reservations_t.c.job_id == ledger_svc.as_uuid(job_id))
                .with_for_update(),
            )
        ).mappings().first()
        if not reservation:
            await session.rollback()
            return "missing"
        if reservation["status"] != "held":
            await session.rollback()
            return str(reservation["status"])
        now = datetime.now(UTC)
        moved = await session.execute(
            sa.update(reservations_t)
            .where(
                reservations_t.c.job_id == ledger_svc.as_uuid(job_id),
                reservations_t.c.status == "held",
            )
            .values(status="released", settled_den=Decimal(0), updated=now),
        )
        budget_moved = await session.execute(
            sa.update(budgets_t)
            .where(
                budgets_t.c.budget_day == reservation["budget_day"],
                budgets_t.c.held_den >= reservation["reserved_den"],
            )
            .values(
                held_den=budgets_t.c.held_den - reservation["reserved_den"],
                updated=now,
            ),
        )
        if moved.rowcount != 1 or budget_moved.rowcount != 1:
            await session.rollback()
            raise AuditBudgetError("validator audit release invariant failed")
        await session.commit()
        return "released"


async def sweep_stale(*, older_than_seconds: int | None = None) -> int:
    """Release crash-orphaned holds; a later result cannot receive payout."""
    stale_seconds = int(
        older_than_seconds
        if older_than_seconds is not None
        else get_settings().validator_paid_audit_stale_seconds,
    )
    cutoff = datetime.now(UTC) - timedelta(seconds=max(300, stale_seconds))
    async with await new_session() as session:
        job_ids = (
            await session.execute(
                sa.select(reservations_t.c.job_id).where(
                    reservations_t.c.status == "held",
                    reservations_t.c.created < cutoff,
                ),
            )
        ).scalars().all()
    released = 0
    for job_id in job_ids:
        if await release(str(job_id)) == "released":
            released += 1
    return released


async def snapshot(budget_day: date | None = None) -> dict[str, Any] | None:
    """Operator/test visibility for one UTC audit budget day."""
    selected_day = budget_day or datetime.now(UTC).date()
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(budgets_t).where(budgets_t.c.budget_day == selected_day),
            )
        ).mappings().first()
    if not row:
        return None
    return {
        "budget_day": row["budget_day"].isoformat(),
        "limit_den": float(row["limit_den"]),
        "held_den": float(row["held_den"]),
        "spent_den": float(row["spent_den"]),
        "remaining_den": float(row["limit_den"] - row["held_den"] - row["spent_den"]),
    }

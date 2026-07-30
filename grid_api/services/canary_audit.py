# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only reconciliation for a supervised demand-billing canary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import sqlalchemy as sa

from ..v2.schema import account_identities
from ..v2.schema import accounts as accounts_t
from ..v2.schema import credit_ledger as credit_ledger_t
from ..v2.schema import credits as credits_t
from ..v2.schema import ledger as worker_ledger_t
from ..v2.schema import reservations as reservations_t
from .identities import account_family_ids, canonical_account_id

CanaryOutcome = Literal["success", "failure", "absent"]


@dataclass(frozen=True)
class JobExpectation:
    job_id: UUID
    outcome: CanaryOutcome


def _finding(findings: list[dict], code: str, scope: str, detail: str) -> None:
    findings.append({"code": code, "scope": scope, "detail": detail})


def _iso(value) -> str | None:
    return value.isoformat() if value else None


async def _global_invariants(session, stale_seconds: int) -> dict:
    cutoff = datetime.now(UTC) - timedelta(seconds=max(stale_seconds, 0))
    balance_total = int(
        await session.scalar(sa.select(sa.func.coalesce(sa.func.sum(credits_t.c.balance_micro), 0))) or 0,
    )
    ledger_total = int(
        await session.scalar(sa.select(sa.func.coalesce(sa.func.sum(credit_ledger_t.c.delta_micro), 0))) or 0,
    )
    negative_balances = int(
        await session.scalar(
            sa.select(sa.func.count()).select_from(credits_t).where(credits_t.c.balance_micro < 0),
        )
        or 0,
    )
    stale_held = int(
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(reservations_t)
            .where(
                reservations_t.c.status == "held",
                reservations_t.c.created < cutoff,
            ),
        )
        or 0,
    )
    invalid_splits = int(
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(reservations_t)
            .where(
                reservations_t.c.free_micro + reservations_t.c.promo_micro
                > reservations_t.c.reserved_micro,
            ),
        )
        or 0,
    )
    return {
        "balance_total_micro": balance_total,
        "ledger_total_micro": ledger_total,
        "balance_delta_micro": balance_total - ledger_total,
        "negative_balances": negative_balances,
        "stale_held": stale_held,
        "invalid_reservation_splits": invalid_splits,
    }


async def _audit_job(
    session,
    expectation: JobExpectation,
    *,
    canonical_id: UUID,
    allowed_services: frozenset[str],
    findings: list[dict],
) -> dict:
    job_id = str(expectation.job_id)
    scope = f"job:{job_id}"
    reservation = (
        await session.execute(
            sa.select(reservations_t).where(reservations_t.c.job_id == job_id),
        )
    ).mappings().first()
    completions = (
        await session.execute(
            sa.select(worker_ledger_t).where(worker_ledger_t.c.job_id == expectation.job_id),
        )
    ).mappings().all()
    completion = completions[0] if completions else None
    if len(completions) > 1:
        _finding(findings, "duplicate_worker_completion", scope, "job has more than one worker ledger row")
    refs = (job_id, f"{job_id}:refund", f"{job_id}:extra")
    movements = (
        await session.execute(
            sa.select(
                credit_ledger_t.c.account_id,
                credit_ledger_t.c.delta_micro,
                credit_ledger_t.c.reason,
                credit_ledger_t.c.ref,
            )
            .where(credit_ledger_t.c.ref.in_(refs))
            .order_by(credit_ledger_t.c.id),
        )
    ).mappings().all()

    if expectation.outcome == "absent":
        if reservation:
            _finding(findings, "unexpected_reservation", scope, "a rejected request created a reservation")
        if completion:
            _finding(findings, "unexpected_worker_completion", scope, "a rejected request paid a worker")
        if movements:
            _finding(findings, "unexpected_credit_movement", scope, "a rejected request moved purchased credit")
        return {
            "job_id": job_id,
            "expected": expectation.outcome,
            "reservation": None,
            "worker_completion": bool(completion),
            "purchased_delta_micro": sum(int(row["delta_micro"]) for row in movements),
        }

    if not reservation:
        _finding(findings, "missing_reservation", scope, "a terminal canary job has no durable reservation")
        if expectation.outcome == "success" and not completion:
            _finding(findings, "missing_worker_completion", scope, "successful work has no worker ledger row")
        if expectation.outcome == "failure" and completion:
            _finding(findings, "worker_paid_on_failure", scope, "released work has a worker completion ledger row")
        return {
            "job_id": job_id,
            "expected": expectation.outcome,
            "reservation": None,
            "worker_completion": bool(completion),
            "purchased_delta_micro": sum(int(row["delta_micro"]) for row in movements),
        }

    row_account = reservation["account_id"]
    if row_account is None:
        _finding(findings, "missing_reservation_account", scope, "a live reservation has no account")
    else:
        row_canonical = await canonical_account_id(row_account, session=session)
        if row_canonical != canonical_id:
            _finding(findings, "wrong_reservation_account", scope, "reservation belongs to another canonical account")
    if reservation["billing_source"] != "credits":
        _finding(findings, "wrong_billing_source", scope, "the prepaid-credit canary used another billing rail")
    if allowed_services and (reservation["service_id"] or "") not in allowed_services:
        _finding(findings, "unexpected_service", scope, "reservation did not come from an approved first-party service")
    if reservation["status"] != "settled":
        _finding(findings, "terminal_not_settled", scope, f"reservation status is {reservation['status']!r}")
    if reservation["settled"] is None:
        _finding(findings, "missing_settled_time", scope, "terminal reservation has no settled timestamp")

    reserved = int(reservation["reserved_micro"] or 0)
    promo = int(reservation["promo_micro"] or 0)
    free = int(reservation["free_micro"] or 0)
    paid_held = reserved - promo - free
    if paid_held < 0:
        _finding(findings, "invalid_reservation_split", scope, "promo plus daily credit exceeds the total hold")

    movement_total = sum(int(row["delta_micro"]) for row in movements)
    reserve_rows = [row for row in movements if row["ref"] == job_id]
    if paid_held > 0:
        if len(reserve_rows) != 1 or int(reserve_rows[0]["delta_micro"]) != -paid_held:
            _finding(findings, "bad_reserve_movement", scope, "purchased-credit reserve does not match the paid hold")
    elif reserve_rows:
        _finding(findings, "unexpected_paid_reserve", scope, "a free-only hold moved purchased credit")
    for movement in movements:
        if await canonical_account_id(movement["account_id"], session=session) != canonical_id:
            _finding(findings, "wrong_credit_account", scope, "a canary credit movement belongs to another account")

    actual = reservation["actual_micro"]
    if expectation.outcome == "success":
        if not completion:
            _finding(findings, "missing_worker_completion", scope, "successful work has no worker ledger row")
        else:
            if completion["model"] != reservation["model"]:
                _finding(findings, "model_mismatch", scope, "reservation and worker ledger name different models")
            if not completion["prompt_hash"]:
                _finding(findings, "missing_prompt_hash", scope, "successful work has no prompt commitment")
            if not completion["result_hash"]:
                _finding(findings, "missing_result_hash", scope, "successful work has no result commitment")
        if actual is None:
            _finding(findings, "missing_actual_charge", scope, "successful work has no final grid-counted charge")
            expected_paid_delta = None
        else:
            expected_paid_delta = -max(int(actual) - promo - free, 0)
            if movement_total != expected_paid_delta:
                _finding(
                    findings,
                    "bad_success_movement",
                    scope,
                    "net purchased-credit movement does not match the final charge split",
                )
    else:
        if completion:
            _finding(findings, "worker_paid_on_failure", scope, "released work has a worker completion ledger row")
        if actual not in (None, 0):
            _finding(findings, "charged_failed_job", scope, "released work records a non-zero actual charge")
        expected_paid_delta = 0
        if movement_total != 0:
            _finding(findings, "bad_release_movement", scope, "released work did not fully restore purchased credit")

    return {
        "job_id": job_id,
        "expected": expectation.outcome,
        "model": reservation["model"],
        "service_id": reservation["service_id"],
        "reservation": {
            "status": reservation["status"],
            "reserved_micro": reserved,
            "promo_micro": promo,
            "free_micro": free,
            "actual_micro": int(actual) if actual is not None else None,
            "created": _iso(reservation["created"]),
            "settled": _iso(reservation["settled"]),
        },
        "worker_completion": bool(completion),
        "worker_job_type": completion["job_type"] if completion else None,
        "worker_output_units": int(completion["output_units"] or 0) if completion else None,
        "purchased_delta_micro": movement_total,
        "expected_purchased_delta_micro": expected_paid_delta,
        "credit_refs": [
            {
                "ref": row["ref"],
                "delta_micro": int(row["delta_micro"]),
                "reason": row["reason"],
            }
            for row in movements
        ],
    }


async def audit_demand_canary(
    session,
    account_id: UUID,
    expectations: list[JobExpectation],
    *,
    stale_seconds: int = 900,
    allowed_services: frozenset[str] = frozenset(),
) -> dict:
    """Reconcile one canonical account and its supervised canary jobs."""
    findings: list[dict] = []
    canonical_id = await canonical_account_id(account_id, session=session)
    account_exists = bool(
        await session.scalar(sa.select(accounts_t.c.id).where(accounts_t.c.id == canonical_id)),
    )
    if not account_exists:
        _finding(findings, "account_not_found", "account", "canonical account does not exist")
    if canonical_id != account_id:
        _finding(findings, "account_not_canonical", "account", "input account is a retired alias")

    balance = int(
        await session.scalar(
            sa.select(credits_t.c.balance_micro).where(credits_t.c.account_id == canonical_id),
        )
        or 0,
    )
    account_ledger = int(
        await session.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(credit_ledger_t.c.delta_micro), 0))
            .where(credit_ledger_t.c.account_id == canonical_id),
        )
        or 0,
    )
    if balance != account_ledger:
        _finding(findings, "account_ledger_drift", "account", "purchased balance differs from its append-only ledger")
    if balance < 0:
        _finding(findings, "negative_account_balance", "account", "canary purchased balance is negative")

    identity_rows = (
        await session.execute(
            sa.select(account_identities.c.kind, sa.func.count())
            .where(
                account_identities.c.account_id == canonical_id,
                account_identities.c.verified_at.is_not(None),
            )
            .group_by(account_identities.c.kind),
        )
    ).all()
    family = await account_family_ids(canonical_id, session=session)
    global_state = await _global_invariants(session, stale_seconds)
    if global_state["balance_delta_micro"] != 0:
        _finding(findings, "global_ledger_drift", "global", "purchased balance cache and ledger totals differ")
    if global_state["negative_balances"]:
        _finding(findings, "global_negative_balance", "global", "one or more purchased balances are negative")
    if global_state["stale_held"]:
        _finding(findings, "stale_reservations", "global", "one or more monetary holds exceeded the warning age")
    if global_state["invalid_reservation_splits"]:
        _finding(findings, "invalid_reservation_splits", "global", "one or more holds overdraw free/promo pockets")

    jobs = [
        await _audit_job(
            session,
            expectation,
            canonical_id=canonical_id,
            allowed_services=allowed_services,
            findings=findings,
        )
        for expectation in expectations
    ]
    return {
        "ok": not findings,
        "checked_at": datetime.now(UTC).isoformat(),
        "account": {
            "input_id": str(account_id),
            "canonical_id": str(canonical_id),
            "exists": account_exists,
            "family_size": len(family),
            "verified_identities": {kind: int(count) for kind, count in identity_rows},
            "balance_micro": balance,
            "ledger_micro": account_ledger,
            "delta_micro": balance - account_ledger,
        },
        "global": global_state,
        "jobs": jobs,
        "findings": findings,
    }

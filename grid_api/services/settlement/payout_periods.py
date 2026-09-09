# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Freeze one bounded, prospective UTC-hour allocation before any broadcast."""

import datetime as dt
import hashlib
import json
import math
import re
import uuid
from decimal import Decimal

import sqlalchemy as sa

from ...database import new_session
from ...v2.schema import payout_periods, payouts

_PLAN_LOCK = 9123848
_QUANTUM = Decimal("0.00000001")


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or result > Decimal("999999999999999999999999999999"):
        raise ValueError("invalid payout amount")
    if result != result.quantize(_QUANTUM):
        raise ValueError("payout amount exceeds eight decimal places")
    return result


def wallet(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError("invalid payout destination")
    if int(value[2:], 16) == 0:
        raise ValueError("zero payout destination")
    return value.lower()


def _utc(value):
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("payout times must be timezone-aware")
    return value.astimezone(dt.UTC)


def contract(start, end, budget, period_id, *, cutoff, hourly_cap, token, now):
    start, end, cutoff, now = map(_utc, (start, end, cutoff, now))
    if start.minute or start.second or start.microsecond or end - start != dt.timedelta(hours=1):
        raise ValueError("payouts require exactly one UTC hour")
    if start < cutoff or end > now:
        raise ValueError("payout hour must be closed and after the reward cutoff")
    if period_id != start.strftime("hour-%Y-%m-%dT%H"):
        raise ValueError("payout period id must match its UTC hour")
    budget, cap = amount(budget), amount(hourly_cap)
    if not 0 < cap or budget > cap:
        raise ValueError("payout exceeds configured hourly budget")
    return {
        "version": 1,
        "utc_hour": int(start.timestamp()) // 3600,
        "budget_aipg": format(budget, ".8f"),
        "reward_cutoff": cutoff.isoformat(),
        "token": wallet(token),
        "chain_id": 8453,
    }


def _hash(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _allocations(rows, budget):
    out, seen = [], set()
    for row in rows:
        account_id = str(uuid.UUID(str(row["account_id"])))
        den = float(row["den"])
        value = amount(row["aipg"])
        if account_id in seen or not math.isfinite(den) or den < 0 or value <= 0:
            raise ValueError("invalid or duplicate payout allocation")
        seen.add(account_id)
        out.append({"account_id": account_id, "den": den, "aipg": str(value), "address": wallet(row["payout_address"])})
    if sum((amount(row["aipg"]) for row in out), Decimal(0)) > amount(budget):
        raise ValueError("payout allocations exceed period budget")
    return sorted(out, key=lambda row: row["account_id"])


async def freeze(period_id, expected, build_allocations):
    """Commit the plan and EVERY allocation together. Replays never aggregate."""
    async with await new_session() as session, session.begin():
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": _PLAN_LOCK})
        elif session.get_bind().dialect.name != "sqlite":
            raise RuntimeError("payout plans require PostgreSQL")
        existing = (
            (
                await session.execute(
                    sa.select(payout_periods).where(
                        sa.or_(payout_periods.c.period_id == period_id, payout_periods.c.utc_hour == expected["utc_hour"]),
                    ),
                )
            )
            .mappings()
            .first()
        )
        if existing:
            plan = existing["plan"]
            if existing["period_id"] != period_id or _hash(plan) != existing["plan_hash"] or plan["contract"] != expected:
                raise ValueError("payout period conflicts with frozen plan")
            return plan
        if (await session.execute(sa.select(payouts.c.id).where(payouts.c.period_id == period_id).limit(1))).first():
            raise ValueError("legacy payout period requires separate reconciliation")
        rows = _allocations(await build_allocations(), expected["budget_aipg"])
        plan = {"contract": expected, "allocations": rows}
        now = dt.datetime.now(dt.UTC)
        await session.execute(
            payout_periods.insert().values(
                period_id=period_id,
                utc_hour=expected["utc_hour"],
                budget_aipg=amount(expected["budget_aipg"]),
                plan=plan,
                plan_hash=_hash(plan),
                created=now,
            ),
        )
        if rows:
            await session.execute(
                payouts.insert(),
                [
                    dict(
                        period_id=period_id,
                        account_id=uuid.UUID(row["account_id"]),
                        address=row["address"],
                        den=row["den"],
                        aipg_amount=amount(row["aipg"]),
                        status="pending" if row["address"] else "accrued",
                        created=now,
                    )
                    for row in rows
                ],
            )
        return plan


async def load_rows(period_id, validate_contract):
    """Verify every persisted allocation before allowing any sends for a plan."""
    async with await new_session() as session:
        header = (
            (
                await session.execute(
                    sa.select(payout_periods).where(
                        payout_periods.c.period_id == period_id,
                    ),
                )
            )
            .mappings()
            .one()
        )
        plan = header["plan"]
        if _hash(plan) != header["plan_hash"]:
            raise ValueError("payout plan commitment mismatch")
        start = dt.datetime.fromtimestamp(header["utc_hour"] * 3600, dt.UTC)
        expected = validate_contract(start, start + dt.timedelta(hours=1), header["budget_aipg"], period_id)
        if plan["contract"] != expected:
            raise ValueError("payout plan policy mismatch")
        rows = (await session.execute(sa.select(payouts).where(payouts.c.period_id == period_id))).mappings().all()
        allocations = {row["account_id"]: row for row in plan["allocations"]}
        if len(rows) != len(allocations):
            raise ValueError("payout allocation count mismatch")
        for row in rows:
            planned = allocations.get(str(row["account_id"]))
            if (
                planned is None
                or amount(row["aipg_amount"]) != amount(planned["aipg"])
                or row["den"] != planned["den"]
                or (planned["address"] is not None and wallet(row["address"]) != planned["address"])
            ):
                raise ValueError("payout allocation differs from frozen plan")
        return [dict(row) for row in rows]

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Public stats/status endpoints — the v2 replacements for the legacy
/api/v2/status/* and /api/v2/stats/* the dashboard reads.

Live state (who's online, what models) comes from the Redis worker registry;
historical aggregates come from grid_ledger — the same append-only events the
settlement pays against, so dashboard numbers and payouts can never disagree.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import APIRouter

from ..database import new_session
from ..redis_client import get_redis
from ..v2.schema import ledger as ledger_table

logger = logging.getLogger("grid_api.stats")

router = APIRouter()

WORKER_STATUS_PREFIX = "grid:worker:"
WORKER_STATUS_SUFFIX = ":status"
WORKER_ACTIVE_SET = "grid:workers:active"


async def _active_workers() -> list[dict]:
    r = get_redis()
    out = []
    for wid in await r.smembers(WORKER_ACTIVE_SET):
        data = await r.get(f"{WORKER_STATUS_PREFIX}{wid}{WORKER_STATUS_SUFFIX}")
        if data:
            out.append(json.loads(data))
        else:
            await r.srem(WORKER_ACTIVE_SET, wid)
    return out


@router.get("/v1/workers")
async def list_workers():
    """Currently-connected workers (live, from the Redis registry)."""
    workers = await _active_workers()
    return {
        "count": len(workers),
        "workers": [
            {
                "id": w.get("worker_id"),
                "name": w.get("name"),
                "models": w.get("models", []),
                "job_types": w.get("job_types", ["text"]),
                "online": True,
            }
            for w in workers
        ],
    }


@router.get("/v1/status/models")
async def status_models():
    """Models currently served, with how many workers serve each."""
    workers = await _active_workers()
    counts: dict[str, int] = {}
    types: dict[str, set] = {}
    for w in workers:
        for m in w.get("models", []):
            counts[m] = counts.get(m, 0) + 1
            types.setdefault(m, set()).update(w.get("job_types", ["text"]))
    return [
        {
            "name": m,
            "count": c,
            "type": sorted(types.get(m, {"text"}))[0] if types.get(m) else "text",
        }
        for m, c in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def _since(period: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    return {
        "minute": now - timedelta(minutes=1),
        "hour": now - timedelta(hours=1),
        "day": now - timedelta(days=1),
        "month": now - timedelta(days=30),
        "total": None,
    }.get(period)


@router.get("/v1/stats/totals")
async def stats_totals():
    """Job/den totals by job type for day, month, and all time — from the
    ledger, so these are exactly the numbers settlement pays on."""
    out: dict = {}
    async with await new_session() as session:
        for period in ("day", "month", "total"):
            q = sa.select(
                ledger_table.c.job_type,
                sa.func.count().label("jobs"),
                sa.func.coalesce(sa.func.sum(ledger_table.c.den), 0.0).label("den"),
                sa.func.coalesce(sa.func.sum(ledger_table.c.output_units), 0).label("units"),
            ).group_by(ledger_table.c.job_type)
            since = _since(period)
            if since is not None:
                q = q.where(ledger_table.c.created >= since)
            rows = (await session.execute(q)).mappings().all()
            out[period] = {
                r["job_type"]: {
                    "jobs": r["jobs"],
                    "den": round(float(r["den"]), 2),
                    "units": int(r["units"]),
                }
                for r in rows
            }
    return out


@router.get("/v1/stats/models")
async def stats_models(period: str = "month"):
    """Per-model job counts + den over a period (minute/hour/day/month/total)."""
    if period not in ("minute", "hour", "day", "month", "total"):
        period = "month"
    async with await new_session() as session:
        q = sa.select(
            ledger_table.c.model,
            ledger_table.c.job_type,
            sa.func.count().label("jobs"),
            sa.func.coalesce(sa.func.sum(ledger_table.c.den), 0.0).label("den"),
        ).group_by(ledger_table.c.model, ledger_table.c.job_type)
        since = _since(period)
        if since is not None:
            q = q.where(ledger_table.c.created >= since)
        rows = (await session.execute(q)).mappings().all()
    return {
        "period": period,
        "models": [
            {
                "name": r["model"],
                "type": r["job_type"],
                "jobs": r["jobs"],
                "den": round(float(r["den"]), 2),
            }
            for r in sorted(rows, key=lambda r: -r["jobs"])
        ],
    }


@router.get("/v1/wallets/{address}/earnings")
async def wallet_earnings(address: str):
    """Den earnings for a wallet, straight from the ledger settlement pays on."""
    addr = address.lower()
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    async with await new_session() as session:
        totals = (
            await session.execute(
                sa.select(
                    sa.func.count().label("jobs"),
                    sa.func.coalesce(sa.func.sum(ledger_table.c.den), 0.0).label("den"),
                ).where(ledger_table.c.wallet == addr)
            )
        ).mappings().first()
        last24 = (
            await session.execute(
                sa.select(
                    sa.func.count().label("jobs"),
                    sa.func.coalesce(sa.func.sum(ledger_table.c.den), 0.0).label("den"),
                ).where(ledger_table.c.wallet == addr, ledger_table.c.created >= day_ago)
            )
        ).mappings().first()
        recent = (
            await session.execute(
                sa.select(
                    ledger_table.c.job_type,
                    ledger_table.c.model,
                    ledger_table.c.den,
                    ledger_table.c.created,
                )
                .where(ledger_table.c.wallet == addr)
                .order_by(ledger_table.c.id.desc())
                .limit(25)
            )
        ).mappings().all()
    return {
        "wallet": addr,
        "total": {"jobs": totals["jobs"], "den": round(float(totals["den"]), 2)},
        "last_24h": {"jobs": last24["jobs"], "den": round(float(last24["den"]), 2)},
        "recent": [
            {
                "type": r["job_type"],
                "model": r["model"],
                "den": round(float(r["den"]), 2),
                "at": r["created"].isoformat() if r["created"] else None,
            }
            for r in recent
        ],
    }

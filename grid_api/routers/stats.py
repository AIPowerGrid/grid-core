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
from ..v2.schema import payouts as payouts_table

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


@router.get("/v1/progress/{token}")
async def job_progress(token: str):
    """Latest generation progress (0–100) for a client-supplied progress_token.

    Media jobs run synchronously on the worker; the worker streams ComfyUI's
    per-step % to the grid, which stashes the latest value under this token so a
    client can poll it while its (blocking) generation request is in flight.
    Returns {progress: int|null} — null when nothing has been reported yet.
    """
    try:
        raw = await get_redis().get(f"grid:progress:{token}")
        return {"progress": int(raw) if raw is not None else None}
    except Exception:
        return {"progress": None}


async def _perf_by_model(session, since: datetime | None) -> dict[tuple[str, str], dict]:
    """Per-(model, job_type) performance from the ledger's timing columns over a
    window. Only rows with a recorded `duration` count (historical/NULL rows are
    excluded). t/s and TTFT are text-meaningful; media gets avg latency only.

    Returns {(model, job_type): {samples, avg_latency_s, tokens_per_s, avg_ttft_s}}.
    Aggregated from the same ledger settlement reads, so perf numbers and payouts
    derive from one source.
    """
    q = sa.select(
        ledger_table.c.model,
        ledger_table.c.job_type,
        sa.func.count().label("samples"),
        sa.func.avg(ledger_table.c.duration).label("avg_dur"),
        sa.func.sum(ledger_table.c.duration).label("sum_dur"),
        # Decode-only time (excl. TTFT/prefill) — the denominator for a comparable
        # tokens/sec. Rows with NULL ttft (media / pre-ttft history) fall back to
        # full duration via COALESCE.
        sa.func.sum(ledger_table.c.duration - sa.func.coalesce(ledger_table.c.ttft, 0.0)).label("sum_decode"),
        sa.func.sum(ledger_table.c.output_units).label("sum_units"),
        sa.func.avg(ledger_table.c.ttft).label("avg_ttft"),
    ).where(ledger_table.c.duration.isnot(None), ledger_table.c.duration > 0)
    if since is not None:
        q = q.where(ledger_table.c.created >= since)
    q = q.group_by(ledger_table.c.model, ledger_table.c.job_type)
    rows = (await session.execute(q)).mappings().all()
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        sum_dur = float(r["sum_dur"] or 0.0)
        sum_decode = float(r["sum_decode"] or 0.0)
        sum_units = int(r["sum_units"] or 0)
        is_text = r["job_type"] == "text"
        out[(r["model"], r["job_type"])] = {
            "samples": int(r["samples"]),
            "avg_latency_s": round(float(r["avg_dur"]), 2) if r["avg_dur"] is not None else None,
            # decode throughput: output tokens / generation time AFTER first token
            "tokens_per_s": round(sum_units / sum_decode, 1) if (is_text and sum_decode > 0) else None,
            "avg_ttft_s": round(float(r["avg_ttft"]), 2) if r["avg_ttft"] is not None else None,
        }
    return out


@router.get("/v1/status/models")
async def status_models():
    """Models currently served, with how many workers serve each — plus recent
    per-model performance (t/s, TTFT, avg latency) from the last 24h of ledger
    timing, so a picker can show live availability AND how fast each model is."""
    workers = await _active_workers()
    counts: dict[str, int] = {}
    types: dict[str, set] = {}
    ctx: dict[str, int] = {}
    for w in workers:
        wc = int(w.get("max_context_length") or 0)
        for m in w.get("models", []):
            counts[m] = counts.get(m, 0) + 1
            types.setdefault(m, set()).update(w.get("job_types", ["text"]))
            if wc > 0:
                ctx[m] = max(ctx.get(m, 0), wc)
    async with await new_session() as session:
        perf = await _perf_by_model(session, _since("day"))
    out = []
    for m, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        mtype = sorted(types.get(m, {"text"}))[0] if types.get(m) else "text"
        p = perf.get((m, mtype)) or {}
        out.append({
            "name": m,
            "count": c,
            "type": mtype,
            "max_context_length": ctx.get(m) or None,
            "samples": p.get("samples", 0),
            "tokens_per_s": p.get("tokens_per_s"),
            "avg_ttft_s": p.get("avg_ttft_s"),
            "avg_latency_s": p.get("avg_latency_s"),
        })
    return out


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
    since = _since(period)
    async with await new_session() as session:
        q = sa.select(
            ledger_table.c.model,
            ledger_table.c.job_type,
            sa.func.count().label("jobs"),
            sa.func.coalesce(sa.func.sum(ledger_table.c.den), 0.0).label("den"),
            sa.func.coalesce(sa.func.sum(ledger_table.c.output_units), 0).label("units"),
        ).group_by(ledger_table.c.model, ledger_table.c.job_type)
        if since is not None:
            q = q.where(ledger_table.c.created >= since)
        rows = (await session.execute(q)).mappings().all()
        perf = await _perf_by_model(session, since)
    return {
        "period": period,
        "models": [
            {
                "name": r["model"],
                "type": r["job_type"],
                "jobs": r["jobs"],
                "den": round(float(r["den"]), 2),
                "units": int(r["units"]),
                **{
                    k: perf.get((r["model"], r["job_type"]), {}).get(k)
                    for k in ("samples", "tokens_per_s", "avg_ttft_s", "avg_latency_s")
                },
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


@router.get("/v1/payouts/public")
async def payouts_public(limit: int = 50):
    """PUBLIC, no-auth worker-payout transparency. Aggregate only — NO account
    IDs / PII; just on-chain payout wallets + tx hashes (already public on Base).
    Feeds the /transparency page so anyone can verify what's been paid to workers."""
    PAID = ("sent", "confirmed")
    limit = max(1, min(int(limit or 50), 200))
    p = payouts_table
    async with await new_session() as session:
        totals = (await session.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(p.c.aipg_amount), 0).label("aipg"),
                sa.func.count().label("payouts"),
                sa.func.count(sa.distinct(p.c.address)).label("workers"),
                sa.func.count(sa.distinct(p.c.period_id)).label("periods"),
                sa.func.max(p.c.paid).label("last_paid"),
            ).where(p.c.status.in_(PAID))
        )).mappings().first()
        periods = (await session.execute(
            sa.select(
                p.c.period_id,
                sa.func.sum(p.c.aipg_amount).label("aipg"),
                sa.func.count().label("payouts"),
                sa.func.max(p.c.paid).label("at"),
            ).where(p.c.status.in_(PAID))
            .group_by(p.c.period_id).order_by(sa.func.max(p.c.paid).desc()).limit(limit)
        )).mappings().all()
        recent = (await session.execute(
            sa.select(p.c.period_id, p.c.aipg_amount, p.c.address, p.c.tx_hash, p.c.paid)
            .where(p.c.status.in_(PAID), p.c.tx_hash.isnot(None))
            .order_by(p.c.paid.desc()).limit(limit)
        )).mappings().all()
    return {
        "totals": {
            "aipg_paid": round(float(totals["aipg"] or 0), 4),
            "payouts": int(totals["payouts"] or 0),
            "workers_paid": int(totals["workers"] or 0),
            "periods": int(totals["periods"] or 0),
            "last_paid": totals["last_paid"].isoformat() if totals["last_paid"] else None,
        },
        "periods": [
            {"period_id": r["period_id"], "aipg": round(float(r["aipg"] or 0), 4),
             "payouts": int(r["payouts"]), "at": r["at"].isoformat() if r["at"] else None}
            for r in periods
        ],
        "recent": [
            {"period_id": r["period_id"], "aipg": round(float(r["aipg_amount"] or 0), 4),
             "address": r["address"], "tx_hash": r["tx_hash"],
             "at": r["paid"].isoformat() if r["paid"] else None}
            for r in recent
        ],
    }

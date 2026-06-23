# ⚠️ UNWIRED / SHIP-DARK (2026-06-22 audit): no live request-path code imports this module.
# It is built but NOT active — do NOT assume billing/slashing/registry-sync runs. Wire it
# intentionally (+ tests) before relying on it. See task #62.

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Slashable-event recording — the bridge between detection and enforcement.

The grid detects worker misbehavior in the hot path (a forged/mismatched result
receipt, repeated health strikes) but NEVER slashes from there: slashing touches
real bonded funds and is a deliberate, human-or-job-gated on-chain action
(WorkerRegistry.slash via SLASHER_ROLE). This module just durably records the
evidence so an offline enforcement job can review it and act.

Recording is fire-and-forget and never raises — detecting misbehavior must not
break the response the client is already getting. A DB hiccup degrades to a log
line, nothing more.

See aipg-smart-contracts/docs/WORKER_BONDING.md for the on-chain side.
"""

import asyncio
import logging
from datetime import datetime, timezone

import sqlalchemy as sa

from ..database import new_session
from ..v2.schema import slashable_events as events_table

logger = logging.getLogger("grid_api.enforcement")

# Receipt the worker SIGNED doesn't match what it streamed — the signed result
# hash differs from the bytes we relayed. Strong forgery signal.
KIND_RECEIPT_HASH_MISMATCH = "receipt_hash_mismatch"
# Receipt signature recovers to an address other than the registered signer —
# the worker is signing as someone else (or a corrupted/forged signature).
KIND_RECEIPT_SIGNER_MISMATCH = "receipt_signer_mismatch"
# Receipt present but couldn't be verified (malformed sig, decode error).
KIND_RECEIPT_VERIFY_ERROR = "receipt_verify_error"
# Worker tripped the health-strike threshold (see worker self-health / evict).
KIND_HEALTH_STRIKES = "health_strikes"


async def _ainsert(values: dict) -> None:
    try:
        async with await new_session() as session:
            await session.execute(sa.insert(events_table).values(**values))
            await session.commit()
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"slashable-event write failed ({values.get('kind')}): {e}", exc_info=True)


def record_slashable_event(
    *,
    worker_info: dict | None,
    kind: str,
    job_id: str | None = None,
    severity: str = "low",
    detail: dict | None = None,
) -> None:
    """Record a detected slashable event. Fire-and-forget, never raises.

    Pulls worker attribution (name, on-chain signer, payout wallet) from
    ``worker_info`` so the enforcement job knows exactly which on-chain identity
    a slash would target. Safe to call from sync or async code: if an event loop
    is running the insert is scheduled on it, otherwise we log and move on.
    """
    wi = worker_info or {}
    worker_id = wi.get("worker_id")
    values = {
        "worker_id": worker_id,
        "worker_name": wi.get("name"),
        "signer_address": (wi.get("signer_address") or None),
        "wallet": (wi.get("wallet_address") or None),
        "job_id": (str(job_id) if job_id else None),
        "kind": kind,
        "severity": severity,
        "detail": detail or {},
        "reviewed": False,
        "created": datetime.now(timezone.utc),
    }

    # Always leave a breadcrumb in the logs regardless of DB outcome.
    logger.warning(
        f"slashable event [{severity}] {kind} worker={wi.get('name')} "
        f"signer={wi.get('signer_address')} job={job_id} detail={detail}"
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_ainsert(values))
    else:  # pragma: no cover - no loop (e.g. unit test); log-only is acceptable
        logger.debug("no running loop; slashable event logged but not persisted")


async def list_unreviewed(limit: int = 200) -> list[dict]:
    """Unreviewed slashable events, newest first — the enforcement job's inbox."""
    async with await new_session() as session:
        rows = (
            await session.execute(
                sa.select(events_table)
                .where(events_table.c.reviewed.is_(False))
                .order_by(events_table.c.created.desc())
                .limit(limit)
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def mark_reviewed(event_id: int, action: str) -> None:
    """Close out an event after an operator acts on it (e.g. ``slash tx 0x…`` or
    ``dismissed``). Append-only in spirit: we only flip reviewed + note the action."""
    async with await new_session() as session:
        await session.execute(
            sa.update(events_table)
            .where(events_table.c.id == event_id)
            .values(reviewed=True, action=action)
        )
        await session.commit()
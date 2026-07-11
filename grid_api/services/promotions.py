# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable expiring promotional credits.

Promotional value is separate from the daily Redis allowance and purchased
credit. Grants are budgeted and idempotent; spends allocate earliest-expiring
grants first and record that allocation so a failed job restores the same
pocket. The whole spend path is flag-gated for a shadow rollout.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..database import new_session
from ..v2.schema import promo_campaigns, promo_grants, promo_spends, reservations

logger = logging.getLogger("grid_api.promotions")

PROMO_ENABLED = os.getenv("GRID_PROMO_CREDITS_ENABLED", "1").lower() in {"1", "true", "yes", "on"}
PROMO_SPENDABLE_LIVE = os.getenv("GRID_PROMO_SPENDABLE_LIVE", "0").lower() in {"1", "true", "yes", "on"}
WELCOME_CAMPAIGN_ID = os.getenv("GRID_WELCOME_CAMPAIGN_ID", "universal-welcome-v1")
WELCOME_GRANT_MICRO = int(os.getenv("GRID_WELCOME_GRANT_MICRO", "150000"))
WELCOME_EXPIRES_DAYS = int(os.getenv("GRID_WELCOME_EXPIRES_DAYS", "30"))
WELCOME_BUDGET_MICRO = int(os.getenv("GRID_WELCOME_BUDGET_MICRO", "15000000000"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_uuid(value) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _after(value: datetime | None, reference: datetime) -> bool:
    if value is None:
        return False
    if value.tzinfo is None and reference.tzinfo is not None:
        reference = reference.replace(tzinfo=None)
    elif value.tzinfo is not None and reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return value > reference


async def ensure_builtin_campaign() -> None:
    """Create/update the built-in welcome campaign without issuing value."""
    now = _now()
    async with await new_session() as session:
        row = (await session.execute(
            sa.select(promo_campaigns.c.id).where(promo_campaigns.c.id == WELCOME_CAMPAIGN_ID)
        )).first()
        if row:
            await session.execute(
                sa.update(promo_campaigns)
                .where(promo_campaigns.c.id == WELCOME_CAMPAIGN_ID)
                .values(
                    grant_micro=WELCOME_GRANT_MICRO,
                    budget_micro=WELCOME_BUDGET_MICRO,
                    expires_days=WELCOME_EXPIRES_DAYS,
                    eligibility={"verified_google": True},
                )
            )
        else:
            await session.execute(sa.insert(promo_campaigns).values(
                id=WELCOME_CAMPAIGN_ID,
                name="Universal welcome credit",
                grant_micro=WELCOME_GRANT_MICRO,
                budget_micro=WELCOME_BUDGET_MICRO,
                granted_micro=0,
                expires_days=WELCOME_EXPIRES_DAYS,
                eligibility={"verified_google": True},
                active=True,
                created=now,
            ))
        await session.commit()


async def grant_once(account_id, campaign_id: str = WELCOME_CAMPAIGN_ID, *, ref: str | None = None) -> dict:
    """Issue one campaign grant to one canonical account.

    The campaign row is locked before checking/incrementing its budget, making
    concurrent first logins unable to overrun the global campaign cap. The
    account+campaign constraint is the second idempotency guard.
    """
    if not PROMO_ENABLED:
        return {"status": "disabled", "granted_micro": 0}
    aid = _as_uuid(account_id)
    ref = ref or f"grant:{campaign_id}:{aid}"
    now = _now()
    async with await new_session() as session:
        campaign = (await session.execute(
            sa.select(promo_campaigns)
            .where(promo_campaigns.c.id == campaign_id)
            .with_for_update()
        )).mappings().first()
        if not campaign or not campaign["active"]:
            return {"status": "inactive", "granted_micro": 0}
        if _after(campaign["starts"], now):
            return {"status": "not_started", "granted_micro": 0}
        if campaign["ends"] and not _after(campaign["ends"], now):
            return {"status": "ended", "granted_micro": 0}

        existing = (await session.execute(
            sa.select(promo_grants.c.amount_micro, promo_grants.c.remaining_micro)
            .where(promo_grants.c.account_id == aid, promo_grants.c.campaign_id == campaign_id)
        )).first()
        if existing:
            return {"status": "already", "granted_micro": int(existing[0]), "remaining_micro": int(existing[1])}

        amount = int(campaign["grant_micro"] or 0)
        budget = campaign["budget_micro"]
        granted = int(campaign["granted_micro"] or 0)
        if amount <= 0:
            return {"status": "empty", "granted_micro": 0}
        if budget is not None and granted + amount > int(budget):
            return {"status": "budget_exhausted", "granted_micro": 0}
        expires = now + timedelta(days=int(campaign["expires_days"])) if campaign["expires_days"] else None
        try:
            await session.execute(sa.insert(promo_grants).values(
                id=uuid4(), account_id=aid, campaign_id=campaign_id,
                amount_micro=amount, remaining_micro=amount, status="active",
                ref=ref, expires=expires, created=now, updated=now,
            ))
            await session.execute(
                sa.update(promo_campaigns)
                .where(promo_campaigns.c.id == campaign_id)
                .values(granted_micro=promo_campaigns.c.granted_micro + amount)
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return {"status": "already", "granted_micro": 0}
    return {"status": "granted", "granted_micro": amount, "remaining_micro": amount,
            "expires": expires.isoformat() if expires else None}


async def available_micro(account_id) -> int:
    if not PROMO_ENABLED or not account_id:
        return 0
    now = _now()
    try:
        async with await new_session() as session:
            value = await session.scalar(
                sa.select(sa.func.coalesce(sa.func.sum(promo_grants.c.remaining_micro), 0))
                .where(
                    promo_grants.c.account_id == _as_uuid(account_id),
                    promo_grants.c.status == "active",
                    promo_grants.c.remaining_micro > 0,
                    sa.or_(promo_grants.c.expires.is_(None), promo_grants.c.expires > now),
                )
            )
            return int(value or 0)
    except Exception:
        return 0  # fail closed: unknown promotional value is not spendable


async def held_micro(account_id, ref: str) -> int:
    if not account_id or not ref:
        return 0
    async with await new_session() as session:
        value = await session.scalar(
            sa.select(promo_spends.c.amount_micro).where(
                promo_spends.c.account_id == _as_uuid(account_id), promo_spends.c.ref == ref
            )
        )
        return int(value or 0)


async def consume(account_id, want_micro: int, ref: str) -> int:
    """Hold up to want_micro from earliest-expiring active grants."""
    if not (PROMO_ENABLED and PROMO_SPENDABLE_LIVE) or not account_id or want_micro <= 0 or not ref:
        return 0
    aid = _as_uuid(account_id)
    now = _now()
    async with await new_session() as session:
        prior = (await session.execute(
            sa.select(promo_spends.c.amount_micro).where(promo_spends.c.ref == ref)
        )).first()
        if prior:
            return int(prior[0] or 0)

        rows = (await session.execute(
            sa.select(promo_grants.c.id, promo_grants.c.remaining_micro)
            .where(
                promo_grants.c.account_id == aid,
                promo_grants.c.status == "active",
                promo_grants.c.remaining_micro > 0,
                sa.or_(promo_grants.c.expires.is_(None), promo_grants.c.expires > now),
            )
            .order_by(promo_grants.c.expires.asc().nullslast(), promo_grants.c.created.asc())
            .with_for_update()
        )).all()
        remaining = int(want_micro)
        allocations: list[dict] = []
        for grant_id, available in rows:
            take = min(remaining, int(available or 0))
            if take <= 0:
                continue
            await session.execute(
                sa.update(promo_grants)
                .where(promo_grants.c.id == grant_id)
                .values(remaining_micro=promo_grants.c.remaining_micro - take, updated=now)
            )
            allocations.append({"grant_id": str(grant_id), "amount_micro": take})
            remaining -= take
            if remaining <= 0:
                break
        taken = int(want_micro) - remaining
        try:
            await session.execute(sa.insert(promo_spends).values(
                ref=ref, account_id=aid, amount_micro=taken, kept_micro=taken,
                allocations=allocations, status="held", created=now, updated=now,
            ))
            await session.commit()
        except IntegrityError:
            await session.rollback()
            prior = (await session.execute(
                sa.select(promo_spends.c.amount_micro).where(promo_spends.c.ref == ref)
            )).first()
            return int(prior[0] or 0) if prior else 0
        return taken


async def release(account_id, ref: str, keep_micro: int = 0) -> int:
    """Settle a promotional hold, restoring the unspent allocation exactly once."""
    if not PROMO_ENABLED or not account_id or not ref:
        return 0
    aid = _as_uuid(account_id)
    now = _now()
    async with await new_session() as session:
        spend = (await session.execute(
            sa.select(promo_spends).where(
                promo_spends.c.ref == ref, promo_spends.c.account_id == aid
            ).with_for_update()
        )).mappings().first()
        if not spend:
            return 0
        target_keep = max(min(int(keep_micro), int(spend["amount_micro"] or 0)), 0)
        current_keep = int(spend["kept_micro"] or 0)
        if target_keep >= current_keep:
            if spend["status"] == "held":
                await session.execute(
                    sa.update(promo_spends).where(promo_spends.c.ref == ref)
                    .values(status="settled", updated=now)
                )
                await session.commit()
            return 0

        keep_left = target_keep
        restored = 0
        for allocation in spend["allocations"] or []:
            amount = int(allocation.get("amount_micro") or 0)
            keep_here = min(keep_left, amount)
            restore_here = amount - keep_here
            keep_left -= keep_here
            if restore_here <= 0:
                continue
            grant_id = _as_uuid(allocation["grant_id"])
            grant = (await session.execute(
                sa.select(promo_grants.c.expires, promo_grants.c.status)
                .where(promo_grants.c.id == grant_id).with_for_update()
            )).first()
            if grant and grant[1] == "active" and (grant[0] is None or _after(grant[0], now)):
                await session.execute(
                    sa.update(promo_grants).where(promo_grants.c.id == grant_id)
                    .values(remaining_micro=promo_grants.c.remaining_micro + restore_here, updated=now)
                )
                restored += restore_here
        await session.execute(
            sa.update(promo_spends).where(promo_spends.c.ref == ref)
            .values(kept_micro=target_keep, status="settled", updated=now)
        )
        await session.commit()
        return restored


async def sweep_stale_spends(older_than_seconds: int = 3600, limit: int = 500) -> int:
    """Recover promo holds left around a reserve/settle process crash.

    No reservation means dispatch was never durably opened, so restore all.
    A settled reservation means demand settlement committed; conservatively keep
    the full promo hold because its exact final attribution is not persisted.
    Held reservations are owned by credits.sweep_stale_reservations and skipped.
    """
    if not PROMO_ENABLED:
        return 0
    cutoff = _now() - timedelta(seconds=max(int(older_than_seconds), 1))
    async with await new_session() as session:
        rows = (await session.execute(
            sa.select(
                promo_spends.c.ref, promo_spends.c.account_id,
                promo_spends.c.amount_micro, reservations.c.status,
            )
            .select_from(promo_spends.outerjoin(
                reservations, reservations.c.job_id == promo_spends.c.ref,
            ))
            .where(
                promo_spends.c.status == "held",
                promo_spends.c.created < cutoff,
            )
            .limit(limit)
        )).all()
    acted = 0
    for ref, account_id, amount_micro, reservation_status in rows:
        if reservation_status == "held":
            continue
        keep = int(amount_micro or 0) if reservation_status == "settled" else 0
        await release(account_id, ref, keep_micro=keep)
        acted += 1
    if acted:
        logger.warning("swept %d stale promotional holds older than %ds", acted, older_than_seconds)
    return acted

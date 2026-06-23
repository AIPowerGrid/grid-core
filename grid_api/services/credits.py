# ⚠️ UNWIRED / SHIP-DARK (2026-06-22 audit): no live request-path code imports this module.
# It is built but NOT active — do NOT assume billing/slashing/registry-sync runs. Wire it
# intentionally (+ tests) before relying on it. See task #62.

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prepaid credit ledger — AIPG-native (integer micro-AIPG, AIPG × 1e6).

No runtime oracle: an AIPG deposit credits the balance 1:1 (micro-AIPG), and a
charge debits AIPG directly (priced by `pricing`, which pegs to competitors at
deploy time only). `balance_micro` / `delta_micro` are micro-AIPG.

`debit` is overdraft-safe (a conditional UPDATE: balance only moves if it
covers the charge) and idempotent (unique `ref` per charge — a retried request
can't double-bill). `credit` (top-up) is idempotent on `ref` too, so a re-seen
AIPG deposit / Stripe event can't double-credit.

Charging is OFF by default (`GRID_CHARGING_ENABLED`): until you flip it on,
`charge_request` only LOGS what it would bill and never debits or blocks — so
this can ship dark and be observed against real traffic first.
"""

import datetime as _dt
import logging
import os

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..database import new_session
from ..v2.schema import credit_ledger as ledger_t
from ..v2.schema import credits as credits_t
from . import pricing

logger = logging.getLogger("grid_api.credits")

CHARGING_ENABLED = os.getenv("GRID_CHARGING_ENABLED", "0").lower() in ("1", "true", "yes")


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


async def get_balance(account_id) -> int:
    """Current balance in micro-USD (0 if no row)."""
    async with await new_session() as s:
        row = (
            await s.execute(
                sa.select(credits_t.c.balance_micro).where(credits_t.c.account_id == account_id)
            )
        ).first()
        return int(row[0]) if row else 0


async def credit(account_id, amount_micro: int, reason: str, ref: str | None = None, model: str | None = None) -> bool:
    """Top up. Idempotent on `ref`. Returns True if applied, False if a dup ref."""
    if amount_micro <= 0:
        return False
    async with await new_session() as s:
        try:
            await s.execute(sa.insert(ledger_t).values(
                account_id=account_id, delta_micro=amount_micro, reason=reason, ref=ref, model=model,
            ))
        except IntegrityError:
            await s.rollback()
            return False  # ref already seen — already credited
        res = await s.execute(
            sa.update(credits_t)
            .where(credits_t.c.account_id == account_id)
            .values(balance_micro=credits_t.c.balance_micro + amount_micro, updated=_now())
        )
        if res.rowcount == 0:
            await s.execute(sa.insert(credits_t).values(
                account_id=account_id, balance_micro=amount_micro, updated=_now(),
            ))
        await s.commit()
        return True


async def debit(account_id, amount_micro: int, reason: str, ref: str | None = None, model: str | None = None) -> str:
    """Atomic, overdraft-safe debit. Returns 'ok' | 'already' | 'insufficient'."""
    if amount_micro <= 0:
        return "ok"
    async with await new_session() as s:
        try:
            await s.execute(sa.insert(ledger_t).values(
                account_id=account_id, delta_micro=-amount_micro, reason=reason, ref=ref, model=model,
            ))
        except IntegrityError:
            await s.rollback()
            return "already"  # this job already charged
        # Conditional debit: only succeeds if the balance covers it (overdraft-safe + race-safe).
        res = await s.execute(
            sa.update(credits_t)
            .where(sa.and_(
                credits_t.c.account_id == account_id,
                credits_t.c.balance_micro >= amount_micro,
            ))
            .values(balance_micro=credits_t.c.balance_micro - amount_micro, updated=_now())
        )
        if res.rowcount == 0:
            await s.rollback()  # undoes the ledger insert too — nothing charged
            return "insufficient"
        await s.commit()
        return "ok"


def _account_id(user: dict):
    """v2 accounts have a Uuid account_id; legacy keys don't (not chargeable)."""
    return user.get("account_id")


async def has_credit(user: dict) -> bool:
    """True if the account has a positive balance (gate for paid access)."""
    aid = _account_id(user)
    if not aid:
        return False
    return (await get_balance(aid)) > 0


async def charge_request(user: dict, model: str, prompt_tokens: int, completion_tokens: int, job_id) -> dict:
    """Charge an account for one completion. Safe to call always.

    Returns {status, charged}. status: free (unpriced), legacy (no account),
    dry_run (charging disabled), ok, already, insufficient.
    """
    cost = pricing.quote_text(model, prompt_tokens, completion_tokens)
    if cost <= 0:
        return {"status": "free", "charged": 0}
    aid = _account_id(user)
    if not aid:
        return {"status": "legacy", "charged": 0}
    if not CHARGING_ENABLED:
        logger.info(
            "[charge:dry] account=%s model=%s in=%d out=%d would_charge=%d micro-AIPG (%.6f AIPG)",
            aid, model, prompt_tokens, completion_tokens, cost, cost / 1_000_000,
        )
        return {"status": "dry_run", "charged": 0, "would_charge": cost}
    status = await debit(aid, cost, reason="debit:chat", ref=str(job_id), model=model)
    if status == "insufficient":
        logger.warning("account=%s insufficient credit for %d micro-USD (model=%s)", aid, cost, model)
    return {"status": status, "charged": cost if status == "ok" else 0}
# ⚠️ WIRED-DARK (2026-06-23): the request path (routers/openai.py _meter_charge) now
# calls charge_request on every completion, but charging is GATED OFF by default
# (GRID_CHARGING_ENABLED=0) — it only LOGS the would-charge amount and never debits,
# blocks, or touches the credit tables. Flip the env var to go live. See task #73.

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prepaid credit ledger — USD-native (integer micro-USD, USD × 1e6).

No runtime oracle: a USDC deposit credits the balance 1:1 (micro-USD), and a
charge debits USD directly (priced by `pricing`, which pegs to competitors at
deploy time only). `balance_micro` / `delta_micro` are micro-USD. Non-USDC
deposits (ETH/cbBTC) are swapped to USDC at the door; AIPG deposits credit at
the peg — the conversion happens in the deposit watcher, never here.

`debit` is overdraft-safe (a conditional UPDATE: balance only moves if it
covers the charge) and idempotent (unique `ref` per charge — a retried request
can't double-bill). `credit` (top-up) is idempotent on `ref` too, so a re-seen
deposit / Stripe event can't double-credit.

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
from ..v2.schema import ledger as grid_ledger_t
from ..v2.schema import reservations as reservations_t
from . import pricing

logger = logging.getLogger("grid_api.credits")

CHARGING_ENABLED = os.getenv("GRID_CHARGING_ENABLED", "0").lower() in ("1", "true", "yes")

# ── >=100k-AIPG holder discount (dark by default) ──────────────────────────
# A login wallet holding >= GRID_HOLDER_MIN_AIPG AIPG on Base pays a percentage
# less on every charge. Ties AIPG *holding* to a demand-side benefit (buy-and-hold
# pressure). Applied identically in reserve / charge / reconcile via a cached
# balance read (holdings.aipg_balance_raw, ~10 min TTL) so the held, billed and
# settled amounts always agree. No-op unless GRID_HOLDER_DISCOUNT_ENABLED=1.
HOLDER_DISCOUNT_ENABLED = os.getenv("GRID_HOLDER_DISCOUNT_ENABLED", "0").lower() in ("1", "true", "yes", "on")
HOLDER_DISCOUNT_BPS = int(os.getenv("GRID_HOLDER_DISCOUNT_BPS", "2500") or 2500)  # 2500 = 25% off
HOLDER_MIN_AIPG = int(os.getenv("GRID_HOLDER_MIN_AIPG", "100000") or 100000)      # whole AIPG


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


async def _wallet_for_account(account_id) -> str | None:
    """Look up the login wallet for an account (media path has only an id)."""
    from ..v2.schema import accounts as accounts_t
    async with await new_session() as s:
        row = (await s.execute(
            sa.select(accounts_t.c.wallet).where(accounts_t.c.id == account_id)
        )).first()
    return row[0] if row and row[0] else None


async def apply_holder_discount(cost_micro: int, *, wallet: str | None = None, account_id=None) -> int:
    """Return the micro-USD cost after the >=100k-AIPG holder discount.

    No-op when the feature is off (default), the cost is non-positive, or the
    holder doesn't qualify. Never raises — a failed/timed-out balance read just
    means "no discount", never a blocked or mis-priced charge. Safe to call in
    dry-run (so the logged would_charge previews the discounted number too)."""
    if cost_micro <= 0 or not HOLDER_DISCOUNT_ENABLED or HOLDER_DISCOUNT_BPS <= 0:
        return cost_micro
    try:
        if wallet is None and account_id is not None:
            wallet = await _wallet_for_account(account_id)
        if not wallet:
            return cost_micro
        from . import holdings
        bal = await holdings.aipg_balance_raw(wallet)
        if bal < HOLDER_MIN_AIPG * (10 ** holdings.AIPG_DECIMALS):
            return cost_micro
        return int(cost_micro * (10_000 - HOLDER_DISCOUNT_BPS) // 10_000)
    except Exception:
        logger.warning("holder-discount read failed; charging full price", exc_info=True)
        return cost_micro


async def get_balance(account_id) -> int:
    """Current balance in micro-USD (0 if no row)."""
    async with await new_session() as s:
        row = (
            await s.execute(
                sa.select(credits_t.c.balance_micro).where(credits_t.c.account_id == account_id)
            )
        ).first()
        return int(row[0]) if row else 0


async def _credit_in_session(s, account_id, amount_micro: int, reason: str, ref: str, model: str | None = None) -> None:
    await s.execute(sa.insert(ledger_t).values(
        account_id=account_id, delta_micro=amount_micro, reason=reason, ref=ref, model=model,
    ))
    res = await s.execute(
        sa.update(credits_t)
        .where(credits_t.c.account_id == account_id)
        .values(balance_micro=credits_t.c.balance_micro + amount_micro, updated=_now())
    )
    if res.rowcount == 0:
        await s.execute(sa.insert(credits_t).values(
            account_id=account_id, balance_micro=amount_micro, updated=_now(),
        ))


async def _debit_in_session(s, account_id, amount_micro: int, reason: str, ref: str, model: str | None = None) -> str:
    await s.execute(sa.insert(ledger_t).values(
        account_id=account_id, delta_micro=-amount_micro, reason=reason, ref=ref, model=model,
    ))
    res = await s.execute(
        sa.update(credits_t)
        .where(sa.and_(
            credits_t.c.account_id == account_id,
            credits_t.c.balance_micro >= amount_micro,
        ))
        .values(balance_micro=credits_t.c.balance_micro - amount_micro, updated=_now())
    )
    return "ok" if res.rowcount else "insufficient"


async def _insert_reservation_in_session(s, job_id, account_id, model: str, reserved_micro: int, prompt_toks: int) -> None:
    await s.execute(sa.insert(reservations_t).values(
        job_id=str(job_id), account_id=account_id, model=model,
        reserved_micro=int(reserved_micro or 0), prompt_toks=int(prompt_toks or 0),
        status="held", created=_now(),
    ))


async def _reservation_reserved_micro(job_id) -> int | None:
    async with await new_session() as s:
        row = (await s.execute(
            sa.select(reservations_t.c.reserved_micro).where(reservations_t.c.job_id == str(job_id))
        )).first()
        return int(row[0] or 0) if row else None


async def _try_extra_debit_in_session(s, account_id, amount_micro: int, ref: str, model: str | None = None) -> bool:
    """Best-effort settlement extra without making the terminal claim retry forever."""
    res = await s.execute(
        sa.update(credits_t)
        .where(sa.and_(
            credits_t.c.account_id == account_id,
            credits_t.c.balance_micro >= amount_micro,
        ))
        .values(balance_micro=credits_t.c.balance_micro - amount_micro, updated=_now())
    )
    if res.rowcount == 0:
        return False
    await s.execute(sa.insert(ledger_t).values(
        account_id=account_id, delta_micro=-amount_micro,
        reason="reconcile:extra", ref=ref, model=model,
    ))
    return True


async def credit(account_id, amount_micro: int, reason: str, ref: str | None = None, model: str | None = None) -> bool:
    """Top up. Idempotent on `ref`. Returns True if applied, False if a dup ref."""
    if amount_micro <= 0:
        return False
    if not ref:
        # Idempotency is structural, not caller-discipline: a value-moving row
        # MUST carry a dedup key. (DB NOT NULL constraint is the migration-phase
        # hard lock; this is the code-level shield.)
        raise ValueError("credit() requires a non-null ref")
    async with await new_session() as s:
        try:
            await _credit_in_session(s, account_id, amount_micro, reason, ref, model)
        except IntegrityError:
            await s.rollback()
            return False  # ref already seen — already credited
        await s.commit()
        return True


async def debit(account_id, amount_micro: int, reason: str, ref: str | None = None, model: str | None = None) -> str:
    """Atomic, overdraft-safe debit. Returns 'ok' | 'already' | 'insufficient'."""
    if amount_micro <= 0:
        return "ok"
    if not ref:
        raise ValueError("debit() requires a non-null ref")
    async with await new_session() as s:
        try:
            status = await _debit_in_session(s, account_id, amount_micro, reason, ref, model)
        except IntegrityError:
            await s.rollback()
            return "already"  # this job already charged
        # Conditional debit: only succeeds if the balance covers it (overdraft-safe + race-safe).
        if status == "insufficient":
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
    cost = await apply_holder_discount(cost, wallet=user.get("wallet"), account_id=aid)
    if not CHARGING_ENABLED:
        logger.info(
            "[charge:dry] account=%s model=%s in=%d out=%d would_charge=%d micro-USD ($%.4f)",
            aid, model, prompt_tokens, completion_tokens, cost, cost / 1_000_000,
        )
        return {"status": "dry_run", "charged": 0, "would_charge": cost}
    status = await debit(aid, cost, reason="debit:chat", ref=str(job_id), model=model)
    if status == "insufficient":
        logger.warning("account=%s insufficient credit for %d micro-USD (model=%s)", aid, cost, model)
    return {"status": status, "charged": cost if status == "ok" else 0}


async def authorize_request(user: dict, model: str, prompt_tokens: int, max_tokens: int, job_id,
                            *, record_reservation: bool = False) -> dict:
    """Pre-dispatch billing gate (LIVE mode only). Reserve the MAX possible cost
    before any work is queued — paid inference is never dispatched unless funds
    are held first. The caller turns ok=False into a 402 BEFORE submitting the
    job. Returns {ok, reserved, status, reason?}.

    Policy:
    - dry-run (charging off) → ok, reserved 0 (caller logs via charge_request).
    - unpriced model in enforce mode → BLOCKED (default-deny; B5).
    - priced at 0 (free model) → ok, reserved 0.
    - no chargeable v2 account (e.g. legacy key) in enforce mode → BLOCKED.
    - insufficient balance → BLOCKED.
    When `record_reservation=True`, the debit and `grid_reservations` row are
    created in one transaction. That is the durable worker-WS lifecycle: the job
    must never be dispatched if either the hold or its settlement context cannot
    be written. Idempotent: a retry with the same job_id re-uses the existing
    reservation (debit returns 'already' on the duplicate ref).
    """
    if not CHARGING_ENABLED:
        return {"ok": True, "reserved": 0, "status": "dry_run"}
    if not pricing.is_priced(model):
        return {"ok": False, "reserved": 0, "status": "unpriced",
                "reason": f"model '{model}' is not available for billing"}
    cost = pricing.quote_text(model, int(prompt_tokens or 0), int(max_tokens or 0))
    if cost <= 0:
        return {"ok": True, "reserved": 0, "status": "free"}
    aid = _account_id(user)
    if not aid:
        return {"ok": False, "reserved": 0, "status": "no_account",
                "reason": "billing requires a v2 account key"}
    cost = await apply_holder_discount(cost, wallet=user.get("wallet"), account_id=aid)
    if record_reservation:
        ref = str(job_id)
        async with await new_session() as s:
            try:
                status = await _debit_in_session(s, aid, cost, reason="reserve:chat", ref=ref, model=model)
            except IntegrityError:
                await s.rollback()
                reserved = await _reservation_reserved_micro(job_id)
                if reserved is not None:
                    return {"ok": True, "reserved": reserved, "status": "already"}
                logger.error("reservation debit exists without reservation row job=%s account=%s", job_id, aid)
                return {"ok": False, "reserved": 0, "status": "reservation_missing",
                        "reason": "billing reservation is inconsistent; retry with a new request id"}
            if status == "insufficient":
                await s.rollback()
                logger.info("[charge:402] account=%s model=%s reserve=%d micro-USD: insufficient", aid, model, cost)
                return {"ok": False, "reserved": 0, "status": "insufficient",
                        "reason": "insufficient credits"}
            try:
                await _insert_reservation_in_session(s, job_id, aid, model, cost, prompt_tokens)
            except IntegrityError:
                await s.rollback()
                reserved = await _reservation_reserved_micro(job_id)
                if reserved is not None:
                    return {"ok": True, "reserved": reserved, "status": "already"}
                logger.error("reservation insert conflicted without readable row job=%s account=%s", job_id, aid)
                return {"ok": False, "reserved": 0, "status": "reservation_failed",
                        "reason": "billing reservation failed"}
            except Exception:
                await s.rollback()
                logger.error("reservation insert failed job=%s account=%s", job_id, aid, exc_info=True)
                return {"ok": False, "reserved": 0, "status": "reservation_failed",
                        "reason": "billing reservation failed"}
            await s.commit()
            return {"ok": True, "reserved": cost, "status": "ok"}

    status = await debit(aid, cost, reason="reserve:chat", ref=str(job_id), model=model)
    if status in ("ok", "already"):
        return {"ok": True, "reserved": cost, "status": status}
    logger.info("[charge:402] account=%s model=%s reserve=%d micro-USD: insufficient", aid, model, cost)
    return {"ok": False, "reserved": 0, "status": "insufficient",
            "reason": "insufficient credits"}


async def reconcile(user: dict, model: str, prompt_tokens: int, completion_tokens: int,
                    reserved_micro: int, job_id) -> None:
    """Post-completion settlement (LIVE mode only). We reserved the max up front;
    refund the unused portion based on ACTUAL usage. Best-effort + loud on error:
    the response already went out, so a settlement failure must NEVER crash it —
    a failed refund is money owed to the user, never a giveaway. Idempotent via
    job-scoped refs (`:refund` / `:extra`)."""
    if not CHARGING_ENABLED:
        return
    aid = _account_id(user)
    if not aid or reserved_micro <= 0:
        return
    try:
        actual = pricing.quote_text(model, int(prompt_tokens or 0), int(completion_tokens or 0))
        # Same discount the reservation used, so the refund math is consistent.
        actual = await apply_holder_discount(actual, wallet=user.get("wallet"), account_id=aid)
        diff = reserved_micro - actual
        if diff > 0:
            await credit(aid, diff, reason="reconcile:refund", ref=f"{job_id}:refund", model=model)
        elif diff < 0:
            # Actual exceeded the reservation (prompt estimate was low). Collect
            # the remainder best-effort; if the balance can't cover it we already
            # served, so log and move on — never block on settlement.
            extra = await debit(aid, -diff, reason="reconcile:extra", ref=f"{job_id}:extra", model=model)
            if extra != "ok":
                logger.warning("reconcile under-collected account=%s job=%s by %d micro-USD (%s)",
                               aid, job_id, -diff, extra)
    except Exception:
        logger.error("reconcile failed account=%s job=%s (refund may be owed)", aid, job_id, exc_info=True)


async def authorize_media(account_id, model: str, job_type: str, n: int, seconds, job_id,
                          *, record_reservation: bool = False) -> dict:
    """Pre-dispatch billing gate for media (image/video). Unlike text, media cost
    is deterministic from the request (n images / video seconds), so we reserve
    the EXACT cost up front; on success it stands (settle_exact), on failure it's
    released (release_job). Same enforce policy as authorize_request:
    unpriced / no-account / insufficient → blocked. Returns {ok, reserved, status}.

    When `record_reservation=True`, the debit and `grid_reservations` row commit
    in one transaction — the durable worker-WS lifecycle (prompt_toks=0 since media
    isn't token-priced; settle_exact ignores it)."""
    if not CHARGING_ENABLED:
        return {"ok": True, "reserved": 0, "status": "dry_run"}
    if not pricing.is_priced(model):
        return {"ok": False, "reserved": 0, "status": "unpriced",
                "reason": f"model '{model}' is not available for billing"}
    if job_type == "video":
        cost = pricing.quote_video(model, float(seconds or 0))
    else:
        cost = pricing.quote_image(model, int(n or 1))
    if cost <= 0:
        return {"ok": True, "reserved": 0, "status": "free"}
    if not account_id:
        return {"ok": False, "reserved": 0, "status": "no_account",
                "reason": "billing requires a v2 account"}
    cost = await apply_holder_discount(cost, account_id=account_id)
    if record_reservation:
        async with await new_session() as s:
            try:
                status = await _debit_in_session(s, account_id, cost,
                                                 reason=f"reserve:{job_type}", ref=str(job_id), model=model)
            except IntegrityError:
                await s.rollback()
                reserved = await _reservation_reserved_micro(job_id)
                if reserved is not None:
                    return {"ok": True, "reserved": reserved, "status": "already"}
                logger.error("media reserve debit exists without reservation row job=%s account=%s", job_id, account_id)
                return {"ok": False, "reserved": 0, "status": "reservation_missing",
                        "reason": "billing reservation is inconsistent; retry with a new request id"}
            if status == "insufficient":
                await s.rollback()
                logger.info("[charge:402] account=%s model=%s reserve=%d micro-USD: insufficient", account_id, model, cost)
                return {"ok": False, "reserved": 0, "status": "insufficient", "reason": "insufficient credits"}
            try:
                await _insert_reservation_in_session(s, job_id, account_id, model, cost, 0)
            except IntegrityError:
                await s.rollback()
                reserved = await _reservation_reserved_micro(job_id)
                if reserved is not None:
                    return {"ok": True, "reserved": reserved, "status": "already"}
                return {"ok": False, "reserved": 0, "status": "reservation_failed",
                        "reason": "billing reservation failed"}
            except Exception:
                await s.rollback()
                logger.error("media reservation insert failed job=%s account=%s", job_id, account_id, exc_info=True)
                return {"ok": False, "reserved": 0, "status": "reservation_failed",
                        "reason": "billing reservation failed"}
            await s.commit()
            return {"ok": True, "reserved": cost, "status": "ok"}

    status = await debit(account_id, cost, reason=f"reserve:{job_type}", ref=str(job_id), model=model)
    if status in ("ok", "already"):
        return {"ok": True, "reserved": cost, "status": status}
    logger.info("[charge:402] account=%s model=%s reserve=%d micro-USD: insufficient", account_id, model, cost)
    return {"ok": False, "reserved": 0, "status": "insufficient", "reason": "insufficient credits"}


async def refund_reservation(account_id, reserved_micro: int, job_id) -> None:
    """Refund a media reservation when the job didn't run (fault / timeout / 429).
    Best-effort + idempotent on the `:refund` ref; never raises into the caller."""
    if not CHARGING_ENABLED or not account_id or reserved_micro <= 0:
        return
    try:
        await credit(account_id, reserved_micro, reason="refund:media", ref=f"{job_id}:refund")
    except Exception:
        logger.error("media refund failed account=%s job=%s (refund owed)", account_id, job_id, exc_info=True)


# ── Durable per-job reservation lifecycle (worker-WS is the sole settler) ─────
#
# The HTTP request handler reserves before dispatch and records a 'held' row in
# the same transaction. The worker-WS handler — which reaches a terminal state
# for EVERY job whether or not the client stayed connected — calls settle_job /
# release_job on the job's outcome. The held→settled UPDATE and ledger movement
# commit together, so a duplicate/retried terminal is a no-op and a disconnected
# client can never strand or double-settle a reservation.


async def open_reservation(job_id, account_id, model: str, reserved_micro: int, prompt_toks: int) -> None:
    """Record durable billing context for a job so the worker-WS terminal handler
    can settle it without the HTTP collector. Idempotent on job_id (a requeued
    job keeps its ORIGINAL held row + reservation). Best-effort; never raises.
    No-op in dry-run so the system still ships dark (no credit-table writes)."""
    if not CHARGING_ENABLED:
        return
    try:
        async with await new_session() as s:
            try:
                await _insert_reservation_in_session(s, job_id, account_id, model, reserved_micro, prompt_toks)
                await s.commit()
            except IntegrityError:
                await s.rollback()  # already opened (retry/requeue) — keep the original
    except Exception:
        logger.error("open_reservation failed job=%s (settlement context missing)", job_id, exc_info=True)


async def settle_job(job_id, completion_tokens: int, *, status: str = "ok") -> None:
    """Authoritative terminal settlement (called from the worker-WS handler).

    Reconciles the held amount against ACTUAL grid-counted usage (refund the
    unused remainder / collect any shortfall) and flips held→settled in the same
    transaction. Pass status='failed' for a job that produced nothing billable →
    full release (refund). The completion count MUST be a grid-side figure
    (server tiktoken of relayed text), never worker-reported usage.
    Best-effort + loud on error: a settlement failure is money owed, never a
    giveaway, and must not crash the worker loop. Because the status flip and
    ledger movement commit together, a failed refund leaves the reservation held
    and retryable instead of marking it settled prematurely."""
    try:
        async with await new_session() as s:
            row = (await s.execute(
                sa.select(reservations_t.c.account_id, reservations_t.c.model,
                          reservations_t.c.reserved_micro, reservations_t.c.prompt_toks)
                .where(reservations_t.c.job_id == str(job_id))
            )).first()
            if not row:
                return

            res = await s.execute(
                sa.update(reservations_t)
                .where(sa.and_(reservations_t.c.job_id == str(job_id),
                               reservations_t.c.status == "held"))
                .values(status="settled", settled=_now())
            )
            if res.rowcount == 0:
                await s.rollback()
                return  # lost the race — another terminal already settled it

            aid, model = row[0], row[1]
            reserved, prompt_toks = int(row[2] or 0), int(row[3] or 0)

            if not CHARGING_ENABLED:
                if status == "ok":
                    cost = pricing.quote_text(model, prompt_toks, int(completion_tokens or 0))
                    logger.info("[settle:dry] job=%s account=%s model=%s in=%d out=%d "
                                "would_charge=%d micro-USD ($%.4f)", job_id, aid, model,
                                prompt_toks, completion_tokens, cost, cost / 1_000_000)
                else:
                    logger.info("[settle:dry] job=%s released (status=%s)", job_id, status)
                await s.commit()
                return

            if aid and reserved > 0:
                if status != "ok":
                    await _credit_in_session(s, aid, reserved, "release:failed", f"{job_id}:refund", model)
                else:
                    actual = pricing.quote_text(model, prompt_toks, int(completion_tokens or 0))
                    diff = reserved - actual
                    if diff > 0:
                        await _credit_in_session(s, aid, diff, "reconcile:refund", f"{job_id}:refund", model)
                    elif diff < 0:
                        extra_ok = await _try_extra_debit_in_session(s, aid, -diff, f"{job_id}:extra", model)
                        if not extra_ok:
                            logger.warning("settle under-collected account=%s job=%s by %d micro-USD (insufficient)",
                                           aid, job_id, -diff)
            await s.commit()
    except Exception:
        logger.error("settle_job failed job=%s (refund may be owed)", job_id, exc_info=True)


async def settle_exact(job_id) -> None:
    """Terminal settlement for a job whose cost was reserved EXACTLY (media): the
    held amount already equals the charge, so the reservation simply stands — flip
    held→settled with no ledger movement. Exactly-once via the conditional UPDATE;
    a no-op on a duplicate terminal or unknown job. Best-effort; never raises."""
    try:
        async with await new_session() as s:
            res = await s.execute(
                sa.update(reservations_t)
                .where(sa.and_(reservations_t.c.job_id == str(job_id),
                               reservations_t.c.status == "held"))
                .values(status="settled", settled=_now())
            )
            await s.commit()
            if res.rowcount == 0:
                return  # already settled / unknown job
    except Exception:
        logger.error("settle_exact failed job=%s", job_id, exc_info=True)


async def release_job(job_id) -> None:
    """Terminal release for a job that produced nothing billable (client error,
    worker fault surfaced, give-up). Full refund of the held reservation, once."""
    await settle_job(job_id, 0, status="failed")


async def record_and_settle(*, ledger_values: dict, completion_tokens: int = 0,
                            exact: bool = False) -> str:
    """ATOMIC terminal for a SUCCESSFUL job: write the worker-payout ledger row
    AND settle the demand reservation in ONE transaction — both commit or neither.

    This closes the window where a crash between the (separate) ledger write and
    settlement could leave a paid worker with a still-`held`, later-refunded
    reservation (or charge the user while the worker payout row was lost).

    `ledger_values` are the record_completion_in_session kwargs. `exact=True`
    (media) lets the exact reserve stand; otherwise reconcile against
    `completion_tokens` (text/passthrough). Returns:
      'duplicate'       — job already in grid_ledger (double dispatch) → nothing done
      'settled'         — ledger written + reservation reconciled (paid success)
      'no_reservation'  — ledger written; no held row (dry-run / legacy / free)
      'stale_no_payout' — reservation existed but already closed → rolled back, no
                          ledger row, no payout, no charge
      'error'           — nothing committed (retryable; the sweeper recovers)
    Best-effort: never raises into the worker loop."""
    from . import ledger as ledger_svc
    job_id = str(ledger_values["job_id"])
    try:
        async with await new_session() as s:
            try:
                await ledger_svc.record_completion_in_session(s, **ledger_values)
            except IntegrityError:
                await s.rollback()
                return "duplicate"  # already settled by a prior dispatch — do nothing

            row = (await s.execute(
                sa.select(reservations_t.c.account_id, reservations_t.c.model,
                          reservations_t.c.reserved_micro, reservations_t.c.prompt_toks)
                .where(reservations_t.c.job_id == job_id)
            )).first()
            if not row:
                await s.commit()  # ledger stands; nothing to settle (dry-run/legacy/free)
                return "no_reservation"

            res = await s.execute(
                sa.update(reservations_t)
                .where(sa.and_(reservations_t.c.job_id == job_id,
                               reservations_t.c.status == "held"))
                .values(status="settled", settled=_now())
            )
            if res.rowcount == 0:
                # A reservation EXISTS but is no longer held — it was already
                # settled/released elsewhere (e.g. a timeout release or the
                # sweeper). The demand side is closed (likely refunded), so paying
                # the worker now would be unbalanced: ROLL BACK the ledger insert.
                # No payout, no charge — the wasted work is the tradeoff for the
                # race. (Caller logs; this never silently double-accounts.)
                await s.rollback()
                return "stale_no_payout"

            aid, model = row[0], row[1]
            reserved, prompt_toks = int(row[2] or 0), int(row[3] or 0)
            if CHARGING_ENABLED and aid and reserved > 0 and not exact:
                actual = pricing.quote_text(model, prompt_toks, int(completion_tokens or 0))
                # The reservation was held at the discounted rate; settle at the
                # same rate so the refund/extra math is consistent (else a holder
                # is over-collected against their discounted hold).
                actual = await apply_holder_discount(actual, account_id=aid)
                diff = reserved - actual
                if diff > 0:
                    await _credit_in_session(s, aid, diff, "reconcile:refund", f"{job_id}:refund", model)
                elif diff < 0:
                    ok = await _try_extra_debit_in_session(s, aid, -diff, f"{job_id}:extra", model)
                    if not ok:
                        logger.warning("settle under-collected account=%s job=%s by %d micro-USD (insufficient)",
                                       aid, job_id, -diff)
            await s.commit()
            return "settled"
    except Exception:
        logger.error("record_and_settle failed job=%s (terminal not committed; retryable)", job_id, exc_info=True)
        return "error"


async def _ledger_completion(job_id):
    """(job_type, output_units) if a worker-payout row exists for this job, else None."""
    from . import ledger as ledger_svc
    async with await new_session() as s:
        row = (await s.execute(
            sa.select(grid_ledger_t.c.job_type, grid_ledger_t.c.output_units)
            .where(grid_ledger_t.c.job_id == ledger_svc.as_uuid(job_id))
        )).first()
        return (row[0], int(row[1] or 0)) if row else None


async def sweep_stale_reservations(older_than_seconds: int = 3600, limit: int = 500) -> int:
    """Safety net for the rare crash between reserve and the worker-WS terminal,
    which would otherwise strand a hold. Acts on reservations stuck in 'held' past
    a GENEROUS deadline (the threshold must exceed the longest job timeout so an
    in-flight job is never touched).

    LEDGER-AWARE — a held row is NOT blindly refunded:
      * if a worker-payout row EXISTS for the job, the worker did the work but
        settlement didn't commit (crash between ledger + settle) → SETTLE it
        (charge), never refund.
      * otherwise the job never produced output → RELEASE (full refund).
    Returns the total number of reservations acted on. No-op in dry-run.
    Idempotent: settle/release re-flip already-settled rows to a no-op."""
    if not CHARGING_ENABLED:
        return 0
    cutoff = _now() - _dt.timedelta(seconds=older_than_seconds)
    async with await new_session() as s:
        rows = (await s.execute(
            sa.select(reservations_t.c.job_id)
            .where(sa.and_(reservations_t.c.status == "held",
                           reservations_t.c.created < cutoff))
            .limit(limit)
        )).all()
    job_ids = [r[0] for r in rows]
    released = settled = 0
    for jid in job_ids:
        led = await _ledger_completion(jid)
        if led is None:
            await release_job(jid)            # never ran → refund the hold
            released += 1
        else:
            job_type, units = led
            if job_type in ("image", "video"):
                await settle_exact(jid)        # media: exact reserve stands
            else:
                await settle_job(jid, units)   # text/passthrough: charge grid usage
            settled += 1
    if job_ids:
        logger.warning("swept stale held reservations older than %ds: %d released, %d settled-from-ledger",
                       older_than_seconds, released, settled)
    return released + settled

# WIRED-DARK: every demand path calls this service, but
# GRID_CHARGING_MODE=off only records dry-run observations. Use allowlist for a
# bounded canary before considering on. The legacy GRID_CHARGING_ENABLED switch
# is compatibility-only when GRID_CHARGING_MODE is absent.

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prepaid credit ledger — USD-native (integer micro-USD, USD × 1e6).

No request-time oracle: a USDC deposit credits the balance 1:1 (micro-USD), and
a charge debits USD directly (priced by `pricing`, which pegs to competitors at
deploy time only). `balance_micro` / `delta_micro` are micro-USD. Non-stable
funding adapters perform and record their bounded deposit-time valuation before
calling this ledger; request settlement never reprices deposited value.

`debit` is overdraft-safe (a conditional UPDATE: balance only moves if it
covers the charge) and idempotent (unique `ref` per charge — a retried request
can't double-bill). `credit` (top-up) is idempotent on `ref` too, so a re-seen
deposit / Stripe event can't double-credit.

Charging is OFF by default (`GRID_CHARGING_MODE=off`): requests only log what
they would bill and never debit or block. `allowlist` enables a bounded account
or service cohort; `on` enables all authenticated demand.
"""

import datetime as _dt
import logging
import os

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..database import new_session
from ..safe_logging import error_type, opaque_id
from ..v2.schema import accounts as accounts_t
from ..v2.schema import credit_ledger as ledger_t
from ..v2.schema import credits as credits_t
from ..v2.schema import ledger as grid_ledger_t
from ..v2.schema import reservations as reservations_t
from . import free_credits
from . import promotions
from . import pricing
from . import service_limits
from . import validator_audit_budgets

logger = logging.getLogger("grid_api.credits")

CHARGING_ENABLED = os.getenv("GRID_CHARGING_ENABLED", "0").lower() in ("1", "true", "yes")
_CHARGING_MODE_ENV = os.getenv("GRID_CHARGING_MODE", "").strip().lower()
_CHARGING_MODES = {"off", "allowlist", "on"}
_EXACT_COST_JOB_TYPES = frozenset({"image", "video", "audio", "3d"})


def _csv_env(name: str) -> frozenset[str]:
    return frozenset(
        item.strip().lower()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    )


CHARGING_ALLOW_ACCOUNTS = _csv_env("GRID_CHARGING_ALLOW_ACCOUNTS")
CHARGING_ALLOW_SERVICES = _csv_env("GRID_CHARGING_ALLOW_SERVICES")
CHARGING_ALLOW_MODELS = _csv_env("GRID_CHARGING_ALLOW_MODELS")


def charging_mode() -> str:
    """Return the rollout mode, preserving the legacy boolean as a fallback."""
    if _CHARGING_MODE_ENV in _CHARGING_MODES:
        return _CHARGING_MODE_ENV
    if _CHARGING_MODE_ENV:
        logger.error("Invalid GRID_CHARGING_MODE=%r; failing closed with charging off", _CHARGING_MODE_ENV)
        return "off"
    return "on" if CHARGING_ENABLED else "off"


def charging_enabled_for(user: dict | None = None, model: str | None = None) -> bool:
    """Whether this request may create a live monetary reservation.

    In allowlist mode, user/delegated work is selected only by canonical
    account. The service allowlist is reserved for an explicitly scoped direct
    service principal; it must never make every user behind that service
    chargeable. The optional model list narrows that cohort and never enables
    charging by itself.
    """
    mode = charging_mode()
    if mode == "off":
        return False
    if mode == "on":
        return True
    user = user or {}
    account_id = str(user.get("account_id") or "").lower()
    service_id = str(user.get("service_id") or "").lower()
    direct_service = (
        user.get("key_kind") == "service"
        and "inference.service_submit" in set(user.get("scopes") or [])
    )
    selected = (
        bool(service_id and service_id in CHARGING_ALLOW_SERVICES)
        if direct_service
        else bool(account_id and account_id in CHARGING_ALLOW_ACCOUNTS)
    )
    if not selected:
        return False
    if CHARGING_ALLOW_MODELS and model is not None:
        return str(model or "").lower() in CHARGING_ALLOW_MODELS
    return True


def _economic_alert(kind: str, severity: str, summary: str, *, account=None, job=None, **fields) -> None:
    from . import alerts

    if account is not None:
        fields["account"] = alerts.opaque_id(account)
    if job is not None:
        fields["job"] = alerts.opaque_id(job)
    alerts.emit(kind, severity, summary, fields=fields, dedupe_key=f"{kind}:{fields.get('account', '-')}")

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
    discount = await holder_discount_bps(wallet=wallet, account_id=account_id)
    if cost_micro <= 0 or discount <= 0:
        return cost_micro
    factor = 10_000 - discount
    return (cost_micro * factor + 9_999) // 10_000 if factor > 0 else 0


async def holder_discount_bps(*, wallet: str | None = None, account_id=None) -> int:
    """Return the reservation-time discount to snapshot with the hold."""
    if not HOLDER_DISCOUNT_ENABLED or HOLDER_DISCOUNT_BPS <= 0:
        return 0
    try:
        if wallet is None and account_id is not None:
            wallet = await _wallet_for_account(account_id)
        if not wallet:
            return 0
        from . import holdings
        bal = await holdings.aipg_balance_raw(wallet)
        if bal < HOLDER_MIN_AIPG * (10 ** holdings.AIPG_DECIMALS):
            return 0
        return max(0, min(HOLDER_DISCOUNT_BPS, 10_000))
    except Exception as exc:
        logger.warning(
            "holder-discount read failed; charging full price error_type=%s",
            error_type(exc),
        )
        return 0


def _snapshot_rates(model: str) -> tuple[int, int]:
    price = pricing.get_price(model)
    if not price:
        return 0, 0
    return (
        int(round(price.input_per_mtok * pricing.MICRO)),
        int(round(price.output_per_mtok * pricing.MICRO)),
    )


def _quote_snapshot(prompt_tokens: int, completion_tokens: int, input_rate: int,
                    output_rate: int, discount_bps: int) -> int:
    numerator = int(prompt_tokens or 0) * input_rate + int(completion_tokens or 0) * output_rate
    base = (numerator + pricing.MICRO - 1) // pricing.MICRO if numerator > 0 else 0
    factor = 10_000 - max(0, min(int(discount_bps or 0), 10_000))
    return (base * factor + 9_999) // 10_000 if base > 0 and factor > 0 else 0


async def get_balance(account_id) -> int:
    """Current balance in micro-USD (0 if no row)."""
    async with await new_session() as s:
        row = (
            await s.execute(
                sa.select(credits_t.c.balance_micro).where(credits_t.c.account_id == account_id)
            )
        ).first()
        return int(row[0]) if row else 0


def _usd(micro: int) -> float:
    return round(int(micro or 0) / pricing.MICRO, 6)


async def account_credit_summary(user: dict) -> dict:
    """Canonical read model for every first-party balance display."""
    account_id = _account_id(user)
    if not account_id:
        raise ValueError("credit summary requires a v2 account")
    wallet = user.get("wallet") or None
    paid = await get_balance(account_id)
    promo_left = await promotions.available_micro(account_id)
    daily_cap = await free_credits.daily_cap_micro(account_id, wallet)
    free_left = await free_credits.available_micro(account_id, wallet)
    free_active = free_credits.FREE_ENABLED and free_credits.FREE_SPENDABLE_LIVE
    promo_active = promotions.PROMO_ENABLED and promotions.PROMO_SPENDABLE_LIVE
    spendable = paid + (free_left if free_active else 0) + (promo_left if promo_active else 0)
    preview = paid + free_left + promo_left
    return {
        "account_id": str(account_id),
        "promotional": {
            "remaining_micro": promo_left,
            "remaining_usd": _usd(promo_left),
            "active": promo_active,
        },
        "free": {
            "daily_cap_micro": daily_cap,
            "remaining_micro": free_left,
            "daily_cap_usd": _usd(daily_cap),
            "remaining_usd": _usd(free_left),
            "resets": "utc-midnight",
            "holder_bonus_active": daily_cap > free_credits.FREE_DAILY_MICRO,
            "active": free_active,
        },
        "paid": {
            "balance_micro": paid,
            "balance_usd": _usd(paid),
        },
        "total_spendable_micro": spendable,
        "total_spendable_usd": _usd(spendable),
        "total_preview_micro": preview,
        "total_preview_usd": _usd(preview),
        "charging_enabled": charging_enabled_for(user),
        "charging_mode": charging_mode(),
    }


async def quote_for_account(
    user: dict,
    *,
    model: str,
    modality: str,
    prompt_tokens: int = 0,
    max_tokens: int = 0,
    n: int = 1,
    seconds: float = 0.0,
) -> dict:
    """Return a non-mutating quote that mirrors reservation-time pricing."""
    modality = (modality or "").lower()
    summary = await account_credit_summary(user)
    priced = pricing.is_priced_for(model, modality)
    request_shape = {
        "prompt_tokens": prompt_tokens if modality == "text" else None,
        "max_tokens": max_tokens if modality == "text" else None,
        "n": n if modality in {"image", "3d"} else None,
        "seconds": seconds if modality in {"video", "audio"} else None,
    }
    if not priced:
        summary["charging_enabled"] = charging_enabled_for(user, model)
        summary["estimate"] = {
            "model": model,
            "modality": modality,
            "priced": False,
            "reason": "unpriced",
            "base_cost_micro": None,
            "base_cost_usd": None,
            "discount_bps": 0,
            "cost_micro": None,
            "cost_usd": None,
            "balance_sufficient": False,
            "from_promotional_micro": None,
            "from_daily_micro": None,
            "from_paid_micro": None,
            "shortfall_micro": None,
            **request_shape,
        }
        return summary

    if modality == "text":
        input_rate, output_rate = _snapshot_rates(model)
        base_cost = _quote_snapshot(prompt_tokens, max_tokens, input_rate, output_rate, 0)
    elif modality == "image":
        base_cost = pricing.quote_image(model, n)
    elif modality == "video":
        base_cost = pricing.quote_video(model, seconds)
    elif modality == "audio":
        base_cost = pricing.quote_audio(model, seconds)
    else:
        base_cost = pricing.quote_3d(model, n)

    discount_bps = await holder_discount_bps(
        wallet=user.get("wallet"),
        account_id=_account_id(user),
    )
    factor = 10_000 - discount_bps
    cost = (base_cost * factor + 9_999) // 10_000 if base_cost > 0 and factor > 0 else 0
    promo_available = (
        summary["promotional"]["remaining_micro"]
        if summary["promotional"]["active"]
        else 0
    )
    daily_available = summary["free"]["remaining_micro"] if summary["free"]["active"] else 0
    paid_available = summary["paid"]["balance_micro"]
    from_promo = min(cost, promo_available)
    from_daily = min(cost - from_promo, daily_available)
    from_paid = min(cost - from_promo - from_daily, paid_available)
    shortfall = max(0, cost - from_promo - from_daily - from_paid)
    summary["charging_enabled"] = charging_enabled_for(user, model)
    summary["estimate"] = {
        "model": model,
        "modality": modality,
        "priced": True,
        "reason": None,
        "base_cost_micro": base_cost,
        "base_cost_usd": _usd(base_cost),
        "discount_bps": discount_bps,
        "cost_micro": cost,
        "cost_usd": _usd(cost),
        "balance_sufficient": shortfall == 0,
        "from_promotional_micro": from_promo,
        "from_daily_micro": from_daily,
        "from_paid_micro": from_paid,
        "shortfall_micro": shortfall,
        **request_shape,
    }
    return summary


async def _locked_canonical_account(s, account_id):
    """Serialize value movement with account merges, then resolve aliases."""
    from .identities import canonical_account_id

    candidate = await canonical_account_id(account_id, session=s)
    lock_ids = sorted({account_id, candidate}, key=str)
    await s.execute(
        sa.select(accounts_t.c.id)
        .where(accounts_t.c.id.in_(lock_ids))
        .order_by(accounts_t.c.id)
        .with_for_update()
    )
    current = await canonical_account_id(account_id, session=s)
    if current != candidate:
        raise RuntimeError("account changed during value movement; retry")
    return current


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


async def _insert_reservation_in_session(s, job_id, account_id, model: str, reserved_micro: int,
                                          prompt_toks: int, free_micro: int = 0,
                                          promo_micro: int = 0, input_rate: int | None = None,
                                          output_rate: int | None = None, discount_bps: int = 0,
                                          service_id: str | None = None,
                                          billing_source: str = "credits",
                                          external_payer: str | None = None) -> None:
    # A UUID authorizes either customer-funded work or protocol-funded audit
    # work, never both. The shared Postgres advisory lock closes the cross-table
    # race that separate UNIQUE constraints cannot express.
    await validator_audit_budgets.assert_no_audit_in_session(s, job_id)
    await s.execute(sa.insert(reservations_t).values(
        job_id=str(job_id), account_id=account_id, model=model,
        reserved_micro=int(reserved_micro or 0), free_micro=int(free_micro or 0),
        promo_micro=int(promo_micro or 0),
        prompt_toks=int(prompt_toks or 0),
        input_per_mtok_micro=input_rate,
        output_per_mtok_micro=output_rate,
        discount_bps=int(discount_bps or 0),
        service_id=service_id,
        billing_source=billing_source,
        external_payer=external_payer,
        status="held", created=_now(),
    ))


async def _free_first(account_id, wallet: str | None, cost: int, ref: str) -> int:
    """Draw the daily FREE allowance before the paid balance (live mode only).
    Returns the micro-USD covered by free; the caller holds only the remainder
    from paid. Gated on GRID_FREE_SPENDABLE_LIVE so the /v1/account/credits
    `free.active` flag stays truthful: flag off → free is display-only and the
    whole cost is held from paid, exactly as the API reports. Atomic + idempotent
    on `ref` (a retried authorize returns the same split); 0 on any failure
    (fail-closed — free never over-grants, paid covers it)."""
    if not (free_credits.FREE_ENABLED and free_credits.FREE_SPENDABLE_LIVE):
        return 0
    if wallet is None:
        wallet = await _wallet_for_account(account_id)
    return await free_credits.consume(account_id, wallet, cost, ref=ref)


async def _promo_first(account_id, cost: int, ref: str) -> int:
    """Draw durable expiring promotion grants before daily and paid credit."""
    if not (promotions.PROMO_ENABLED and promotions.PROMO_SPENDABLE_LIVE):
        return 0
    try:
        return await promotions.consume(account_id, cost, ref=ref)
    except Exception as exc:
        logger.warning(
            "promotion consume failed; falling through to daily/paid error_type=%s",
            error_type(exc),
        )
        return 0


async def _promo_release(account_id, ref: str, keep_micro: int = 0) -> int:
    try:
        return await promotions.release(account_id, ref, keep_micro=keep_micro)
    except Exception as exc:
        logger.error(
            "promotion release failed account=%s ref=%s error_type=%s",
            opaque_id(account_id),
            opaque_id(ref),
            error_type(exc),
        )
        return 0


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
        account_id = await _locked_canonical_account(s, account_id)
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
        account_id = await _locked_canonical_account(s, account_id)
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
    wallet = user.get("wallet")
    if not charging_enabled_for(user, model):
        # Preview the free-first split for observability (no consume in dry-run).
        promo_avail = await promotions.available_micro(aid)
        from_promo = min(cost, promo_avail)
        free_avail = await free_credits.available_micro(aid, wallet)
        from_free = min(cost - from_promo, free_avail)
        logger.info(
            "[charge:dry] account=%s model=%s in=%d out=%d would_charge=%d micro-USD "
            "(promo=%d free=%d paid=%d, $%.4f)",
            opaque_id(aid), model, prompt_tokens, completion_tokens, cost, from_promo, from_free,
            cost - from_promo - from_free, cost / 1_000_000,
        )
        return {"status": "dry_run", "charged": 0, "would_charge": cost,
                "from_promo": from_promo, "from_free": from_free,
                "from_paid": cost - from_promo - from_free}
    # LIVE: draw the daily FREE allowance first, then the purchased balance.
    from_promo = await _promo_first(aid, cost, str(job_id))
    from_free = await _free_first(aid, wallet, cost - from_promo, str(job_id))
    remainder = cost - from_promo - from_free
    if remainder <= 0:
        return {"status": "ok", "charged": cost, "from_promo": from_promo,
                "from_free": from_free, "from_paid": 0}
    status = await debit(aid, remainder, reason="debit:chat", ref=str(job_id), model=model)
    if status == "insufficient":
        await _promo_release(aid, str(job_id))
        await free_credits.release(aid, str(job_id))
        logger.warning("account=%s insufficient credit for %d micro-USD (model=%s, free-applied=%d)",
                       opaque_id(aid), remainder, model, from_free)
        _economic_alert(
            "insufficient_credit",
            "warning",
            "A charge was rejected because the account had insufficient spendable credit.",
            account=aid,
            job=job_id,
            model=model,
            required_micro=remainder,
        )
    return {"status": status,
            "charged": cost if status == "ok" else 0,
            "from_promo": from_promo,
            "from_free": from_free,
            "from_paid": remainder if status == "ok" else 0}


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
    if not charging_enabled_for(user, model):
        return {"ok": True, "reserved": 0, "status": "dry_run"}
    if not pricing.is_priced_for(model, "text"):
        _economic_alert(
            "unpriced_work_blocked",
            "critical",
            "A live text request was blocked because no applicable price exists.",
            account=_account_id(user),
            job=job_id,
            model=model,
            modality="text",
        )
        return {"ok": False, "reserved": 0, "status": "unpriced",
                "reason": f"model '{model}' has no text price"}
    input_rate, output_rate = _snapshot_rates(model)
    discount_bps = await holder_discount_bps(
        wallet=user.get("wallet"), account_id=_account_id(user),
    )
    cost = _quote_snapshot(prompt_tokens, max_tokens, input_rate, output_rate, discount_bps)
    if cost <= 0:
        return {"ok": True, "reserved": 0, "status": "free"}
    aid = _account_id(user)
    if not aid:
        return {"ok": False, "reserved": 0, "status": "no_account",
                "reason": "billing requires a v2 account key"}
    service_ok, service_reason = await service_limits.authorize(user, cost, str(job_id))
    if not service_ok:
        _economic_alert(
            "service_exposure_blocked",
            "warning",
            "A service request exceeded its configured monetary exposure limit.",
            account=aid,
            job=job_id,
            service=user.get("service_id") or "-",
            model=model,
        )
        return {"ok": False, "reserved": 0, "status": "service_limit",
                "reason": service_reason}
    # PROMO → DAILY FREE → PAID. Idempotent on job_id — a retry sees the same split. The free
    # consume is a Redis op outside the SQL txn: every failure path below releases
    # it, so the worst crash case forfeits part of ONE day's free allowance
    # (self-heals at midnight), never paid money.
    ref = str(job_id)
    from_promo = await _promo_first(aid, cost, ref)
    from_free = await _free_first(aid, user.get("wallet"), cost - from_promo, ref)
    remainder = cost - from_promo - from_free
    if record_reservation:
        async with await new_session() as s:
            try:
                if remainder > 0:
                    status = await _debit_in_session(s, aid, remainder, reason="reserve:chat", ref=ref, model=model)
                else:
                    status = "ok"  # free covered everything — nothing held from paid
            except IntegrityError:
                await s.rollback()
                reserved = await _reservation_reserved_micro(job_id)
                if reserved is not None:
                    return {"ok": True, "reserved": reserved, "status": "already"}
                await service_limits.release(user.get("service_id"), ref)
                logger.error(
                    "reservation debit exists without reservation row job=%s account=%s",
                    opaque_id(job_id),
                    opaque_id(aid),
                )
                _economic_alert(
                    "reservation_inconsistent",
                    "critical",
                    "A debit exists without its durable reservation row.",
                    account=aid,
                    job=job_id,
                    model=model,
                )
                return {"ok": False, "reserved": 0, "status": "reservation_missing",
                        "reason": "billing reservation is inconsistent; retry with a new request id"}
            if status == "insufficient":
                await s.rollback()
                await _promo_release(aid, ref)
                await free_credits.release(aid, ref)  # give back the free we just took
                await service_limits.release(user.get("service_id"), ref)
                logger.info("[charge:402] account=%s model=%s reserve=%d micro-USD (free=%d): insufficient",
                            opaque_id(aid), model, cost, from_free)
                _economic_alert(
                    "insufficient_credit",
                    "warning",
                    "A text request was rejected before dispatch for insufficient spendable credit.",
                    account=aid,
                    job=job_id,
                    model=model,
                    required_micro=cost,
                )
                return {"ok": False, "reserved": 0, "status": "insufficient",
                        "reason": "insufficient credits"}
            try:
                await _insert_reservation_in_session(s, job_id, aid, model, cost, prompt_tokens,
                                                     free_micro=from_free, promo_micro=from_promo,
                                                     input_rate=input_rate, output_rate=output_rate,
                                                     discount_bps=discount_bps,
                                                     service_id=user.get("service_id"))
            except IntegrityError:
                await s.rollback()
                reserved = await _reservation_reserved_micro(job_id)
                if reserved is not None:
                    return {"ok": True, "reserved": reserved, "status": "already"}
                logger.error(
                    "reservation insert conflicted without readable row job=%s account=%s",
                    opaque_id(job_id),
                    opaque_id(aid),
                )
                await _promo_release(aid, ref)
                await free_credits.release(aid, ref)
                await service_limits.release(user.get("service_id"), ref)
                _economic_alert(
                    "reservation_conflict",
                    "critical",
                    "A reservation insert conflicted without a readable durable hold.",
                    account=aid,
                    job=job_id,
                    model=model,
                )
                return {"ok": False, "reserved": 0, "status": "reservation_failed",
                        "reason": "billing reservation failed"}
            except Exception as exc:
                await s.rollback()
                logger.error(
                    "reservation insert failed job=%s account=%s error_type=%s",
                    opaque_id(job_id),
                    opaque_id(aid),
                    error_type(exc),
                )
                await _promo_release(aid, ref)
                await free_credits.release(aid, ref)
                await service_limits.release(user.get("service_id"), ref)
                _economic_alert(
                    "reservation_failed",
                    "critical",
                    "A text reservation failed before dispatch.",
                    account=aid,
                    job=job_id,
                    model=model,
                )
                return {"ok": False, "reserved": 0, "status": "reservation_failed",
                        "reason": "billing reservation failed"}
            await s.commit()
            return {"ok": True, "reserved": cost, "status": "ok",
                    "from_promo": from_promo, "from_free": from_free}

    if remainder > 0:
        status = await debit(aid, remainder, reason="reserve:chat", ref=ref, model=model)
    else:
        status = "ok"
    if status in ("ok", "already"):
        return {"ok": True, "reserved": cost, "status": status,
                "from_promo": from_promo, "from_free": from_free}
    await _promo_release(aid, ref)
    await free_credits.release(aid, ref)
    await service_limits.release(user.get("service_id"), ref)
    logger.info("[charge:402] account=%s model=%s reserve=%d micro-USD (free=%d): insufficient",
                opaque_id(aid), model, cost, from_free)
    return {"ok": False, "reserved": 0, "status": "insufficient",
            "reason": "insufficient credits"}


async def authorize_x402_request(
    model: str,
    prompt_tokens: int,
    max_tokens: int,
    job_id,
    *,
    payment_payload,
    payment_requirements,
) -> dict:
    """Open a durable external reservation after x402 verification.

    This never touches a Grid credit balance or daily free/promotional pockets.
    The x402 middleware already verified the payer's authorization; we still
    price default-deny, require the quoted maximum to fit inside the signed
    authorization, and atomically write both reservation and payment receipt
    before dispatch.
    """
    from . import x402_payments

    if not x402_payments.ENABLED:
        return {
            "ok": False,
            "reserved": 0,
            "status": "disabled",
            "reason": "x402 payments are not enabled",
        }
    if not pricing.is_priced_for(model, "text"):
        return {
            "ok": False,
            "reserved": 0,
            "status": "unpriced",
            "reason": f"model '{model}' has no text price",
        }

    details = x402_payments.payment_payload_details(
        payment_payload,
        payment_requirements,
    )
    if details["network"] != x402_payments.NETWORK:
        return {
            "ok": False,
            "reserved": 0,
            "status": "wrong_network",
            "reason": "x402 payment uses the wrong network",
        }
    if details["asset"] != x402_payments.USDC:
        return {
            "ok": False,
            "reserved": 0,
            "status": "wrong_asset",
            "reason": "x402 payment must use configured Base USDC",
        }
    if details["pay_to"] != x402_payments.PAY_TO:
        return {
            "ok": False,
            "reserved": 0,
            "status": "wrong_recipient",
            "reason": "x402 payment uses the wrong recipient",
        }

    input_rate, output_rate = _snapshot_rates(model)
    cost = _quote_snapshot(prompt_tokens, max_tokens, input_rate, output_rate, 0)
    if cost <= 0:
        return {
            "ok": False,
            "reserved": 0,
            "status": "invalid_price",
            "reason": "x402 requires a positive quoted amount",
        }
    if cost > int(details["authorized_micro"]):
        return {
            "ok": False,
            "reserved": 0,
            "status": "authorization_too_small",
            "reason": (
                f"request can cost up to {cost} micro-USD; "
                f"x402 authorization covers {details['authorized_micro']}"
            ),
        }

    async with await new_session() as session:
        try:
            await _insert_reservation_in_session(
                session,
                job_id,
                None,
                model,
                cost,
                prompt_tokens,
                input_rate=input_rate,
                output_rate=output_rate,
                billing_source="x402",
                external_payer=details["payer"],
            )
            await x402_payments.insert_verified_in_session(
                session,
                job_id=str(job_id),
                details=details,
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            async with await new_session() as check:
                row = (
                    await check.execute(
                        sa.select(
                            reservations_t.c.reserved_micro,
                            reservations_t.c.external_payer,
                            reservations_t.c.billing_source,
                        ).where(reservations_t.c.job_id == str(job_id))
                    )
                ).first()
            if (
                row
                and row[1] == details["payer"]
                and row[2] == "x402"
                and int(row[0]) <= int(details["authorized_micro"])
            ):
                return {"ok": True, "reserved": int(row[0]), "status": "already"}
            return {
                "ok": False,
                "reserved": 0,
                "status": "conflict",
                "reason": "x402 request id conflicts with another payment",
            }
        except Exception as exc:
            await session.rollback()
            logger.error(
                "x402 reservation failed job=%s error_type=%s",
                opaque_id(job_id),
                error_type(exc),
            )
            return {
                "ok": False,
                "reserved": 0,
                "status": "reservation_failed",
                "reason": "x402 reservation failed",
            }
    return {
        "ok": True,
        "reserved": cost,
        "status": "ok",
        "payer": details["payer"],
    }


async def reservation_actual_micro(job_id) -> int | None:
    """Return the immutable terminal charge once worker-side settlement wins."""
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(
                    reservations_t.c.status,
                    reservations_t.c.actual_micro,
                ).where(reservations_t.c.job_id == str(job_id))
            )
        ).first()
        if not row or row[0] != "settled" or row[1] is None:
            return None
        return int(row[1])


async def reconcile(user: dict, model: str, prompt_tokens: int, completion_tokens: int,
                    reserved_micro: int, job_id) -> None:
    """Post-completion settlement (LIVE mode only). We reserved the max up front;
    refund the unused portion based on ACTUAL usage. Best-effort + loud on error:
    the response already went out, so a settlement failure must NEVER crash it —
    a failed refund is money owed to the user, never a giveaway. Idempotent via
    job-scoped refs (`:refund` / `:extra`)."""
    if not charging_enabled_for(user, model):
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
                               opaque_id(aid), opaque_id(job_id), -diff, extra)
                _economic_alert(
                    "settlement_under_collected",
                    "critical",
                    "A completed request exceeded its reservation and the shortfall could not be collected.",
                    account=aid,
                    job=job_id,
                    model=model,
                    shortfall_micro=-diff,
                )
    except Exception as exc:
        logger.error(
            "reconcile failed account=%s job=%s error_type=%s (refund may be owed)",
            opaque_id(aid),
            opaque_id(job_id),
            error_type(exc),
        )
        _economic_alert(
            "reconcile_failed",
            "critical",
            "Post-completion reconciliation failed; a refund may be owed.",
            account=aid,
            job=job_id,
            model=model,
        )


async def authorize_media(account_id, model: str, job_type: str, n: int, seconds, job_id,
                          *, record_reservation: bool = False, user: dict | None = None) -> dict:
    """Pre-dispatch billing gate for media (image/video). Unlike text, media cost
    is deterministic from the request (n images / video seconds), so we reserve
    the EXACT cost up front; on success it stands (settle_exact), on failure it's
    released (release_job). Same enforce policy as authorize_request:
    unpriced / no-account / insufficient → blocked. Returns {ok, reserved, status}.

    When `record_reservation=True`, the debit and `grid_reservations` row commit
    in one transaction — the durable worker-WS lifecycle (prompt_toks=0 since media
    isn't token-priced; settle_exact ignores it)."""
    billing_user = user or {"account_id": account_id}
    if not charging_enabled_for(billing_user, model):
        return {"ok": True, "reserved": 0, "status": "dry_run"}
    if not pricing.is_priced_for(model, job_type):
        _economic_alert(
            "unpriced_work_blocked",
            "critical",
            "A live media request was blocked because no applicable price exists.",
            account=account_id,
            job=job_id,
            model=model,
            modality=job_type,
        )
        return {"ok": False, "reserved": 0, "status": "unpriced",
                "reason": f"model '{model}' has no {job_type} price"}
    if job_type == "video":
        cost = pricing.quote_video(model, float(seconds or 0))
    elif job_type == "audio":
        cost = pricing.quote_audio(model, float(seconds or 0))
    elif job_type == "3d":
        cost = pricing.quote_3d(model, int(n or 1))
    else:
        cost = pricing.quote_image(model, int(n or 1))
    if cost <= 0:
        return {"ok": True, "reserved": 0, "status": "free"}
    if not account_id:
        return {"ok": False, "reserved": 0, "status": "no_account",
                "reason": "billing requires a v2 account"}
    cost = await apply_holder_discount(cost, account_id=account_id)
    service_ok, service_reason = await service_limits.authorize(
        user or {"account_id": account_id}, cost, str(job_id),
    )
    if not service_ok:
        _economic_alert(
            "service_exposure_blocked",
            "warning",
            "A service media request exceeded its configured monetary exposure limit.",
            account=account_id,
            job=job_id,
            service=(user or {}).get("service_id") or "-",
            model=model,
            modality=job_type,
        )
        return {"ok": False, "reserved": 0, "status": "service_limit",
                "reason": service_reason}
    # PROMO → DAILY FREE → PAID (same contract as authorize_request).
    ref = str(job_id)
    from_promo = await _promo_first(account_id, cost, ref)
    from_free = await _free_first(account_id, None, cost - from_promo, ref)
    remainder = cost - from_promo - from_free
    if record_reservation:
        async with await new_session() as s:
            try:
                if remainder > 0:
                    status = await _debit_in_session(s, account_id, remainder,
                                                     reason=f"reserve:{job_type}", ref=ref, model=model)
                else:
                    status = "ok"  # free covered everything
            except IntegrityError:
                await s.rollback()
                reserved = await _reservation_reserved_micro(job_id)
                if reserved is not None:
                    return {"ok": True, "reserved": reserved, "status": "already"}
                await service_limits.release((user or {}).get("service_id"), ref)
                logger.error(
                    "media reserve debit exists without reservation row job=%s account=%s",
                    opaque_id(job_id),
                    opaque_id(account_id),
                )
                _economic_alert(
                    "reservation_inconsistent",
                    "critical",
                    "A media debit exists without its durable reservation row.",
                    account=account_id,
                    job=job_id,
                    model=model,
                    modality=job_type,
                )
                return {"ok": False, "reserved": 0, "status": "reservation_missing",
                        "reason": "billing reservation is inconsistent; retry with a new request id"}
            if status == "insufficient":
                await s.rollback()
                await _promo_release(account_id, ref)
                await free_credits.release(account_id, ref)
                await service_limits.release((user or {}).get("service_id"), ref)
                logger.info("[charge:402] account=%s model=%s reserve=%d micro-USD (free=%d): insufficient",
                            opaque_id(account_id), model, cost, from_free)
                _economic_alert(
                    "insufficient_credit",
                    "warning",
                    "A media request was rejected before dispatch for insufficient spendable credit.",
                    account=account_id,
                    job=job_id,
                    model=model,
                    modality=job_type,
                    required_micro=cost,
                )
                return {"ok": False, "reserved": 0, "status": "insufficient", "reason": "insufficient credits"}
            try:
                await _insert_reservation_in_session(s, job_id, account_id, model, cost, 0,
                                                     free_micro=from_free, promo_micro=from_promo,
                                                     service_id=(user or {}).get("service_id"))
            except IntegrityError:
                await s.rollback()
                reserved = await _reservation_reserved_micro(job_id)
                if reserved is not None:
                    return {"ok": True, "reserved": reserved, "status": "already"}
                await _promo_release(account_id, ref)
                await free_credits.release(account_id, ref)
                await service_limits.release((user or {}).get("service_id"), ref)
                _economic_alert(
                    "reservation_failed",
                    "critical",
                    "A media reservation failed before dispatch.",
                    account=account_id,
                    job=job_id,
                    model=model,
                    modality=job_type,
                )
                return {"ok": False, "reserved": 0, "status": "reservation_failed",
                        "reason": "billing reservation failed"}
            except Exception as exc:
                await s.rollback()
                logger.error(
                    "media reservation insert failed job=%s account=%s error_type=%s",
                    opaque_id(job_id),
                    opaque_id(account_id),
                    error_type(exc),
                )
                await _promo_release(account_id, ref)
                await free_credits.release(account_id, ref)
                await service_limits.release((user or {}).get("service_id"), ref)
                _economic_alert(
                    "reservation_failed",
                    "critical",
                    "A media reservation failed before dispatch.",
                    account=account_id,
                    job=job_id,
                    model=model,
                    modality=job_type,
                )
                return {"ok": False, "reserved": 0, "status": "reservation_failed",
                        "reason": "billing reservation failed"}
            await s.commit()
            return {"ok": True, "reserved": cost, "status": "ok",
                    "from_promo": from_promo, "from_free": from_free}

    if remainder > 0:
        status = await debit(account_id, remainder, reason=f"reserve:{job_type}", ref=ref, model=model)
    else:
        status = "ok"
    if status in ("ok", "already"):
        return {"ok": True, "reserved": cost, "status": status,
                "from_promo": from_promo, "from_free": from_free}
    await _promo_release(account_id, ref)
    await free_credits.release(account_id, ref)
    await service_limits.release((user or {}).get("service_id"), ref)
    logger.info("[charge:402] account=%s model=%s reserve=%d micro-USD (free=%d): insufficient",
                opaque_id(account_id), model, cost, from_free)
    return {"ok": False, "reserved": 0, "status": "insufficient", "reason": "insufficient credits"}


async def refund_reservation(account_id, reserved_micro: int, job_id) -> None:
    """Refund a media reservation when the job didn't run (fault / timeout / 429).
    Free-aware: the free portion (recorded under the job ref) goes back to the
    day's allowance; only the paid remainder is credited. Best-effort +
    idempotent on the `:refund` ref and the free ref; never raises."""
    if not account_id or reserved_micro <= 0:
        return
    try:
        promo_held = await promotions.held_micro(account_id, str(job_id))
        await _promo_release(account_id, str(job_id))
        freed = await free_credits.release(account_id, str(job_id))
        paid_portion = max(int(reserved_micro) - promo_held - int(freed or 0), 0)
        if paid_portion > 0:
            await credit(account_id, paid_portion, reason="refund:media", ref=f"{job_id}:refund")
    except Exception as exc:
        logger.error(
            "media refund failed account=%s job=%s error_type=%s (refund owed)",
            opaque_id(account_id),
            opaque_id(job_id),
            error_type(exc),
        )
        _economic_alert(
            "media_refund_failed",
            "critical",
            "A failed media job could not be refunded automatically.",
            account=account_id,
            job=job_id,
        )


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
    No-op when no positive hold was created."""
    if not account_id or reserved_micro <= 0:
        return
    try:
        async with await new_session() as s:
            try:
                await _insert_reservation_in_session(s, job_id, account_id, model, reserved_micro, prompt_toks)
                await s.commit()
            except IntegrityError:
                await s.rollback()  # already opened (retry/requeue) — keep the original
    except Exception as exc:
        logger.error(
            "open_reservation failed job=%s error_type=%s (settlement context missing)",
            opaque_id(job_id),
            error_type(exc),
        )
        _economic_alert(
            "reservation_open_failed",
            "critical",
            "A durable reservation context could not be opened.",
            account=account_id,
            job=job_id,
            model=model,
        )


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
    free_restore = None  # (account_id, keep_micro) — applied AFTER the SQL commit
    promo_restore = None  # (account_id, keep_micro) — durable promo pocket
    service_reconcile = None  # (service_id, actual_micro) — Redis after commit
    try:
        async with await new_session() as s:
            row = (await s.execute(
                sa.select(reservations_t.c.account_id, reservations_t.c.model,
                          reservations_t.c.reserved_micro, reservations_t.c.prompt_toks,
                          reservations_t.c.free_micro, reservations_t.c.promo_micro,
                          reservations_t.c.input_per_mtok_micro,
                          reservations_t.c.output_per_mtok_micro,
                          reservations_t.c.discount_bps,
                          reservations_t.c.service_id)
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
            from .identities import canonical_account_id
            paid_account_id = await canonical_account_id(aid, session=s) if aid else aid
            reserved, prompt_toks = int(row[2] or 0), int(row[3] or 0)
            free_held = int(row[4] or 0)
            promo_held = int(row[5] or 0)
            paid_held = reserved - free_held - promo_held
            service_id = row[9]

            if aid and reserved > 0:
                # Two pockets, never converted: the paid refund moves in THIS txn;
                # the free restore is a Redis op queued for after commit (a crash
                # between them forfeits free-day allowance, never paid money).
                if status != "ok":
                    service_reconcile = (service_id, 0)
                    if paid_held > 0:
                        await _credit_in_session(s, paid_account_id, paid_held, "release:failed", f"{job_id}:refund", model)
                    if free_held > 0:
                        free_restore = (aid, 0)  # full free release
                    if promo_held > 0:
                        promo_restore = (aid, 0)
                else:
                    if row[6] is not None and row[7] is not None:
                        actual = _quote_snapshot(
                            prompt_toks, completion_tokens, int(row[6]), int(row[7]), int(row[8] or 0),
                        )
                    else:
                        actual = pricing.quote_text(model, prompt_toks, int(completion_tokens or 0))
                    service_reconcile = (service_id, actual)
                    # Attribute consumption promo → daily free → paid, matching reserve.
                    promo_spent = min(promo_held, actual)
                    after_promo = actual - promo_spent
                    free_spent = min(free_held, after_promo)
                    paid_spent = after_promo - free_spent
                    if promo_held > promo_spent:
                        promo_restore = (aid, promo_spent)
                    if free_held > free_spent:
                        free_restore = (aid, free_spent)  # keep the spent part consumed
                    paid_diff = paid_held - paid_spent
                    if paid_diff > 0:
                        await _credit_in_session(s, paid_account_id, paid_diff, "reconcile:refund", f"{job_id}:refund", model)
                    elif paid_diff < 0:
                        extra_ok = await _try_extra_debit_in_session(s, paid_account_id, -paid_diff, f"{job_id}:extra", model)
                        if not extra_ok:
                            logger.warning("settle under-collected account=%s job=%s by %d micro-USD (insufficient)",
                                           opaque_id(aid), opaque_id(job_id), -paid_diff)
                            _economic_alert(
                                "settlement_under_collected",
                                "critical",
                                "A completed request exceeded its reservation and the shortfall could not be collected.",
                                account=aid,
                                job=job_id,
                                model=model,
                                shortfall_micro=-paid_diff,
                            )
            await s.commit()
        if free_restore is not None:
            await free_credits.release(free_restore[0], str(job_id), keep_micro=free_restore[1])
        if promo_restore is not None:
            await _promo_release(promo_restore[0], str(job_id), keep_micro=promo_restore[1])
        if service_reconcile is not None:
            await service_limits.reconcile(service_reconcile[0], str(job_id), service_reconcile[1])
    except Exception as exc:
        logger.error(
            "settle_job failed job=%s error_type=%s (refund may be owed)",
            opaque_id(job_id),
            error_type(exc),
        )
        _economic_alert(
            "settlement_failed",
            "critical",
            "Durable job settlement failed; the hold remains recoverable.",
            job=job_id,
        )


async def settle_exact(job_id) -> None:
    """Terminal settlement for a job whose cost was reserved EXACTLY (media): the
    held amount already equals the charge, so the reservation simply stands — flip
    held→settled with no ledger movement. Exactly-once via the conditional UPDATE;
    a no-op on a duplicate terminal or unknown job. Best-effort; never raises."""
    try:
        promo_finalize = None
        async with await new_session() as s:
            row = (await s.execute(
                sa.select(reservations_t.c.account_id, reservations_t.c.promo_micro)
                .where(reservations_t.c.job_id == str(job_id))
            )).first()
            res = await s.execute(
                sa.update(reservations_t)
                .where(sa.and_(reservations_t.c.job_id == str(job_id),
                               reservations_t.c.status == "held"))
                .values(
                    status="settled",
                    settled=_now(),
                    actual_micro=reservations_t.c.reserved_micro,
                ),
            )
            await s.commit()
            if res.rowcount == 0:
                return  # already settled / unknown job
            if row and row[0] and int(row[1] or 0) > 0:
                promo_finalize = (row[0], int(row[1] or 0))
        if promo_finalize:
            await _promo_release(promo_finalize[0], str(job_id), keep_micro=promo_finalize[1])
    except Exception as exc:
        logger.error(
            "settle_exact failed job=%s error_type=%s",
            opaque_id(job_id),
            error_type(exc),
        )
        _economic_alert(
            "settlement_failed",
            "critical",
            "Exact-cost media settlement failed; the hold remains recoverable.",
            job=job_id,
        )


async def release_job(job_id) -> None:
    """Terminal release for a job that produced nothing billable (client error,
    worker fault surfaced, give-up). Full refund of exactly one held demand or
    compensated-audit reservation, once."""
    try:
        audit_release = False
        async with await new_session() as s:
            await validator_audit_budgets.lock_job_in_session(s, job_id)
            audit = await validator_audit_budgets.maybe_audit_for_job_in_session(
                s,
                job_id,
                for_update=True,
            )
            demand = await s.scalar(
                sa.select(sa.literal(True)).where(
                    sa.exists(
                        sa.select(reservations_t.c.job_id).where(
                            reservations_t.c.job_id == str(job_id),
                        ),
                    ),
                ),
            )
            if audit and demand:
                raise RuntimeError("job has both demand and compensated-audit reservations")
            if audit:
                await validator_audit_budgets.release_audit_in_session(
                    s,
                    job_id=job_id,
                    failure_code="ordinary_terminal_failure",
                )
                await s.commit()
                audit_release = True
            else:
                await s.rollback()
        if not audit_release:
            await settle_job(job_id, 0, status="failed")
    except Exception as exc:
        logger.error(
            "release_job failed job=%s error_type=%s (hold remains recoverable)",
            opaque_id(job_id),
            error_type(exc),
        )
        _economic_alert(
            "authorization_release_failed",
            "critical",
            "A terminal failure could not release its exclusive authorization hold.",
            job=job_id,
        )


async def record_and_settle(*, ledger_values: dict, completion_tokens: int = 0,
                            exact: bool = False) -> str:
    """ATOMIC terminal for a SUCCESSFUL job: write the worker-payout ledger row
    AND settle the exclusive demand or compensated-audit reservation in ONE
    transaction — both commit or neither.

    This closes the window where a crash between the (separate) ledger write and
    settlement could leave a paid worker with a still-`held`, later-refunded
    reservation (or charge the user while the worker payout row was lost).

    `ledger_values` are the record_completion_in_session kwargs. `exact=True`
    (media) lets the exact reserve stand; otherwise reconcile against
    `completion_tokens` (text/passthrough). Returns:
      'duplicate'       — job already in grid_ledger (double dispatch) → nothing done
      'settled'         — ledger written + reservation reconciled (paid success)
      'audit_settled'   — ledger written + audit budget consumed (paid success)
      'no_reservation'  — ledger written; no held row (dry-run / legacy / free)
      'stale_no_payout' — reservation existed but already closed → rolled back, no
                          ledger row, no payout, no charge
      'audit_manual_review' — a pre-existing payout row conflicted with a held
                              audit; budget remains quarantined for review
      'error'           — nothing committed (retryable; the sweeper recovers)
    Best-effort: never raises into the worker loop."""
    from . import ledger as ledger_svc
    job_id = str(ledger_values["job_id"])
    try:
        async with await new_session() as s:
            await validator_audit_budgets.lock_job_in_session(s, job_id)
            row = (await s.execute(
                sa.select(reservations_t.c.account_id, reservations_t.c.model,
                          reservations_t.c.reserved_micro, reservations_t.c.prompt_toks,
                          reservations_t.c.free_micro, reservations_t.c.promo_micro,
                          reservations_t.c.input_per_mtok_micro,
                          reservations_t.c.output_per_mtok_micro,
                          reservations_t.c.discount_bps,
                          reservations_t.c.service_id,
                          reservations_t.c.billing_source)
                .where(reservations_t.c.job_id == job_id)
                .with_for_update()
            )).first()
            audit = await validator_audit_budgets.maybe_audit_for_job_in_session(
                s,
                job_id,
                for_update=True,
            )
            if row and audit:
                raise RuntimeError("job has both demand and compensated-audit reservations")
            try:
                await ledger_svc.record_completion_in_session(s, **ledger_values)
            except IntegrityError:
                await s.rollback()
                return await validator_audit_budgets.reconcile_duplicate_terminal(
                    job_id=job_id,
                )

            if audit:
                audit_status = await validator_audit_budgets.settle_audit_in_session(
                    s,
                    job_id=job_id,
                    actual_units=validator_audit_budgets.den_to_units(ledger_values["den"]),
                    worker_id=ledger_values["worker_id"],
                    model=ledger_values["model"],
                    modality=ledger_values["job_type"],
                    request_hash=ledger_values.get("prompt_hash"),
                    result_hash=ledger_values.get("result_hash"),
                )
                if audit_status == "settled":
                    await s.commit()
                    return "audit_settled"
                await s.rollback()
                if audit_status in {"stale_no_payout", "duplicate"}:
                    return audit_status
                raise RuntimeError(f"unexpected audit terminal status: {audit_status}")

            if not row:
                await s.commit()  # ledger stands; nothing to settle (dry-run/legacy/free)
                return "no_reservation"

            aid, model = row[0], row[1]
            from .identities import canonical_account_id
            paid_account_id = await canonical_account_id(aid, session=s) if aid else aid
            reserved, prompt_toks = int(row[2] or 0), int(row[3] or 0)
            free_held = int(row[4] or 0)
            promo_held = int(row[5] or 0)
            free_restore = None  # (keep_micro) — Redis, applied after commit
            promo_restore = None
            service_keep = reserved
            billing_source = row[10] or "credits"
            actual = reserved
            if reserved > 0 and not exact:
                if row[6] is not None and row[7] is not None:
                    actual = _quote_snapshot(
                        prompt_toks, completion_tokens, int(row[6]), int(row[7]), int(row[8] or 0),
                    )
                else:
                    # Compatibility for reservations opened before migration 0015.
                    actual = pricing.quote_text(model, prompt_toks, int(completion_tokens or 0))
                if actual > reserved and billing_source == "x402":
                    _economic_alert(
                        "x402_settlement_under_authorized",
                        "critical",
                        "Grid-counted usage exceeded the payer's x402 authorization.",
                        job=job_id,
                        model=model,
                        actual_micro=actual,
                        authorized_micro=reserved,
                    )
                    actual = reserved
                service_keep = actual
            res = await s.execute(
                sa.update(reservations_t)
                .where(sa.and_(reservations_t.c.job_id == job_id,
                               reservations_t.c.status == "held"))
                .values(status="settled", settled=_now(), actual_micro=actual)
            )
            if res.rowcount == 0:
                # A reservation EXISTS but is no longer held — it was already
                # settled/released elsewhere (e.g. a timeout release or the
                # sweeper). The demand side is closed (likely refunded), so paying
                # the worker now would be unbalanced: ROLL BACK the ledger insert.
                # No payout, no charge — the wasted work is the tradeoff for the
                # race. (Caller logs; this never silently double-accounts.)
                await s.rollback()
                _economic_alert(
                    "late_success_no_payout",
                    "warning",
                    "A worker success arrived after its demand reservation was already closed.",
                    account=aid,
                    job=job_id,
                    model=model,
                )
                return "stale_no_payout"

            if aid and reserved > 0 and billing_source == "credits" and not exact:
                # Three pockets, never converted: promo → daily free → paid
                # (matching the draw); the paid refund/extra moves in THIS txn,
                # the free restore follows the commit (crash between = free-day
                # allowance forfeited, never paid money).
                promo_spent = min(promo_held, actual)
                after_promo = actual - promo_spent
                free_spent = min(free_held, after_promo)
                paid_held = reserved - promo_held - free_held
                paid_spent = after_promo - free_spent
                if promo_held > promo_spent:
                    promo_restore = promo_spent
                if free_held > free_spent:
                    free_restore = free_spent
                paid_diff = paid_held - paid_spent
                if paid_diff > 0:
                    await _credit_in_session(s, paid_account_id, paid_diff, "reconcile:refund", f"{job_id}:refund", model)
                elif paid_diff < 0:
                    ok = await _try_extra_debit_in_session(s, paid_account_id, -paid_diff, f"{job_id}:extra", model)
                    if not ok:
                        logger.warning("settle under-collected account=%s job=%s by %d micro-USD (insufficient)",
                                       opaque_id(aid), opaque_id(job_id), -paid_diff)
                        _economic_alert(
                            "settlement_under_collected",
                            "critical",
                            "An atomic terminal exceeded its hold and the shortfall could not be collected.",
                            account=aid,
                            job=job_id,
                            model=model,
                            shortfall_micro=-paid_diff,
                        )
            await s.commit()
            if exact and promo_held > 0:
                promo_restore = promo_held
            if free_restore is not None:
                await free_credits.release(aid, job_id, keep_micro=free_restore)
            if promo_restore is not None:
                await _promo_release(aid, job_id, keep_micro=promo_restore)
            await service_limits.reconcile(row[9], job_id, service_keep)
            return "settled"
    except Exception as exc:
        logger.error(
            "record_and_settle failed job=%s error_type=%s (terminal not committed; retryable)",
            opaque_id(job_id),
            error_type(exc),
        )
        _economic_alert(
            "atomic_terminal_failed",
            "critical",
            "Worker payout and demand settlement did not commit; the terminal is retryable.",
            job=job_id,
        )
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
    Returns the total number of reservations acted on. Existing holds are swept
    even after charging is disabled; the rollout mode controls new holds only.
    Idempotent: settle/release re-flip already-settled rows to a no-op."""
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
            if job_type in _EXACT_COST_JOB_TYPES:
                await settle_exact(jid)        # media: exact reserve stands
            else:
                await settle_job(jid, units)   # text/passthrough: charge grid usage
            settled += 1
    if job_ids:
        logger.warning("swept stale held reservations older than %ds: %d released, %d settled-from-ledger",
                       older_than_seconds, released, settled)
        _economic_alert(
            "stale_reservations_recovered",
            "warning",
            "The reservation sweeper recovered stale monetary holds.",
            released=released,
            settled=settled,
            age_seconds=older_than_seconds,
        )
    return released + settled


async def billing_health(held_warning_seconds: int = 900) -> dict[str, int | bool]:
    """Read-only economic invariants for the operator monitor.

    The purchased-balance cache must equal the append-only purchased ledger in
    aggregate. Promotional and daily-free pockets intentionally live elsewhere
    and are excluded from both sides of this invariant.
    """
    held_cutoff = _now() - _dt.timedelta(seconds=max(0, held_warning_seconds))
    async with await new_session() as s:
        balance_total = int(
            await s.scalar(sa.select(sa.func.coalesce(sa.func.sum(credits_t.c.balance_micro), 0)))
            or 0
        )
        ledger_total = int(
            await s.scalar(sa.select(sa.func.coalesce(sa.func.sum(ledger_t.c.delta_micro), 0)))
            or 0
        )
        negative_balances = int(
            await s.scalar(
                sa.select(sa.func.count()).select_from(credits_t).where(credits_t.c.balance_micro < 0),
            )
            or 0
        )
        stale_held = int(
            await s.scalar(
                sa.select(sa.func.count())
                .select_from(reservations_t)
                .where(
                    reservations_t.c.status == "held",
                    reservations_t.c.created < held_cutoff,
                ),
            )
            or 0
        )
        invalid_splits = int(
            await s.scalar(
                sa.select(sa.func.count())
                .select_from(reservations_t)
                .where(
                    reservations_t.c.free_micro + reservations_t.c.promo_micro
                    > reservations_t.c.reserved_micro,
                ),
            )
            or 0
        )
    return {
        "ok": (
            balance_total == ledger_total
            and negative_balances == 0
            and invalid_splits == 0
        ),
        "balance_total_micro": balance_total,
        "ledger_total_micro": ledger_total,
        "balance_delta_micro": balance_total - ledger_total,
        "negative_balances": negative_balances,
        "stale_held": stale_held,
        "invalid_reservation_splits": invalid_splits,
    }

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Custodial worker payouts (v1) — pay each EARNING ACCOUNT its pro-rata share of
a fixed per-period AIPG budget, by den, via direct ERC-20 transfers on Base.

Attribution is by ACCOUNT, not by a worker-supplied wallet string: a worker
authenticates with its account key, so its den belongs to the account. An account
with a `payout_wallet` (fallback login `wallet`) is paid now; one without is
recorded **accrued** (owed) and paid the moment it sets a wallet — nothing
strands. Same source of truth (grid_ledger den) as the future trustless on-chain
Merkle-claim rail (settlement/bot.py, still a scaffold).

Idempotent per (period_id, account_id) via grid_payouts. DRY-RUN by default;
`--send` executes transfers (needs SETTLEMENT_TREASURY_PK + BASE_RPC_URL).
"""

import argparse
import asyncio
import datetime as _dt
import logging
import math
import os
import uuid as _uuid
from decimal import ROUND_DOWN, Decimal
from functools import wraps

import sqlalchemy as sa

from ...config import get_settings
from ...database import close_database, init_database, new_session
from ...v2.schema import accounts as accounts_t
from ...v2.schema import payout_periods as periods_t
from ...v2.schema import payouts as payouts_t
from . import demand_rewards, payout_periods, sanctions
from .aggregate import aggregate_den_by_account, purchased_work_by_account, total_den_in_window

logger = logging.getLogger("grid_api.payouts")

AIPG_TOKEN_ADDRESS = os.getenv("AIPG_TOKEN_ADDRESS", "0xa1c0deCaFE3E9Bf06A5F29B7015CD373a9854608")
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "")
TREASURY_PK = os.getenv("SETTLEMENT_TREASURY_PK", "")  # funded AIPG sender; never logged
MIN_AIPG = float(os.getenv("PAYOUT_MIN_AIPG", "0.01"))
HOURLY_BUDGET = os.getenv("PAYOUT_HOURLY_BUDGET", "208.33")
# Versioned prospective policy: at most 0.5% of a period's emission budget
# across the whole SmolLM family and all accounts; clipped value stays unspent.
SMOLLM_EMISSION_CAP_BPS = 50

_ERC20_ABI = [
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
]


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


# ── pure math (no I/O) ───────────────────────────────────────────────────────

def compute_account_payouts(rows: list[dict], budget_aipg: float, *, min_aipg: float = MIN_AIPG,
                            conservative_rounding: bool = False) -> list[dict]:
    """Split `budget_aipg` across accounts pro-rata by den. Returns
    [{account_id, payout_address, den, share, aipg, payable}] sorted high→low,
    dropping sub-dust rows. `payable` = the account has a wallet (else it accrues).
    Emissions-funded bootstrap → the whole budget goes to supply (the 85/3/12
    revenue split is a revenue concept, not applicable to emissions).

    Prospective aggregation adds `smollm_den`. That family shares one network
    cap, including walletless accounts; excess is NOT redistributed. Historical
    aggregates without that field retain their original allocation arithmetic.
    """
    # Reject the whole input before returning any payable or accrued allocation.
    # In particular, a negative weight must not inflate another account's share.
    if not math.isfinite(budget_aipg) or budget_aipg < 0:
        raise ValueError("Invalid payout budget")
    if not math.isfinite(min_aipg) or min_aipg < 0:
        raise ValueError("Invalid payout minimum")
    for r in rows:
        den, small = float(r["den"]), float(r.get("smollm_den", 0))
        if not math.isfinite(den) or not math.isfinite(small) or not 0 <= small <= den:
            raise ValueError("Invalid payout weight")
    total_den = sum(float(r["den"]) for r in rows)
    if not math.isfinite(total_den):
        raise ValueError("Non-finite total payout weight")
    if total_den <= 0 or budget_aipg <= 0:
        return []
    capped = conservative_rounding or any("smollm_den" in row for row in rows)
    if capped:
        budget = Decimal(str(budget_aipg))
        total = sum(Decimal(str(r["den"])) for r in rows)
        small_total = sum(Decimal(str(r.get("smollm_den", 0))) for r in rows)
        if not budget.is_finite() or not total.is_finite() or not small_total.is_finite():
            raise ValueError("Non-finite payout inputs")
        small_scale = min(Decimal(1), Decimal(SMOLLM_EMISSION_CAP_BPS) * total / (10000 * small_total)) if small_total > 0 else Decimal(1)
    out = []
    for r in rows:
        share = float(r["den"]) / total_den
        aipg = budget_aipg * share
        if capped:
            den, small = Decimal(str(r["den"])), Decimal(str(r.get("smollm_den", 0)))
            if not den.is_finite() or not small.is_finite() or not 0 <= small <= den:
                raise ValueError("Invalid capped payout weight")
            eligible = den - small + small * small_scale
            amount = (budget * eligible / total).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            aipg, share = float(amount), float(eligible / total)
        if aipg < min_aipg:
            continue
        addr = r.get("payout_address")
        out.append({"account_id": r["account_id"], "payout_address": addr,
                    "den": float(r["den"]), "share": share, "aipg": round(aipg, 8),
                    "payable": bool(addr)})
    return sorted(out, key=lambda x: x["aipg"], reverse=True)


# ── period window ────────────────────────────────────────────────────────────

def _window(days, since, until):
    now = _now()
    end = _dt.datetime.fromisoformat(until) if until else now
    start = _dt.datetime.fromisoformat(since) if since else end - _dt.timedelta(days=days if days is not None else 1.0)
    return start, end, f"{start.date().isoformat()}_{end.date().isoformat()}"


# ── dry-run preview ──────────────────────────────────────────────────────────

def _demand_policy(start, end):
    policies = getattr(get_settings(), "worker_reward_demand_policies", [])
    if not policies or end <= policies[0].since:
        return None
    for policy in policies:
        if policy.since <= start and end <= policy.until:
            return {
                "since": policy.since.astimezone(_dt.UTC).isoformat(),
                "until": policy.until.astimezone(_dt.UTC).isoformat(),
                "price_micro_per_aipg": policy.price_micro_per_aipg,
                "worker_share_bps": policy.worker_share_bps,
            }
    raise ValueError("payout window lacks a reviewed demand reward policy")


async def _period_allocations(start, end, budget, *, policy):
    rows = await aggregate_den_by_account(start, end)
    pay = compute_account_payouts(rows, budget, conservative_rounding=policy is not None)
    if policy is not None:
        pay = demand_rewards.bound_allocations(
            pay, await purchased_work_by_account(start, end),
            price_micro_per_aipg=policy["price_micro_per_aipg"],
            worker_share_bps=policy["worker_share_bps"],
        )
        pay = [row for row in pay if row["aipg"] >= Decimal(str(MIN_AIPG))]
        for row in pay:
            row["share"] = float(row["aipg"] / Decimal(str(budget)))
    return rows, pay


async def preview_period(start, end, budget_aipg: float, *, period_id=None) -> dict:
    if period_id is not None:
        async with await new_session() as session:
            frozen = await session.scalar(sa.select(periods_t.c.period_id).where(periods_t.c.period_id == period_id))
        if frozen:
            expected = _period_contract(start, end, budget_aipg, period_id)

            async def never_reaggregate():
                raise RuntimeError("frozen payout preview cannot create a plan")

            plan = await payout_periods.freeze(period_id, expected, never_reaggregate)
            recorded = {str(row["account_id"]): row for row in await _planned_rows(period_id)}
            values = plan["allocations"]
            total = sum(row["den"] for row in values)
            rows = [dict(account_id=row["account_id"], den=row["den"],
                         payout_address=recorded[row["account_id"]]["address"], aipg=float(row["aipg"]),
                         status=recorded[row["account_id"]]["status"],
                         share=(float(Decimal(row["aipg"]) / Decimal(str(budget_aipg)))
                                if plan["contract"]["version"] == 2 and budget_aipg else
                                row["den"] / total if total else 0),
                         payable=bool(recorded[row["account_id"]]["address"])) for row in values]
            active = [row for row in rows if row["status"] in ("pending", "failed")]
            accrued = [row for row in rows if row["status"] == "accrued"]
            return dict(accounts=len(rows), total_den=total, budget_aipg=budget_aipg,
                        unallocated_aipg=float(Decimal(str(budget_aipg)) - sum((Decimal(row["aipg"]) for row in values), Decimal(0))),
                        payouts=rows, payable_now_aipg=sum(row["aipg"] for row in active),
                        n_payable=len(active), accrued_aipg=sum(row["aipg"] for row in accrued),
                        n_accrued=len(accrued), no_account_den=None, frozen=True,
                        demand_policy=plan["contract"].get("demand_policy"))
    policy = _demand_policy(start, end)
    rows, pay = await _period_allocations(start, end, budget_aipg, policy=policy)
    attributed = sum(float(r["den"]) for r in rows)
    no_account_den = round(max(0.0, await total_den_in_window(start, end) - attributed), 2)
    payable = [p for p in pay if p["payable"]]
    accrued = [p for p in pay if not p["payable"]]
    return {
        "accounts": len(rows),
        "total_den": attributed,
        "budget_aipg": budget_aipg,
        "unallocated_aipg": float(Decimal(str(budget_aipg)) - sum((Decimal(str(p["aipg"])) for p in pay), Decimal(0))),
        "demand_policy": policy,
        "payouts": pay,
        "payable_now_aipg": round(sum(p["aipg"] for p in payable), 4), "n_payable": len(payable),
        "accrued_aipg": round(sum(p["aipg"] for p in accrued), 4), "n_accrued": len(accrued),
        "no_account_den": no_account_den,   # truly unattributable (no account at all)
    }


# ── persistence (idempotent per period+account) ──────────────────────────────

def _as_uuid(v):
    """Coerce account_id to a uuid.UUID for queries against the Uuid column.
    Aggregation returns account_id as a str; comparing that to a sa.Uuid column
    crashes ('str' has no attribute 'hex') on sqlite/CI. Idempotent for UUIDs/None."""
    if v is None or isinstance(v, _uuid.UUID):
        return v
    try:
        return _uuid.UUID(str(v))
    except (ValueError, TypeError):
        return v


async def _row(period_id, account_id) -> dict | None:
    account_id = _as_uuid(account_id)
    async with await new_session() as s:
        r = (await s.execute(sa.select(payouts_t).where(
            payouts_t.c.period_id == period_id, payouts_t.c.account_id == account_id))).mappings().first()
        return dict(r) if r else None


async def _max_assigned_nonce(session=None) -> int:
    """Highest treasury nonce ever bound across worker and validator rails; one
    treasury account = one nonce space). Fresh assignments go above this so a new
    payment can't collide with one already in flight even when the chain's
    pending-nonce view is stale (e.g. during a Base outage)."""
    from ...v2.schema import payout_legs as legs_t
    from ...v2.schema import validator_compensation_payments as validator_payments
    if session is None:
        async with await new_session() as s:
            return await _max_assigned_nonce(s)
    values = [
        (await session.execute(sa.select(sa.func.max(table.c.nonce)))).scalar()
        for table in (payouts_t, legs_t, validator_payments)
    ]
    return max(int(value) if value is not None else -1 for value in values)


async def _write(period_id, account_id, *, address, den, aipg, status,
                 tx_hash=None, nonce=None, paid=False, set_tx=True):
    """Upsert a payout row. set_tx=False preserves the existing tx_hash (used when
    marking sent via the nonce check, where the winning hash may differ)."""
    account_id = _as_uuid(account_id)
    async with await new_session() as s:
        existing = (await s.execute(sa.select(payouts_t).where(
            payouts_t.c.period_id == period_id, payouts_t.c.account_id == account_id).with_for_update())).mappings().first()
        if existing:
            if Decimal(str(aipg)) != existing["aipg_amount"] or float(den) != existing["den"]:
                raise ValueError("cannot change a recorded payout allocation")
            old_address = payout_periods.wallet(existing["address"])
            new_address = payout_periods.wallet(address)
            binding = (old_address is None and existing["status"] == "accrued"
                       and existing["nonce"] is None and existing["tx_hash"] is None)
            if old_address != new_address and not binding:
                raise ValueError("cannot redirect a recorded payout")
            if existing["nonce"] is not None and nonce is not None and nonce != existing["nonce"]:
                raise ValueError("cannot rebind a payout nonce")
            if existing["status"] in ("sent", "confirmed") and status != existing["status"]:
                raise ValueError("cannot reopen a completed payout")
        vals = dict(address=address, den=den, aipg_amount=aipg, status=status)
        if set_tx:
            vals["tx_hash"] = tx_hash
        if nonce is not None:
            vals["nonce"] = nonce
        if paid:
            vals["paid"] = _now()
        if existing:
            await s.execute(sa.update(payouts_t).where(payouts_t.c.id == existing["id"]).values(**vals))
        else:
            await s.execute(sa.insert(payouts_t).values(
                period_id=period_id, account_id=account_id, created=_now(), **vals))
        await s.commit()


# ── send (gated, idempotent) ─────────────────────────────────────────────────

def _w3():
    if not (BASE_RPC_URL and TREASURY_PK):
        raise RuntimeError("SETTLEMENT_TREASURY_PK + BASE_RPC_URL required to send")
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    acct = w3.eth.account.from_key(TREASURY_PK)
    token = w3.eth.contract(address=Web3.to_checksum_address(AIPG_TOKEN_ADDRESS), abi=_ERC20_ABI)
    return Web3, w3, acct, token


def _ctx():
    """(Web3, w3, acct, token, decimals) — the on-chain handle bundle for sends."""
    Web3, w3, acct, token = _w3()
    if w3.eth.chain_id != 8453:
        raise RuntimeError("payout RPC must be on Base")
    return (Web3, w3, acct, token, token.functions.decimals().call())


def _signed_transfer(Web3, w3, acct, token, decimals, to_addr, aipg, nonce, attempt):
    amount_wei = _token_units(aipg, decimals)
    # Tip escalates per attempt so a retry actually REPLACES a stuck tx at the
    # same nonce; maxFee derives from the live base fee so a Base spike can't
    # reject it. Base base fees are tiny → still sub-cent.
    priority = w3.to_wei(0.05 * (2 ** min(attempt, 4)), "gwei")
    try:
        base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    except Exception:
        base_fee = w3.to_wei(0.1, "gwei")
    tx = token.functions.transfer(Web3.to_checksum_address(to_addr), amount_wei).build_transaction({
        "from": acct.address, "nonce": nonce, "gas": 120000,
        "maxFeePerGas": base_fee * 5 + priority, "maxPriorityFeePerGas": priority,
    })
    return acct.sign_transaction(tx)


def _token_units(value, decimals):
    units = Decimal(str(value)) * (10 ** decimals)
    if not units.is_finite() or units < 0 or units != units.to_integral_value():
        raise ValueError("invalid payout token precision")
    return int(units)


def _hx(x) -> str:
    s = x.hex() if hasattr(x, "hex") else str(x)
    return s.lower()[2:] if s.lower().startswith("0x") else s.lower()


def _receipt_proves_transfer(w3, rec, expected_to, expected_wei) -> bool:
    """PROOF from a receipt: mined with status==1 AND it emitted an AIPG
    Transfer(_, expected_to, expected_wei). status==1 alone is NOT proof — a
    success receipt can carry no/other logs; only the matching Transfer counts."""
    if not rec or rec.get("status") != 1:
        return False
    topic = _hx(w3.keccak(text="Transfer(address,address,uint256)"))
    to_pad = ("0" * 24) + expected_to.lower().replace("0x", "")
    for lg in rec.get("logs", []):
        try:
            if lg["address"].lower() != AIPG_TOKEN_ADDRESS.lower():
                continue
            topics = [_hx(t) for t in lg["topics"]]
            if len(topics) >= 3 and topics[0] == topic and topics[2] == to_pad:
                if int(_hx(lg["data"]) or "0", 16) == expected_wei:
                    return True
        except Exception:
            continue
    return False


def _verify_transfer(w3, tx_hash, expected_to, expected_wei) -> bool:
    """Fetch tx_hash's receipt and prove it carried the expected AIPG Transfer.
    Per-hash receipt lookup (reliable) — never a getLogs range scan."""
    if not tx_hash:
        return False
    try:
        rec = w3.eth.get_transaction_receipt(tx_hash if str(tx_hash).startswith("0x") else "0x" + str(tx_hash))
    except Exception:
        return False
    return _receipt_proves_transfer(w3, rec, expected_to, expected_wei)


async def _settle_one(ctx, *, period_id, account_id, address, den, aipg,
                      stored_nonce, stored_tx=None, attempt=0) -> str:
    """Idempotently settle ONE (period, account) payout. Double-pay-proof: the
    payout is BOUND to a nonce; once that nonce is consumed we never re-send at a
    new one (only replace at the bound nonce). We mark 'sent' ONLY after PROVING
    the on-chain Transfer — a consumed nonce we can't prove becomes 'manual_review'
    (never auto-'sent', never re-sent). Screening holds also return 'manual_review'
    without signing or broadcasting; that state fits the existing status column."""
    Web3, w3, acct, token, decimals = ctx
    mined = w3.eth.get_transaction_count(acct.address)  # 'latest' = mined count

    # (1) Bound nonce already consumed → require on-chain PROOF before settling.
    if stored_nonce is not None and mined > stored_nonce:
        expected_wei = _token_units(aipg, decimals)
        if _verify_transfer(w3, stored_tx, address, expected_wei):
            await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                         status="sent", nonce=stored_nonce, paid=True, set_tx=False)
            return "sent"
        # Nonce gone but the expected transfer can't be proven (revert / replacement
        # we didn't record / unrelated tx). Do NOT re-send and do NOT call it paid.
        await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                     status="manual_review", nonce=stored_nonce, set_tx=False)
        logger.error("payout nonce consumed without transfer proof: manual_review")
        return "manual_review"

    # Every sender, including accrued/retry paths, reaches this gate. Already
    # mined proof above records history; screening here governs NEW broadcasts.
    blocked = sanctions.payable_status(await sanctions.screen(address))
    if blocked:
        await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                     status="manual_review", nonce=stored_nonce, set_tx=False)
        logger.warning("payout held before broadcast: %s", blocked)
        return "manual_review"

    # (2) Nonce to use: reuse the bound one (replacement) or assign a fresh,
    # collision-proof one (above both the chain's pending view and any nonce we've
    # ever assigned — survives a stale mempool during an outage).
    if stored_nonce is not None:
        nonce = stored_nonce
    else:
        nonce = max(w3.eth.get_transaction_count(acct.address, "pending"),
                    (await _max_assigned_nonce()) + 1)

    signed = _signed_transfer(Web3, w3, acct, token, decimals, address, aipg, nonce, attempt)
    h = signed.hash  # HexBytes — used for the RPC receipt wait

    # (3) Record pending + the BOUND nonce BEFORE broadcast (crash-safe: a crash
    # mid-send still leaves the nonce recorded so reconcile can resolve it).
    await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                 status="pending", tx_hash=h.hex(), nonce=nonce)

    # (4) Broadcast. "already known"/"nonce too low"/"replacement underpriced" are
    # NOT errors — they mean a tx for this nonce is already in flight or mined;
    # the nonce check resolves it. Any other error → failed (retryable).
    try:
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as e:
        msg = str(e).lower()
        if not any(k in msg for k in ("already known", "nonce too low",
                                      "replacement transaction underpriced")):
            await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                         status="failed", nonce=nonce, set_tx=False)
            return "failed"

    # (5) Confirm (short) — and PROVE the Transfer, never trust status==1 alone.
    #     mined+Transfer→sent ; revert→failed ; mined-but-no-matching-Transfer→
    #     manual_review (don't claim paid) ; timeout→stay 'pending'.
    try:
        rec = w3.eth.wait_for_transaction_receipt(h, timeout=90, poll_latency=2)
        if rec.get("status") != 1:
            await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                         status="failed", tx_hash=h.hex(), nonce=nonce)
            return "failed"
        expected_wei = _token_units(aipg, decimals)
        if _receipt_proves_transfer(w3, rec, address, expected_wei):
            await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                         status="sent", tx_hash=h.hex(), nonce=nonce, paid=True)
            return "sent"
        await _write(period_id, account_id, address=address, den=den, aipg=aipg,
                     status="manual_review", tx_hash=h.hex(), nonce=nonce)
        logger.error("payout receipt lacks matching Transfer: manual_review")
        return "manual_review"
    except Exception:
        return "pending"  # UNKNOWN — not failed. The nonce check settles it next run.


def _serialized_sender(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        async with await new_session() as session, session.begin():
            if not await _try_payout_lock(session):
                raise RuntimeError("another payout sender holds the treasury lock")
            return await function(*args, **kwargs)
    return wrapped


def _period_contract(start, end, budget, period_id):
    contract = payout_periods.contract(
        start, end, budget, period_id,
        cutoff=get_settings().worker_rewards_paid_only_since,
        hourly_cap=HOURLY_BUDGET, token=AIPG_TOKEN_ADDRESS, now=_now(),
    )
    contract["smollm_cap_bps"] = SMOLLM_EMISSION_CAP_BPS
    contract["minimum_aipg"] = format(payout_periods.amount(MIN_AIPG), ".8f")
    policy = _demand_policy(start, end)
    if policy is not None:
        contract["version"] = 2
        contract["demand_policy"] = policy
    return contract


async def _planned_rows(period_id):
    return await payout_periods.load_rows(period_id, _period_contract)


async def send_period(start, end, budget_aipg: float, period_id: str) -> dict:
    """Freeze and send a bounded, closed UTC hour; never reprice on retry."""
    expected = _period_contract(start, end, budget_aipg, period_id)
    return await _send_period_locked(start, end, budget_aipg, period_id, expected)


@_serialized_sender
async def _send_period_locked(start, end, budget_aipg, period_id, expected):

    async def build():
        _, allocations = await _period_allocations(start, end, budget_aipg, policy=expected.get("demand_policy"))
        return allocations

    await payout_periods.freeze(period_id, expected, build)
    pay = await _planned_rows(period_id)
    counts = {"sent": 0, "pending": 0, "accrued": 0, "skipped": 0, "failed": 0}
    ctx = _ctx() if (any(p["address"] and p["status"] in ("pending", "failed") for p in pay)
                    and BASE_RPC_URL and TREASURY_PK) else None
    for p in pay:
        if p["status"] not in ("pending", "failed", "accrued"):
            counts["skipped"] += 1
            continue
        if not p["address"]:
            counts["accrued"] += 1
            continue
        if ctx is None:
            counts["failed"] += 1  # has a wallet but no treasury configured → can't send
            continue
        try:
            st = await _settle_one(ctx, period_id=period_id, account_id=p["account_id"],
                                   address=p["address"], den=p["den"], aipg=p["aipg_amount"],
                                   stored_nonce=p["nonce"], stored_tx=p["tx_hash"])
            counts[st] = counts.get(st, 0) + 1
        except Exception as e:
            logger.error("payout failed: %s", type(e).__name__)
            counts["failed"] += 1
    return {"period_id": period_id, **counts}


@_serialized_sender
async def pay_accrued() -> dict:
    """Pay every 'accrued' balance whose account NOW has a wallet — the 'pay later'
    path. Run after operators connect a payout wallet in the console."""
    async with await new_session() as s:
        rows = (await s.execute(
            sa.select(payouts_t.c.period_id, payouts_t.c.account_id, payouts_t.c.den,
                      payouts_t.c.aipg_amount, payouts_t.c.nonce, payouts_t.c.tx_hash,
                      sa.func.coalesce(sa.func.nullif(accounts_t.c.payout_wallet, ""),
                                       sa.func.nullif(accounts_t.c.wallet, "")).label("addr"))
            .select_from(payouts_t.join(accounts_t, accounts_t.c.id == payouts_t.c.account_id)
                         .join(periods_t, periods_t.c.period_id == payouts_t.c.period_id))
            .where(payouts_t.c.status == "accrued")
        )).all()
    targets = [r for r in rows if r.addr]
    if not targets:
        return {"paid": 0, "still_accrued": len(rows)}
    paid = 0
    for period in {r.period_id for r in targets}:
        await _planned_rows(period)
    ctx = _ctx()
    for r in targets:
        try:
            address = payout_periods.wallet(r.addr)
            await _write(r.period_id, r.account_id, address=address, den=r.den,
                         aipg=r.aipg_amount, status="pending", set_tx=False)
            st = await _settle_one(ctx, period_id=r.period_id, account_id=r.account_id,
                                   address=address, den=float(r.den or 0),
                                   aipg=r.aipg_amount, stored_nonce=r.nonce,
                                   stored_tx=r.tx_hash)
            if st == "sent":
                paid += 1
        except Exception as e:
            logger.error("accrued payout failed: %s", type(e).__name__)
    return {"paid": paid, "still_accrued": len(rows) - paid}


@_serialized_sender
async def reconcile_and_retry() -> dict:
    """Resolve in-flight ('pending') and genuinely-failed payouts idempotently.
    Each row runs through _settle_one, which FIRST checks whether its bound nonce
    already mined (→ settle, no re-send) and otherwise replaces at that same nonce.
    This is what makes a chain outage / dropped tx unable to double-pay: a payout
    is never re-sent at a new nonce once it has a bound one. Safe to run every hour."""
    async with await new_session() as s:
        rows = (await s.execute(
            sa.select(payouts_t.c.period_id, payouts_t.c.account_id, payouts_t.c.address,
                      payouts_t.c.aipg_amount, payouts_t.c.den, payouts_t.c.nonce, payouts_t.c.tx_hash)
            .select_from(payouts_t.join(periods_t, periods_t.c.period_id == payouts_t.c.period_id))
            .where(payouts_t.c.status.in_(("pending", "failed")), payouts_t.c.address.isnot(None))
        )).all()
    if not rows:
        return {"settled": 0, "pending": 0, "failed": 0, "manual_review": 0}
    out = {"settled": 0, "pending": 0, "failed": 0, "manual_review": 0}
    for period in {r.period_id for r in rows}:
        await _planned_rows(period)
    ctx = _ctx()
    for r in rows:
        try:
            st = await _settle_one(ctx, period_id=r.period_id, account_id=r.account_id,
                                   address=r.address, den=float(r.den or 0),
                                   aipg=r.aipg_amount, stored_nonce=r.nonce,
                                   stored_tx=r.tx_hash, attempt=1)
            out["settled" if st == "sent" else st] += 1
        except Exception as e:
            out["failed"] += 1
            logger.error("payout reconciliation failed: %s", type(e).__name__)
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print_preview(pv, period_id):
    print(f"\n=== payout preview — period {period_id} ===")
    if pv.get("frozen"):
        print("Frozen allocation; no aggregation or repricing on replay.")
    print(f"accounts={pv['accounts']}  total_den={pv['total_den']:.2f}  budget={pv['budget_aipg']:.2f} AIPG")
    print(f"payable now: {pv['payable_now_aipg']:.4f} AIPG to {pv['n_payable']} acct(s) | "
          f"ACCRUED (no wallet yet): {pv['accrued_aipg']:.4f} AIPG owed to {pv['n_accrued']} acct(s)")
    if pv.get("no_account_den"):
        print(f"den with NO account at all (unattributable): {pv['no_account_den']:.2f}")
    print(f"{'account_id':38}{'den':>12}{'share':>9}{'AIPG':>14}  wallet")
    for p in pv["payouts"]:
        tag = (p['payout_address'][:10] + '…') if p['payable'] else 'ACCRUED (set wallet)'
        if p.get("status"):
            tag += f" [{p['status']}]"
        print(f"{p['account_id']:38}{p['den']:>12.2f}{p['share']*100:>8.2f}%{p['aipg']:>14.4f}  {tag}")
    print()


_PAYOUT_LOCK_KEY = 9123847  # fixed pg advisory-lock key — serializes payout runners


async def _try_payout_lock(session) -> bool:
    """Serialize payout runners via a Postgres advisory lock (non-blocking), so two
    runs can't allocate nonces or send concurrently. Non-Postgres (sqlite tests) is
    single-process → treat as acquired."""
    if session.get_bind().dialect.name == "sqlite":
        return True
    if session.get_bind().dialect.name != "postgresql":
        raise RuntimeError("payout lock requires PostgreSQL")
    return bool((await session.execute(
        sa.text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _PAYOUT_LOCK_KEY})).scalar())


async def _amain():
    ap = argparse.ArgumentParser(description="Custodial AIPG worker payouts (account-based, dry-run by default).")
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--since"); ap.add_argument("--until"); ap.add_argument("--period-id")
    ap.add_argument("--budget", type=float, help="total AIPG to distribute this period")
    ap.add_argument("--send", action="store_true", help="EXECUTE transfers (default: dry-run)")
    ap.add_argument("--pay-accrued", action="store_true", help="pay all accrued balances whose account now has a wallet")
    ap.add_argument("--retry-failed", action="store_true", help="reconcile pending + retry failed payouts (nonce-bound, idempotent)")
    a = ap.parse_args()
    await init_database()
    try:
        if a.retry_failed:
            print("reconciling pending + failed payouts ...", await reconcile_and_retry()); return
        if a.pay_accrued:
            print("paying accrued balances ...", await pay_accrued()); return
        if a.budget is None:
            ap.error("--budget is required (or use --pay-accrued)")
        start, end, pid = _window(a.days, a.since, a.until)
        if a.period_id:
            pid = a.period_id
        _print_preview(await preview_period(start, end, a.budget, period_id=pid), pid)
        if a.send:
            print(f"SENDING for {pid} ...", await send_period(start, end, a.budget, pid))
        else:
            print("(dry-run — re-run with --send to execute)")
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(_amain())

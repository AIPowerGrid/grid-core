# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Roll up the den ledger into per-wallet totals for a settlement period.

The on-chain settlement bot calls this to turn the durable grid_ledger
(written per completed job in worker_ws.py via services.ledger) into the
[(wallet, total_den)] list it commits as a Merkle root and pays out.

NOTE: this reads `grid_ledger` (the v2 source of truth, column `wallet`). It
previously read an orphan `grid_den_events` table that nothing ever wrote to,
so every aggregation returned zero rows — i.e. settlement would have paid
nobody. Keep this pointed at the same table services.ledger writes.

Period boundaries are [start, end) UTC half-open intervals so adjacent
periods never double-count a job on the boundary.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa

from ...database import new_session
from ...config import get_settings
from ...v2.schema import accounts as accounts_table
from ...v2.schema import credit_ledger as credit_ledger_table
from ...v2.schema import ledger as ledger_table
from ...v2.schema import reservations as reservations_table
from ...v2.schema import workers as workers_table
from ...v2.schema import x402_payments as x402_payments_table


def _reservation_matches_job():
    # Both persisted UUID spellings are supported without applying a function
    # to the reservation primary key (which would force a scan for every job).
    raw = sa.func.replace(sa.cast(ledger_table.c.job_id, sa.String()), "-", "")
    canonical = (sa.func.substr(raw, 1, 8).concat("-")
                 .concat(sa.func.substr(raw, 9, 4)).concat("-")
                 .concat(sa.func.substr(raw, 13, 4)).concat("-")
                 .concat(sa.func.substr(raw, 17, 4)).concat("-")
                 .concat(sa.func.substr(raw, 21, 12)))
    return reservations_table.c.job_id.in_((raw, canonical))


def _purchased_den():
    """Purchased fraction shared by payout eligibility and its read-only monitor."""
    r = reservations_table.c
    purchased = sa.case(
        (r.billing_source == "x402", r.actual_micro),
        else_=sa.case(
            (r.actual_micro > r.free_micro + r.promo_micro,
             r.actual_micro - r.free_micro - r.promo_micro),
            else_=0,
        ),
    )
    fraction = (
        sa.select(sa.cast(purchased, sa.Float) / sa.func.nullif(r.actual_micro, 0))
        .where(
            _reservation_matches_job(),
            r.status == "settled",
            r.billing_source.in_(("credits", "x402")),
            sa.or_(r.billing_source == "x402", r.account_id.isnot(None)),
            r.actual_micro > 0,
            r.reserved_micro > 0,
            r.free_micro >= 0,
            r.promo_micro >= 0,
            r.free_micro + r.promo_micro <= r.reserved_micro,
        )
        .correlate(ledger_table)
        .scalar_subquery()
    )
    return ledger_table.c.den * sa.func.coalesce(fraction, 0.0)


def _reward_den():
    """Purchased share of DEN after the explicit prospective policy boundary.

    A mixed-pocket job contributes only its purchased fraction, not its whole
    DEN for a nominal paid remainder. Free/promo and unreserved work need a
    separate capped subsidy allocator and never enter this unrestricted pool.
    Old rows retain their original DEN; no economic history is rewritten.
    """
    since = get_settings().worker_rewards_paid_only_since
    if since is None:
        return ledger_table.c.den
    return sa.case(
        (ledger_table.c.created < since, ledger_table.c.den),
        else_=_purchased_den(),
    )


def _funded_job():
    """Exclude x402 work until its on-chain USDC settlement is durable.

    This proof gate applies before and after the paid-only policy boundary.
    `_reward_den` additionally removes unbilled/free DEN after that boundary.
    """
    unsettled_x402 = (
        sa.select(sa.literal(1))
        .select_from(
            reservations_table.join(
                x402_payments_table,
                x402_payments_table.c.job_id == reservations_table.c.job_id,
                isouter=True,
            ),
        )
        .where(
            _reservation_matches_job(),
            reservations_table.c.billing_source == "x402",
            sa.or_(
                x402_payments_table.c.job_id.is_(None),
                x402_payments_table.c.status != "settled",
            ),
        )
    )
    return ~sa.exists(unsettled_x402)


async def reward_backing_health(start: datetime, end: datetime) -> dict:
    """Observe unrestricted-pool exposure, not transfers, in one SQL snapshot.

    Include walletless account accruals. Legacy wallet-only rows are included
    for the wallet rail, but completely unattributable rows cannot be allocated.
    This neither reprices history nor authorizes paying any reported DEN.
    """
    exposure = (
        sa.select((_reward_den() - _purchased_den()).label("unbacked"))
        .select_from(ledger_table.outerjoin(workers_table, workers_table.c.id == ledger_table.c.worker_id))
        .where(
            ledger_table.c.created >= start, ledger_table.c.created < end,
            ledger_table.c.den > 0, _funded_job(),
            sa.or_(workers_table.c.account_id.isnot(None),
                   sa.func.trim(sa.func.coalesce(ledger_table.c.wallet, "")) != ""),
        )
        .subquery()
    )
    statement = sa.select(
        sa.func.count().label("unbacked_jobs"),
        sa.func.coalesce(sa.func.sum(exposure.c.unbacked), 0.0).label("unbacked_den"),
    ).where(exposure.c.unbacked > 0)
    async with await new_session() as session:
        row = (await session.execute(statement)).mappings().one()
    return {"unbacked_jobs": int(row["unbacked_jobs"]), "unbacked_den": float(row["unbacked_den"])}


async def aggregate_den_by_account(start: datetime, end: datetime, *, min_den: float = 0.0) -> list[dict]:
    """Roll up den per ACCOUNT for [start, end), resolved through the worker that
    earned it (grid_ledger.worker_id → grid_workers.account_id → grid_accounts).

    This is the payout-correct attribution: a worker authenticates with its
    account key, so its earnings belong to the account — payable to the account's
    `payout_wallet` (falling back to its login `wallet`) whenever that's set, now
    or later. Den with no resolvable account (legacy/no-account workers) is
    excluded here and surfaced by count_unattributed_den.

    Returns [{account_id, den, payout_address}] where payout_address is None when
    the account hasn't set a wallet yet (→ the caller ACCRUES that share)."""
    j = ledger_table.join(workers_table, workers_table.c.id == ledger_table.c.worker_id, isouter=True).join(
        accounts_table, accounts_table.c.id == workers_table.c.account_id, isouter=True,
    )
    since = get_settings().worker_rewards_paid_only_since
    capped_den = sa.literal(0.0)
    if since is not None:
        capped_den = sa.case(
            (sa.and_(ledger_table.c.created >= since,
                     sa.func.lower(ledger_table.c.model).contains("smollm")), _reward_den()),
            else_=0.0,
        )
    async with await new_session() as session:
        result = await session.execute(
            sa.select(
                workers_table.c.account_id.label("account_id"),
                accounts_table.c.payout_wallet.label("payout_wallet"),
                accounts_table.c.wallet.label("login_wallet"),
                sa.func.sum(_reward_den()).label("den"),
                sa.func.sum(capped_den).label("smollm_den"),
            )
            .select_from(j)
            .where(
                ledger_table.c.created >= start,
                ledger_table.c.created < end,
                workers_table.c.account_id.isnot(None),
                _funded_job(),
            )
            .group_by(workers_table.c.account_id, accounts_table.c.payout_wallet, accounts_table.c.wallet)
            .having(sa.func.sum(_reward_den()) > min_den),
        )
        out = []
        for row in result:
            addr = (row.payout_wallet or "").strip() or (row.login_wallet or "").strip() or None
            item = {"account_id": str(row.account_id), "den": float(row.den), "payout_address": addr}
            if since is not None and row.smollm_den > 0:
                item["smollm_den"] = float(row.smollm_den)
            out.append(item)
        return out


async def purchased_work_by_account(start: datetime, end: datetime) -> dict[str, int]:
    """Settled externally funded consumption served by each account.

    Deliberately separate from historical DEN arithmetic. Ambiguous UUID
    spellings and malformed/underfunded receipts supply no emission backing.
    This is accounting evidence, not proof of independent customer demand.
    """
    r = reservations_table.c
    purchased = sa.case(
        (r.billing_source == "x402", r.actual_micro),
        else_=r.actual_micro - r.free_micro - r.promo_micro,
    )
    c = credit_ledger_table.c
    job_movements = sa.and_(
        c.account_id == r.account_id,
        c.ref.in_([r.job_id, r.job_id.concat(":refund"), r.job_id.concat(":extra")]),
    )
    def consumed(column):
        return (sa.select(-sa.func.coalesce(sa.func.sum(sa.cast(column, sa.Numeric(38, 0))), 0))
                .where(job_movements).correlate(reservations_table).scalar_subquery())

    funded = consumed(c.funded_delta_micro)
    spent = consumed(c.delta_micro)
    # A label such as "purchased" or "usdc_deposit" is not funding proof.
    # Refunds reduce backing; missing/contradictory job movements earn none.
    backing = sa.case(
        (r.billing_source == "x402", purchased),
        (sa.and_(spent == purchased, funded >= 0, funded <= purchased), funded),
        else_=0,
    )
    matching_count = (
        sa.select(sa.func.count()).select_from(reservations_table)
        .where(_reservation_matches_job()).correlate(ledger_table).scalar_subquery()
    )
    paid_x402 = sa.exists(
        sa.select(sa.literal(1)).select_from(x402_payments_table).where(
            x402_payments_table.c.job_id == r.job_id,
            x402_payments_table.c.status == "settled",
            x402_payments_table.c.settled_micro >= r.actual_micro,
        )
    )
    stmt = (
        sa.select(workers_table.c.account_id,
                  sa.func.sum(sa.cast(backing, sa.Numeric(38, 0))).label("purchased_micro"))
        .select_from(ledger_table.join(workers_table, workers_table.c.id == ledger_table.c.worker_id)
                     .join(reservations_table, _reservation_matches_job()))
        .where(
            ledger_table.c.created >= start, ledger_table.c.created < end,
            ledger_table.c.den > 0, workers_table.c.account_id.isnot(None),
            matching_count == 1, r.status == "settled",
            r.actual_micro > 0, r.actual_micro <= r.reserved_micro,
            r.free_micro >= 0, r.promo_micro >= 0,
            r.free_micro + r.promo_micro <= r.actual_micro,
            sa.or_(sa.and_(r.billing_source == "credits", r.account_id.isnot(None)),
                   sa.and_(r.billing_source == "x402", paid_x402)),
        ).group_by(workers_table.c.account_id)
    )
    async with await new_session() as session:
        rows = (await session.execute(stmt)).mappings().all()
        return {str(row["account_id"]): int(row["purchased_micro"]) for row in rows}


async def total_den_in_window(start: datetime, end: datetime) -> float:
    """All den in [start, end), no attribution filter — for measuring how much
    truly has NO account (vs the per-account rollup which excludes account_id IS
    NULL). den_no_account = total - sum(per-account)."""
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(_reward_den()), 0.0)).where(
                    ledger_table.c.created >= start,
                    ledger_table.c.created < end,
                    _funded_job(),
                ),
            )
        ).first()
        return float(row[0] or 0.0)


async def aggregate_den_for_period(
    start: datetime,
    end: datetime,
    *,
    min_den: float = 0.0,
) -> list[dict]:
    """Return [{address, den}] for all wallets that earned den in [start, end).

    Rows with an empty wallet_address are excluded here — they need
    worker->user wallet resolution the bot does separately (or they were
    workers that never supplied a wallet and can't be paid until they do).
    `min_den` drops dust rows so a period isn't bloated by sub-threshold
    earners whose payout would round to zero on-chain anyway.
    """
    async with await new_session() as session:
        result = await session.execute(
            sa.select(
                ledger_table.c.wallet,
                sa.func.sum(_reward_den()).label("den"),
            )
            .where(
                ledger_table.c.created >= start,
                ledger_table.c.created < end,
                ledger_table.c.wallet != "",
                ledger_table.c.wallet.isnot(None),
                _funded_job(),
            )
            .group_by(ledger_table.c.wallet)
            .having(sa.func.sum(_reward_den()) > min_den),
        )
        return [{"address": row.wallet, "den": float(row.den)} for row in result]


async def count_unattributed_den(start: datetime, end: datetime) -> dict:
    """Diagnostic: how much den in the window has no wallet attached.

    The settlement bot logs this so the team can see how much earning is
    stranded for lack of a wallet (and chase those workers to set one).
    """
    async with await new_session() as session:
        result = await session.execute(
            sa.select(
                sa.func.count().label("jobs"),
                sa.func.coalesce(sa.func.sum(_reward_den()), 0).label("den"),
            ).where(
                ledger_table.c.created >= start,
                ledger_table.c.created < end,
                sa.or_(
                    ledger_table.c.wallet == "",
                    ledger_table.c.wallet.is_(None),
                ),
                _funded_job(),
                _reward_den() > 0,
            ),
        )
        row = result.first()
        return {"jobs": int(row.jobs), "den": float(row.den)}

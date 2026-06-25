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
from ...v2.schema import accounts as accounts_table
from ...v2.schema import ledger as ledger_table
from ...v2.schema import workers as workers_table


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
    j = (
        ledger_table
        .join(workers_table, workers_table.c.id == ledger_table.c.worker_id, isouter=True)
        .join(accounts_table, accounts_table.c.id == workers_table.c.account_id, isouter=True)
    )
    async with await new_session() as session:
        result = await session.execute(
            sa.select(
                workers_table.c.account_id.label("account_id"),
                accounts_table.c.payout_wallet.label("payout_wallet"),
                accounts_table.c.wallet.label("login_wallet"),
                sa.func.sum(ledger_table.c.den).label("den"),
            )
            .select_from(j)
            .where(
                ledger_table.c.created >= start,
                ledger_table.c.created < end,
                workers_table.c.account_id.isnot(None),
            )
            .group_by(workers_table.c.account_id,
                      accounts_table.c.payout_wallet, accounts_table.c.wallet)
            .having(sa.func.sum(ledger_table.c.den) > min_den)
        )
        out = []
        for row in result:
            addr = (row.payout_wallet or "").strip() or (row.login_wallet or "").strip() or None
            out.append({"account_id": str(row.account_id), "den": float(row.den), "payout_address": addr})
        return out


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
                sa.func.sum(ledger_table.c.den).label("den"),
            )
            .where(
                ledger_table.c.created >= start,
                ledger_table.c.created < end,
                ledger_table.c.wallet != "",
                ledger_table.c.wallet.isnot(None),
            )
            .group_by(ledger_table.c.wallet)
            .having(sa.func.sum(ledger_table.c.den) > min_den)
        )
        return [
            {"address": row.wallet, "den": float(row.den)}
            for row in result
        ]


async def count_unattributed_den(start: datetime, end: datetime) -> dict:
    """Diagnostic: how much den in the window has no wallet attached.

    The settlement bot logs this so the team can see how much earning is
    stranded for lack of a wallet (and chase those workers to set one).
    """
    async with await new_session() as session:
        result = await session.execute(
            sa.select(
                sa.func.count().label("jobs"),
                sa.func.coalesce(sa.func.sum(ledger_table.c.den), 0).label("den"),
            ).where(
                ledger_table.c.created >= start,
                ledger_table.c.created < end,
                sa.or_(
                    ledger_table.c.wallet == "",
                    ledger_table.c.wallet.is_(None),
                ),
            )
        )
        row = result.first()
        return {"jobs": int(row.jobs), "den": float(row.den)}

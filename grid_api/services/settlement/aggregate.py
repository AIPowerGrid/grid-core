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
from ...v2.schema import ledger as ledger_table


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

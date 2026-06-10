# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Roll up the den ledger into per-wallet totals for a settlement period.

The on-chain settlement bot calls this to turn the durable den_events
ledger (written per completed job in worker_ws.py) into the
[(wallet_address, total_den)] list it commits as a Merkle root and pays out.

Period boundaries are [start, end) UTC half-open intervals so adjacent
periods never double-count a job on the boundary.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa

from ...database import den_events_table, new_session


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
                den_events_table.c.wallet_address,
                sa.func.sum(den_events_table.c.den).label("den"),
            )
            .where(
                den_events_table.c.created >= start,
                den_events_table.c.created < end,
                den_events_table.c.wallet_address != "",
                den_events_table.c.wallet_address.isnot(None),
            )
            .group_by(den_events_table.c.wallet_address)
            .having(sa.func.sum(den_events_table.c.den) > min_den)
        )
        return [
            {"address": row.wallet_address, "den": float(row.den)}
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
                sa.func.coalesce(sa.func.sum(den_events_table.c.den), 0).label("den"),
            ).where(
                den_events_table.c.created >= start,
                den_events_table.c.created < end,
                sa.or_(
                    den_events_table.c.wallet_address == "",
                    den_events_table.c.wallet_address.is_(None),
                ),
            )
        )
        row = result.first()
        return {"jobs": int(row.jobs), "den": float(row.den)}

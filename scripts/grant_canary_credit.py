#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Grant at most $10 of idempotent operator credit to one billing canary."""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.database import close_database, init_database, new_session
from grid_api.services import alerts, credits
from grid_api.v2.schema import accounts as accounts_table

_MAX_CANARY_MICRO = 10_000_000


def amount_to_micro(raw: str) -> int:
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("amount must be a decimal USD value") from exc
    micro = amount * 1_000_000
    if micro != micro.to_integral_value():
        raise ValueError("amount supports at most six decimal places")
    value = int(micro)
    if value <= 0 or value > _MAX_CANARY_MICRO:
        raise ValueError("canary amount must be greater than $0 and no more than $10")
    return value


async def _run(args) -> None:
    account_id = UUID(args.account_id)
    amount_micro = amount_to_micro(args.amount_usd)
    if not args.ref.startswith("canary:") or len(args.ref) > 120:
        raise ValueError("ref must be a stable canary:* idempotency key of at most 120 characters")
    if not args.apply:
        print(f"dry_run=true account_id={account_id} amount_micro={amount_micro} ref={args.ref}")
        return

    await alerts.start()
    await init_database()
    try:
        async with await new_session() as session:
            exists = await session.scalar(
                sa.select(accounts_table.c.id).where(accounts_table.c.id == account_id),
            )
        if not exists:
            raise ValueError("account does not exist")
        applied = await credits.credit(
            account_id,
            amount_micro,
            reason="operator_canary",
            ref=args.ref,
        )
        balance = await credits.get_balance(account_id)
        alerts.emit(
            "canary_credit_granted",
            "success",
            "Operator credit is available for a supervised billing canary.",
            fields={
                "account": alerts.opaque_id(account_id),
                "amount_micro": amount_micro,
                "applied": applied,
                "balance_micro": balance,
            },
            dedupe_key=f"canary-credit:{args.ref}",
        )
        await alerts.flush()
    finally:
        await close_database()
        await alerts.stop()
    print(
        f"applied={str(applied).lower()} account_id={account_id} "
        f"amount_micro={amount_micro} balance_micro={balance}",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--amount-usd", required=True)
    parser.add_argument("--ref", required=True, help="Stable idempotency key, e.g. canary:2026-07-28")
    parser.add_argument("--apply", action="store_true", help="Actually move value; default is dry-run")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

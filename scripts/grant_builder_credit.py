#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Grant one selected builder $5-$20 of expiring promotional Grid credit."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.database import close_database, init_database, new_session
from grid_api.services import alerts, promotions
from grid_api.v2.schema import accounts as accounts_table

_MIN_GRANT_MICRO = 5_000_000
_MAX_GRANT_MICRO = 20_000_000
_MAX_CAMPAIGN_BUDGET_MICRO = 1_000_000_000
_MAX_EXPIRY_DAYS = 90
_CAMPAIGN_RE = re.compile(r"builder-[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_REF_RE = re.compile(r"builder:[A-Za-z0-9][A-Za-z0-9:._/#-]*$")


def usd_to_micro(raw: str, *, label: str) -> int:
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a decimal USD value") from exc
    if not amount.is_finite():
        raise ValueError(f"{label} must be finite")
    micro = amount * 1_000_000
    if micro != micro.to_integral_value():
        raise ValueError(f"{label} supports at most six decimal places")
    return int(micro)


def validate_policy(args) -> tuple[UUID, int, int, int]:
    account_id = UUID(args.account_id)
    amount_micro = usd_to_micro(args.amount_usd, label="amount")
    budget_micro = usd_to_micro(args.budget_usd, label="budget")
    expiry_days = int(args.expires_days)
    if not _MIN_GRANT_MICRO <= amount_micro <= _MAX_GRANT_MICRO:
        raise ValueError("builder grant must be between $5 and $20")
    if budget_micro < amount_micro or budget_micro > _MAX_CAMPAIGN_BUDGET_MICRO:
        raise ValueError("campaign budget must cover one grant and be no more than $1,000")
    if expiry_days <= 0 or expiry_days > _MAX_EXPIRY_DAYS:
        raise ValueError("builder credit must expire in 1 to 90 days")
    if len(args.campaign_id) > 64 or not _CAMPAIGN_RE.fullmatch(args.campaign_id):
        raise ValueError("campaign id must match builder-[a-z0-9-] and be 5-64 characters")
    if not 1 <= len(args.campaign_name.strip()) <= 160:
        raise ValueError("campaign name must be 1-160 characters")
    if len(args.ref) > 128 or not _REF_RE.fullmatch(args.ref):
        raise ValueError("ref must be a stable builder:* review id of at most 128 characters")
    return account_id, amount_micro, budget_micro, expiry_days


async def _run(args) -> None:
    account_id, amount_micro, budget_micro, expiry_days = validate_policy(args)
    if not args.apply:
        print(
            f"dry_run=true account_id={account_id} campaign_id={args.campaign_id} "
            f"amount_micro={amount_micro} budget_micro={budget_micro} "
            f"expires_days={expiry_days} ref={args.ref}",
        )
        return

    if not (promotions.PROMO_ENABLED and promotions.PROMO_SPENDABLE_LIVE):
        raise RuntimeError("promotional credits are not enabled and spendable")
    await alerts.start()
    await init_database()
    try:
        async with await new_session() as session:
            exists = await session.scalar(
                sa.select(accounts_table.c.id).where(accounts_table.c.id == account_id),
            )
        if not exists:
            raise ValueError("account does not exist")
        campaign_status = await promotions.ensure_fixed_campaign(
            args.campaign_id,
            name=args.campaign_name.strip(),
            grant_micro=amount_micro,
            budget_micro=budget_micro,
            expires_days=expiry_days,
            eligibility={"manual_builder_review": True},
        )
        result = await promotions.grant_once(account_id, args.campaign_id, ref=args.ref)
        if result["status"] not in {"granted", "already"}:
            raise RuntimeError(f"builder grant failed closed with status={result['status']}")
        alerts.emit(
            "builder_credit_granted",
            "success",
            "Promotional credit is available for a reviewed builder.",
            fields={
                "account": alerts.opaque_id(account_id),
                "campaign_id": args.campaign_id,
                "amount_micro": amount_micro,
                "campaign_status": campaign_status,
                "grant_status": result["status"],
                "spendable_live": promotions.PROMO_SPENDABLE_LIVE,
            },
            dedupe_key=f"builder-credit:{args.ref}",
        )
        await alerts.flush()
    finally:
        await close_database()
        await alerts.stop()
    print(
        f"campaign_status={campaign_status} grant_status={result['status']} "
        f"account_id={account_id} campaign_id={args.campaign_id} "
        f"amount_micro={amount_micro} spendable_live={str(promotions.PROMO_SPENDABLE_LIVE).lower()}",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, help="Canonical Grid account UUID")
    parser.add_argument("--campaign-id", required=True, help="Stable builder-* cohort id")
    parser.add_argument("--campaign-name", required=True)
    parser.add_argument("--amount-usd", required=True, help="Fixed per-builder grant, $5-$20")
    parser.add_argument("--budget-usd", required=True, help="Finite cohort budget, at most $1,000")
    parser.add_argument("--expires-days", default="60", help="Grant lifetime, 1-90 days")
    parser.add_argument("--ref", required=True, help="Stable builder:* review or artifact reference")
    parser.add_argument("--apply", action="store_true", help="Actually issue value; default is dry-run")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except (ValueError, TypeError, RuntimeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

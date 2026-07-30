#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only reconciliation of one demand-billing canary account and its jobs."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.config import get_settings
from grid_api.services.canary_audit import JobExpectation, audit_demand_canary


def _expectations(args) -> list[JobExpectation]:
    rows = [
        *((job_id, "success") for job_id in args.success),
        *((job_id, "failure") for job_id in args.failure),
        *((job_id, "absent") for job_id in args.absent),
    ]
    return [JobExpectation(job_id=UUID(job_id), outcome=outcome) for job_id, outcome in rows]


async def _run(args) -> bool:
    settings = get_settings()
    engine = create_async_engine(
        settings.async_database_url,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": "grid_demand_canary_audit",
                "default_transaction_read_only": "on",
            },
        },
    )
    try:
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            report = await audit_demand_canary(
                session,
                UUID(args.account_id),
                _expectations(args),
                stale_seconds=args.stale_seconds,
                allowed_services=frozenset(args.allow_service),
            )
    finally:
        await engine.dispose()
    print(json.dumps(report, indent=2, sort_keys=True))
    return bool(report["ok"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, help="Canonical Grid account UUID")
    parser.add_argument("--success", action="append", default=[], help="Job UUID expected to have settled successfully")
    parser.add_argument("--failure", action="append", default=[], help="Job UUID expected to have released without payout")
    parser.add_argument("--absent", action="append", default=[], help="Job UUID expected to have been rejected before reserve")
    parser.add_argument(
        "--allow-service",
        action="append",
        default=[],
        help="Approved service ID; when present every reservation must match one of these values",
    )
    parser.add_argument(
        "--stale-seconds",
        type=int,
        default=900,
        help="Fail when any held reservation is older than this many seconds (default: 900)",
    )
    args = parser.parse_args()
    if args.stale_seconds < 0:
        parser.error("--stale-seconds must be non-negative")
    try:
        passed = asyncio.run(_run(args))
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()

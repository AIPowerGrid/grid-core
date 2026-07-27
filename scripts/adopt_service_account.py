#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Promote one existing API key into a bounded backend service client."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.database import close_database, init_database
from grid_api.services.accounts import adopt_service_client


async def _run(args) -> None:
    await init_database()
    try:
        service = await adopt_service_client(
            args.id,
            args.name,
            account_id=args.account_id,
            key_label=args.key_label,
            allowed_providers=args.provider,
            google_audiences=args.google_audience,
            siwe_domains=args.siwe_domain,
            per_request_micro=args.per_request_micro,
            daily_micro=args.daily_micro,
        )
    finally:
        await close_database()
    print(f"service_id={service['id']}")
    print(f"account_id={service['account_id']}")
    print("existing_key_promoted=true")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, help="Stable id, e.g. aigarth")
    parser.add_argument("--name", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--key-label", required=True)
    parser.add_argument("--provider", action="append", choices=("app", "google", "wallet"), default=[])
    parser.add_argument("--google-audience", action="append", default=[])
    parser.add_argument(
        "--siwe-domain",
        action="append",
        default=[],
        help="Exact allowed wallet sign-in authority, e.g. aipg.art",
    )
    parser.add_argument("--per-request-micro", type=int, required=True)
    parser.add_argument("--daily-micro", type=int, required=True)
    args = parser.parse_args()
    if not args.provider:
        args.provider = ["app"]
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

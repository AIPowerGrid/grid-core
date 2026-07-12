#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Provision a bounded backend service account and print its key once."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.database import close_database, init_database
from grid_api.services.accounts import create_service_client


async def _run(args) -> None:
    await init_database()
    try:
        service, key = await create_service_client(
            args.id,
            args.name,
            allowed_providers=args.provider,
            google_audiences=args.google_audience,
            per_request_micro=args.per_request_micro,
            daily_micro=args.daily_micro,
        )
    finally:
        await close_database()
    print(f"service_id={service['id']}")
    print(f"account_id={service['account_id']}")
    print(f"api_key={key}")
    print("Store the API key now; Core stores only its salted hash.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, help="Stable id, e.g. grid-console")
    parser.add_argument("--name", required=True)
    parser.add_argument("--provider", action="append", choices=("app", "google"), default=[])
    parser.add_argument("--google-audience", action="append", default=[])
    parser.add_argument("--per-request-micro", type=int)
    parser.add_argument("--daily-micro", type=int)
    args = parser.parse_args()
    if not args.provider:
        args.provider = ["app"]
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

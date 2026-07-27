#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preview or compare-and-swap one service client's identity proof policy."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.database import close_database, init_database
from grid_api.services.service_auth import configure_identity_policy


async def _run(args) -> None:
    await init_database()
    try:
        result = await configure_identity_policy(
            args.id,
            allowed_providers=args.provider,
            google_audiences=args.google_audience,
            siwe_domains=args.siwe_domain,
            expected_digest=args.expect_digest,
            apply=args.apply,
        )
    finally:
        await close_database()
    print(json.dumps(result, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument(
        "--provider",
        action="append",
        choices=("app", "google", "wallet"),
        required=True,
    )
    parser.add_argument("--google-audience", action="append", default=[])
    parser.add_argument("--siwe-domain", action="append", default=[])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the policy; default is preview-only",
    )
    parser.add_argument(
        "--expect-digest",
        help="Required with --apply; copy current_digest from a fresh preview",
    )
    args = parser.parse_args()
    if args.apply and not args.expect_digest:
        parser.error("--apply requires --expect-digest from a fresh preview")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

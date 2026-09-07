#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preview or explicitly execute one approved validator allocation on Base."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api import database
from grid_api.config import get_settings
from grid_api.services.settlement.validator_payments import preview_payment, send_payment
from scripts.preview_validator_compensation import read_private, write_private


async def run(args):
    request = read_private(args.input)
    engine = create_async_engine(
        get_settings().async_database_url,
        connect_args={"server_settings": {"default_transaction_read_only": "off" if args.send else "on"}},
    )
    previous = database._session_factory
    database._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.send:
            return await send_payment(request, expected_digest=args.expect_digest)
        return await preview_payment(request)
    finally:
        database._session_factory = previous
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Owned private payment request JSON")
    parser.add_argument("--output", type=Path, required=True, help="New private output, never overwritten")
    parser.add_argument("--send", action="store_true", help="Execute only with the separate Core send gate enabled")
    parser.add_argument("--expect-digest")
    args = parser.parse_args()
    if args.send and not args.expect_digest:
        parser.error("--send requires the exact reviewed --expect-digest")
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output already exists")
        result = asyncio.run(run(args))
        write_private(args.output, result)
    except Exception:
        print(
            "Payment operation or output failed. A transaction may already be committed or broadcast. "
            "Retry the SAME input and digest with a new private output path; do not send manually or change the allocation.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({key: result[key] for key in ("status", "reason", "digest", "dry_run") if key in result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

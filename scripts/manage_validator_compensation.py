#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preview or approve durable pilot allocations. Never sends a payment."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api import database
from grid_api.config import get_settings
from grid_api.services.validator_compensation import create_campaign, finalize_campaign
from scripts.preview_validator_compensation import read_private, write_private


async def run(args):
    # Do not call init_database: preview must never create/alter any table.
    engine = create_async_engine(
        get_settings().async_database_url,
        connect_args={
            "server_settings": {"default_transaction_read_only": "off" if args.apply else "on"},
        },
    )
    previous = database._session_factory
    database._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        options = {"apply": args.apply, "expected_digest": args.expect_digest}
        if args.action == "create":
            request = read_private(args.input)
            if not isinstance(request, dict) or set(request) != {"terms", "validator_ids"}:
                raise ValueError("invalid campaign request")
            return await create_campaign(request["terms"], request["validator_ids"], **options)
        return await finalize_campaign(args.campaign_id, **options)
    finally:
        database._session_factory = previous
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "finalize"))
    parser.add_argument("--input", type=Path, help="Private create request: terms and validator_ids")
    parser.add_argument("--campaign-id", help="Existing campaign for finalization")
    parser.add_argument("--output", type=Path, required=True, help="New private output file, never overwritten")
    parser.add_argument("--apply", action="store_true", help="Commit with an exact reviewed preview digest")
    parser.add_argument("--expect-digest")
    args = parser.parse_args()
    if args.apply and not args.expect_digest:
        parser.error("--apply requires --expect-digest")
    if args.action == "create" and (not args.input or args.campaign_id):
        parser.error("create requires only --input")
    if args.action == "finalize" and (not args.campaign_id or args.input):
        parser.error("finalize requires only --campaign-id")
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output already exists")
        result = asyncio.run(run(args))
        write_private(args.output, result)
    except Exception:
        # A failed file write can follow a successful commit. Retrying the same
        # immutable identifier/digest recovers it; never suggest creating a new id.
        print(
            "Operation or output failed. No payment was sent. An allocation may have committed; "
            "retry the same campaign and digest with a new private output path.",
            file=sys.stderr,
        )
        return 1
    fields = (
        "schema",
        "status",
        "dry_run",
        "sendable",
        "digest",
        "reviewed_units",
        "eligible_operators",
        "allocated_atomic",
        "unallocated_atomic",
        "excluded",
    )
    print(json.dumps({key: result[key] for key in fields if key in result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

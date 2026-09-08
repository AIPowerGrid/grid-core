#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prepare or review signed validator payout consent. Never sends payments."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api import database
from grid_api.config import get_settings
from grid_api.services.validator_compensation_operator import export_for_review
from grid_api.services.validator_compensation_recipients import bind_recipient, prepare_recipient
from scripts.preview_validator_compensation import read_private, write_private


async def run(args):
    request = read_private(args.input)
    engine = create_async_engine(
        get_settings().async_database_url,
        connect_args={"server_settings": {"default_transaction_read_only": "off" if args.apply else "on"}},
    )
    previous = database._session_factory
    database._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.action == "prepare":
            if not isinstance(request, dict) or set(request) != {"campaign_id", "operator_group_id", "recipient"}:
                raise ValueError("invalid recipient preparation request")
            return await prepare_recipient(**request)
        if args.action == "export":
            if not isinstance(request, dict) or set(request) != {"request_id", "approval_ref"}:
                raise ValueError("invalid recipient review export")
            return await export_for_review(**request)
        return await bind_recipient(request, apply=args.apply, expected_digest=args.expect_digest)
    finally:
        database._session_factory = previous
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "bind", "export"))
    parser.add_argument("--input", type=Path, required=True, help="Owned private JSON file; never a private key")
    parser.add_argument("--output", type=Path, required=True, help="New private output file")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-digest")
    args = parser.parse_args()
    if args.action in {"prepare", "export"} and (args.apply or args.expect_digest):
        parser.error("prepare and export are read-only")
    if args.apply and not args.expect_digest:
        parser.error("--apply requires the exact reviewed --expect-digest")
    try:
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output already exists")
        result = asyncio.run(run(args))
        write_private(args.output, result)
    except Exception:
        print(
            "Recipient operation or output failed. No payment was sent. Consent may have committed; "
            "retry the same signed input and digest with a new private output path.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({key: result[key] for key in ("status", "digest", "dry_run", "sendable") if key in result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

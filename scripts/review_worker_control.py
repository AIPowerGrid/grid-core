#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preview or apply a media-worker common-control review."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.database import close_database, init_database
from grid_api.services.worker_control_reviews import review_worker_control


async def _run(args) -> None:
    await init_database()
    try:
        result = await review_worker_control(
            args.worker_id,
            action=args.action,
            operator_group_id=args.operator_group,
            review_ref=args.review_ref,
            review_days=args.review_days,
            expected_digest=args.expect_digest,
            apply=args.apply,
        )
    finally:
        await close_database()
    print(json.dumps(result, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--action", choices=("verify", "reject", "revoke"), required=True)
    parser.add_argument("--operator-group", help="Opaque opg_* common-control identifier")
    parser.add_argument("--review-ref", required=True, help="Non-sensitive opaque review reference")
    parser.add_argument("--review-days", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Persist; default is preview-only")
    parser.add_argument("--expect-digest", help="Required with --apply; obtain from a fresh preview")
    args = parser.parse_args()
    if args.action == "verify" and not args.operator_group:
        parser.error("verify requires --operator-group")
    if args.action != "verify" and args.operator_group:
        parser.error("--operator-group is accepted only with --action verify")
    if args.apply and not args.expect_digest:
        parser.error("--apply requires --expect-digest from a fresh preview")
    try:
        asyncio.run(_run(args))
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

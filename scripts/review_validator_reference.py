#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preview or apply a governed media reference-worker review."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.database import close_database, init_database
from grid_api.services.validator_reference_reviews import review_reference


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


async def _run(args) -> None:
    await init_database()
    try:
        result = await review_reference(
            args.worker_id,
            model=args.model,
            modality=args.modality,
            action=args.action,
            review_ref=args.review_ref,
            quality_window_start=args.quality_window_start,
            quality_window_end=args.quality_window_end,
            quality_pass_rate=args.quality_pass_rate,
            expected_digest=args.expect_digest,
            apply=args.apply,
        )
    finally:
        await close_database()
    print(json.dumps(result, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--modality", choices=("image", "video"), required=True)
    parser.add_argument("--action", choices=("review", "activate", "pause", "revoke"), required=True)
    parser.add_argument("--review-ref", required=True, help="Non-sensitive opaque review reference")
    parser.add_argument("--quality-window-start", type=_timestamp)
    parser.add_argument("--quality-window-end", type=_timestamp)
    parser.add_argument("--quality-pass-rate", type=float)
    parser.add_argument("--apply", action="store_true", help="Persist; default is preview-only")
    parser.add_argument("--expect-digest", help="Required with --apply; obtain from a fresh preview")
    args = parser.parse_args()
    if args.action == "review" and (
        args.quality_window_start is None
        or args.quality_window_end is None
        or args.quality_pass_rate is None
    ):
        parser.error("review requires the quality window and pass rate")
    if args.action != "review" and any(
        value is not None
        for value in (
            args.quality_window_start,
            args.quality_window_end,
            args.quality_pass_rate,
        )
    ):
        parser.error("quality fields are accepted only with --action review")
    if args.apply and not args.expect_digest:
        parser.error("--apply requires --expect-digest from a fresh preview")
    try:
        asyncio.run(_run(args))
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Publish the AIPG funding price Core uses for AIPG deposits.

Core reads `GRID_AIPG_PRICE_FILE` on every funding request, so a new price
takes effect without restarting the API. This writes that file atomically.
Dry-run by default; `--apply` writes. Standard library only: it reads no
database, chain or market, and moves no value. The operator chooses the price.

The price is micro-USD per whole AIPG (1200 = $0.0012). Core subtracts its own
haircut and enforces its own caps on top. Choose a price at or below what the
treasury could realistically realize; the pool is thin and a quoted spot price
is not a safe credit oracle.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_FILE = "/etc/aipg/aipg-price.json"
EPOCH_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
MAX_PRICE_MICRO = 1_000_000_000  # $1,000 per AIPG; a typo guard, not a policy
MAX_VALID_HOURS = 14 * 24
MAX_GRACE_MINUTES = 24 * 60
LARGE_CHANGE_RATIO = 1.5


def build_record(
    *, price_micro: int, valid_hours: float, grace_minutes: int, block: int,
    epoch: str | None, now: datetime,
) -> dict:
    """Validated price record. `as_of` is backdated by the grace period so a
    transfer mined just before publication is still claimable at this price."""
    if type(price_micro) is not int or not 1 <= price_micro <= MAX_PRICE_MICRO:
        raise ValueError(f"price must be 1 to {MAX_PRICE_MICRO} micro-USD per AIPG")
    if not 1 <= valid_hours <= MAX_VALID_HOURS:
        raise ValueError(f"validity must be 1 to {MAX_VALID_HOURS} hours")
    if not 0 <= grace_minutes <= MAX_GRACE_MINUTES:
        raise ValueError(f"grace must be 0 to {MAX_GRACE_MINUTES} minutes")
    if type(block) is not int or block < 0:
        raise ValueError("block must be a non-negative integer")
    now = now.astimezone(UTC).replace(microsecond=0)
    epoch = epoch or now.strftime("%Y-%m-%dT%H%MZ")
    if not EPOCH_RE.fullmatch(epoch):
        raise ValueError("epoch may contain letters, digits and . _ : - (at most 64)")
    return {
        "version": 1,
        "epoch": epoch,
        "price_micro": price_micro,
        "as_of": (now - timedelta(minutes=grace_minutes)).isoformat(),
        "valid_until": (now + timedelta(hours=valid_hours)).isoformat(),
        "block": block,
    }


def current_price(path: Path) -> int | None:
    try:
        value = json.loads(path.read_text()).get("price_micro")
    except (OSError, ValueError, AttributeError):
        return None
    return value if type(value) is int and value > 0 else None


def large_change(previous: int | None, new: int) -> bool:
    if not previous:
        return False
    return new > previous * LARGE_CHANGE_RATIO or new * LARGE_CHANGE_RATIO < previous


def write_atomic(path: Path, record: dict) -> None:
    """Replace the file in one step, world-readable and writable only by its owner."""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--price-micro", type=int, required=True, help="micro-USD per whole AIPG, e.g. 1000 = $0.0010")
    parser.add_argument("--valid-hours", type=float, default=168.0, help="how long the price stays valid (default 168)")
    parser.add_argument("--grace-minutes", type=int, default=60, help="accept transfers mined this long before publication (default 60)")
    parser.add_argument("--block", type=int, default=0, help="optional Base block the price was observed at")
    parser.add_argument("--epoch", help="label stored on receipts (default: publication time)")
    parser.add_argument("--file", default=os.environ.get("GRID_AIPG_PRICE_FILE") or DEFAULT_FILE)
    parser.add_argument("--allow-large-change", action="store_true", help="permit a change of more than 50%% from the current price")
    parser.add_argument("--apply", action="store_true", help="write the file; default is a dry run")
    args = parser.parse_args(argv)

    path = Path(args.file)
    try:
        record = build_record(
            price_micro=args.price_micro, valid_hours=args.valid_hours, grace_minutes=args.grace_minutes,
            block=args.block, epoch=args.epoch, now=datetime.now(UTC),
        )
        previous = current_price(path)
        if large_change(previous, args.price_micro) and not args.allow_large_change:
            raise ValueError(
                f"price changes by more than 50% from the current {previous}; "
                "re-run with --allow-large-change if that is intended",
            )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    span_hours = args.valid_hours + args.grace_minutes / 60
    print(f"file:        {path}")
    print(f"epoch:       {record['epoch']}")
    print(f"price:       {record['price_micro']} micro-USD per AIPG (${record['price_micro'] / 1_000_000:.6f})"
          + (f", was {previous}" if previous else ""))
    print(f"accepts transfers mined from {record['as_of']} to {record['valid_until']}")
    print(f"Core needs GRID_AIPG_PRICE_MAX_AGE_SECONDS >= {int(span_hours * 3600)}, or the price expires early.")
    if not args.apply:
        print("dry run; re-run with --apply to publish")
        return 0
    try:
        write_atomic(path, record)
    except OSError as exc:
        print(f"error: could not write {path} ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

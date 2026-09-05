#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Allocate a private review snapshot offline. No --apply or --send mode exists."""

import argparse
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grid_api.services.validator_compensation_preview import PreviewError, preview_allocation

MAX_BYTES = 2 * 1024 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreviewError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise PreviewError("invalid JSON constant")


def read_private(path):
    if os.name != "posix":
        raise PreviewError("private snapshot CLI requires POSIX file permissions")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise PreviewError("input must be a private regular file owned by this user")
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise PreviewError("input file is oversized")
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def write_private(path, result):
    data = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    # A failed write stays private for inspection; never unlink a possibly replaced path.
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", required=True, help="Explicit timezone-aware snapshot time")
    args = parser.parse_args()
    try:
        result = preview_allocation(read_private(args.input), as_of=args.as_of)
        write_private(args.output, result)
    except (OSError, ValueError, RecursionError, OverflowError):
        print("Invalid private input or output; no payment was sent.", file=sys.stderr)
        return 1
    # Operator-control correlation stays in the private file, not terminal logs.
    keys = (
        "schema",
        "dry_run",
        "sendable",
        "input_authority",
        "simulation_digest",
        "eligible_operators",
        "reviewed_units",
        "allocated_atomic",
        "unallocated_atomic",
    )
    print(json.dumps({key: result[key] for key in keys}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

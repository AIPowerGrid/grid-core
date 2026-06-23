# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for IPFS snapshot building + serialization.

The pin_settlement_snapshot() function itself hits the network; we don't
test it here. Instead we test the deterministic-snapshot guarantees: same
inputs always produce the same bytes, regardless of input order or whitespace.

This determinism is what lets independent verifiers rebuild the same Merkle
tree from the IPFS snapshot and confirm the on-chain root.
"""

from __future__ import annotations

import json

from grid_api.services.settlement.ipfs import (
    build_settlement_snapshot,
    settlement_snapshot_json,
)
from grid_api.services.den import DEN_SCALE


# ============ SNAPSHOT BUILDING ============


def test_snapshot_sums_total_den():
    snap = build_settlement_snapshot(
        period_id=42,
        period_length_seconds=86400,
        pool_allocation_wei=4080 * 10**18,
        entries=[
            {"address": "0x" + "11" * 20, "den": 1000},
            {"address": "0x" + "22" * 20, "den": 2500},
            {"address": "0x" + "33" * 20, "den": 500},
        ],
        timestamp_iso="2026-06-07T00:00:00+00:00",
    )

    assert snap["period_id"] == 42
    assert snap["total_den"] == 4000 * DEN_SCALE
    assert snap["pool_allocation_wei"] == 4080 * 10**18
    assert len(snap["entries"]) == 3


def test_snapshot_sorts_entries_deterministically():
    """Same set of entries fed in different orders must produce the same
    snapshot. Tests address-based sorting."""
    entries_a = [
        {"address": "0x" + "aa" * 20, "den": 1000},
        {"address": "0x" + "11" * 20, "den": 2000},
        {"address": "0x" + "ff" * 20, "den": 500},
    ]
    entries_b = list(reversed(entries_a))

    snap_a = build_settlement_snapshot(1, 86400, 1000, entries_a, "2026-06-07T00:00:00+00:00")
    snap_b = build_settlement_snapshot(1, 86400, 1000, entries_b, "2026-06-07T00:00:00+00:00")

    assert snap_a["entries"] == snap_b["entries"]
    assert settlement_snapshot_json(snap_a) == settlement_snapshot_json(snap_b)


def test_snapshot_coerces_den_to_int():
    """Floats / strings are normalized to integer micro-den settlement units."""
    snap = build_settlement_snapshot(
        period_id=1,
        period_length_seconds=86400,
        pool_allocation_wei=1000,
        entries=[{"address": "0x" + "11" * 20, "den": "1.234567"}],
        timestamp_iso="2026-06-07T00:00:00+00:00",
    )
    assert snap["entries"][0]["den"] == 1_234_567
    assert isinstance(snap["entries"][0]["den"], int)


# ============ JSON SERIALIZATION ============


def test_canonical_json_is_compact_and_sorted():
    """`settlement_snapshot_json` must emit deterministic bytes: keys sorted,
    no whitespace. Otherwise the IPFS CID won't match for independent
    rebuilders."""
    snap = build_settlement_snapshot(
        period_id=1,
        period_length_seconds=86400,
        pool_allocation_wei=1000,
        entries=[{"address": "0x" + "11" * 20, "den": 100}],
        timestamp_iso="2026-06-07T00:00:00+00:00",
    )
    serialized = settlement_snapshot_json(snap)

    # No whitespace
    assert " " not in serialized.replace('"2026-06-07T00:00:00+00:00"', "")

    # Round-trip is identical (json.dumps(json.loads(x), ..., sort_keys=True) == x)
    parsed = json.loads(serialized)
    reserialized = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    assert serialized == reserialized


def test_canonical_json_same_inputs_same_output():
    """Two independent calls with the same inputs must produce byte-identical output."""
    base = lambda: build_settlement_snapshot(
        period_id=99,
        period_length_seconds=86400,
        pool_allocation_wei=4080 * 10**18,
        entries=[
            {"address": "0x" + "aa" * 20, "den": 100},
            {"address": "0x" + "bb" * 20, "den": 200},
        ],
        timestamp_iso="2026-06-07T12:00:00+00:00",
    )
    assert settlement_snapshot_json(base()) == settlement_snapshot_json(base())

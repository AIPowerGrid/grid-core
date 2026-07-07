# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OFAC screening gate — denylist authority, fail-closed on unverifiable."""

import pytest

from grid_api.services.settlement import sanctions

CLEAN = "0x1111111111111111111111111111111111111111"
BAD = "0xBAD0000000000000000000000000000000000000"


@pytest.mark.asyncio
async def test_denylist_blocks(monkeypatch):
    monkeypatch.setenv("GRID_SANCTIONS_DENYLIST", BAD)
    monkeypatch.setattr(sanctions, "ORACLE_ADDRESS", "")  # oracle off
    r = await sanctions.screen(BAD.lower())
    assert r["sanctioned"] and r["source"] == "denylist"
    assert sanctions.payable_status(r) == "blocked_sanctions"


@pytest.mark.asyncio
async def test_clear_when_no_oracle(monkeypatch):
    monkeypatch.setenv("GRID_SANCTIONS_DENYLIST", "")
    monkeypatch.setattr(sanctions, "ORACLE_ADDRESS", "")
    r = await sanctions.screen(CLEAN)
    assert not r["sanctioned"] and not r["hold"] and r["source"] == "clear"
    assert sanctions.payable_status(r) is None


@pytest.mark.asyncio
async def test_empty_address_holds():
    r = await sanctions.screen("")
    assert not r["sanctioned"] and r["hold"] and sanctions.payable_status(r) == "review_sanctions"


@pytest.mark.asyncio
async def test_oracle_unreachable_holds(monkeypatch):
    # Oracle configured but RPC dead → fail-closed: don't pay blind, hold for review.
    monkeypatch.setenv("GRID_SANCTIONS_DENYLIST", "")
    monkeypatch.setattr(sanctions, "ORACLE_ADDRESS", "0x40C57923924B5c5c5455c48D93317139ADDaC8fb")
    monkeypatch.setattr(sanctions, "ORACLE_RPC", "http://127.0.0.1:1")
    r = await sanctions.screen(CLEAN)
    assert not r["sanctioned"] and r["hold"] and r["source"] == "error"
    assert sanctions.payable_status(r) == "review_sanctions"

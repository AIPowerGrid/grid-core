# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Multi-asset Base sender — proof functions + period-runner routing.

The proof layer is exercised against synthetic receipts (no chain): an ERC-20
leg is proven ONLY by a matching Transfer from the right token; a native leg
ONLY by the tx itself carrying the right to+value. The runner test drives the
real DB path (in-memory sqlite) with no treasury configured, proving the
routing: clean→unsendable (dark), no-wallet→accrued, denylisted→blocked, and
terminal statuses skipped on re-run.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import sqlalchemy as sa

from grid_api import database
from grid_api.services.settlement import assets, multiasset, revenue, sanctions
from grid_api.v2.schema import metadata as v2_metadata
from grid_api.v2.schema import payout_legs as legs_t

# keccak("Transfer(address,address,uint256)") — constant, no web3 needed.
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TO = "0x00000000000000000000000000000000000000aa"
USDC = assets.spec("USDC")["address"]


class _FakeEth:
    def __init__(self, tx=None):
        self._tx = tx or {}

    def get_transaction(self, h):
        return self._tx


class _FakeW3:
    def __init__(self, tx=None):
        self.eth = _FakeEth(tx)

    def keccak(self, text=""):
        assert text == "Transfer(address,address,uint256)"
        return bytes.fromhex(TRANSFER_TOPIC)


def _erc20_receipt(token, to, units, status=1):
    return {"status": status, "logs": [{
        "address": token,
        "topics": ["0x" + TRANSFER_TOPIC,
                   "0x" + "0" * 64,
                   "0x" + "0" * 24 + to.lower().replace("0x", "")],
        "data": hex(units),
    }]}


def test_erc20_proof_requires_matching_transfer():
    w3 = _FakeW3()
    good = _erc20_receipt(USDC, TO, 12_500_000)
    assert multiasset._receipt_proves_erc20(w3, good, USDC, TO, 12_500_000)
    # wrong amount / wrong recipient / wrong token / reverted → all unproven
    assert not multiasset._receipt_proves_erc20(w3, _erc20_receipt(USDC, TO, 1), USDC, TO, 12_500_000)
    other = "0x000000000000000000000000000000000000dead"
    assert not multiasset._receipt_proves_erc20(w3, _erc20_receipt(USDC, other, 12_500_000), USDC, TO, 12_500_000)
    assert not multiasset._receipt_proves_erc20(w3, _erc20_receipt(other, TO, 12_500_000), USDC, TO, 12_500_000)
    assert not multiasset._receipt_proves_erc20(w3, _erc20_receipt(USDC, TO, 12_500_000, status=0), USDC, TO, 12_500_000)
    # status-1 receipt with NO logs (the classic false-positive) → unproven
    assert not multiasset._receipt_proves_erc20(w3, {"status": 1, "logs": []}, USDC, TO, 12_500_000)


def test_native_proof_is_the_tx_itself():
    units = 10**16  # 0.01 ETH
    w3 = _FakeW3(tx={"to": TO, "value": units})
    assert multiasset._tx_proves_native(w3, "0xabc", {"status": 1}, TO, units)
    assert not multiasset._tx_proves_native(w3, "0xabc", {"status": 0}, TO, units)  # reverted
    w3 = _FakeW3(tx={"to": TO, "value": units - 1})
    assert not multiasset._tx_proves_native(w3, "0xabc", {"status": 1}, TO, units)  # wrong value
    w3 = _FakeW3(tx={"to": "0x" + "b" * 40, "value": units})
    assert not multiasset._tx_proves_native(w3, "0xabc", {"status": 1}, TO, units)  # wrong dest


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(v2_metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield
    finally:
        database._session_factory = old
        await engine.dispose()


A_CLEAN = "11111111-1111-1111-1111-111111111111"
A_NOWALLET = "22222222-2222-2222-2222-222222222222"
A_BAD = "33333333-3333-3333-3333-333333333333"
BADADDR = "0xbad0000000000000000000000000000000000bad"


@pytest.mark.asyncio
async def test_runner_routing_dark(db, monkeypatch):
    """No treasury configured: clean legs count 'unsendable' (nothing sent, no
    rows invented), no-wallet legs accrue, denylisted legs are blocked — and a
    re-run skips terminal legs instead of rewriting them."""
    P = "p-test"
    rows = [
        {"account_id": A_CLEAN, "den": 60.0, "payout_address": TO},
        {"account_id": A_NOWALLET, "den": 20.0, "payout_address": None},
        {"account_id": A_BAD, "den": 20.0, "payout_address": BADADDR},
    ]

    async def fake_rows(start, end):
        return rows

    async def fake_pots(period_id):
        return {"USDC": 100.0, "ETH": 1.0}

    monkeypatch.setattr(multiasset, "aggregate_den_by_account", fake_rows)
    monkeypatch.setattr(multiasset, "revenue_pots", fake_pots)
    monkeypatch.setattr(revenue.economics, "PROTOCOL_FEE_BPS", 0)
    monkeypatch.setattr(revenue.economics, "SENTINEL_FEE_BPS", 0)
    monkeypatch.setenv("GRID_SANCTIONS_DENYLIST", BADADDR)
    monkeypatch.setattr(sanctions, "ORACLE_ADDRESS", "")
    monkeypatch.setattr(multiasset, "BASE_RPC_URL", "")
    monkeypatch.setattr(multiasset, "TREASURY_PK", "")

    out = await multiasset.send_period_multiasset(None, None, P)
    # 2 assets × {clean: unsendable, no-wallet: accrued, denylisted: blocked}
    assert out.get("unsendable") == 2
    assert out.get("accrued") == 2
    assert out.get("blocked_sanctions") == 2
    assert "sent" not in out and "failed" not in out

    async with await database.new_session() as s:
        legs = (await s.execute(sa.select(legs_t.c.account_id, legs_t.c.asset,
                                          legs_t.c.status, legs_t.c.amount))).all()
    by = {(str(a), asset): (status, float(amt)) for a, asset, status, amt in legs}
    # accrued + blocked recorded (4 rows); unsendable NOT recorded (retry next run)
    assert len(legs) == 4
    assert by[(A_NOWALLET, "USDC")][0] == "accrued"
    assert by[(A_BAD, "USDC")][0] == "blocked_sanctions"
    # amounts are the den shares: no-wallet did 20% → 20 USDC, 0.2 ETH
    assert by[(A_NOWALLET, "USDC")][1] == 20.0
    assert by[(A_NOWALLET, "ETH")][1] == pytest.approx(0.2)

    # Re-run: blocked legs are terminal (skipped); accrued rewritten (still accrued).
    out2 = await multiasset.send_period_multiasset(None, None, P)
    assert out2.get("skipped") == 2          # the two blocked_sanctions legs
    assert out2.get("blocked_sanctions") is None
    assert out2.get("accrued") == 2


@pytest.mark.asyncio
async def test_runner_holds_unsupported_pot_assets(db, monkeypatch):
    async def fake_rows(start, end):
        return [{"account_id": A_CLEAN, "den": 1.0, "payout_address": TO}]

    async def fake_pots(period_id):
        return {"DOGE": 100.0}

    monkeypatch.setattr(multiasset, "aggregate_den_by_account", fake_rows)
    monkeypatch.setattr(multiasset, "revenue_pots", fake_pots)
    out = await multiasset.send_period_multiasset(None, None, "p-doge")
    assert out["unsupported"] == ["DOGE"]
    assert out["pots"] == {}  # nothing distributable — held, never guessed

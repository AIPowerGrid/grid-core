# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DB + settle-lifecycle tests for custodial payouts — proves the safety
properties the pure-math tests can't:

  1. unique nonce index rejects duplicate nonces
  2. concurrent runners cannot bind the same nonce (Postgres advisory lock)
  3. immediate confirm marks 'sent' ONLY with a matching ERC-20 Transfer
  4. a consumed nonce without on-chain proof becomes 'manual_review'
  5. a rerun after 'sent' does NOT rebroadcast (idempotent)
  6. reconcile of a 'pending' row settles via the nonce check, no second tx

DB: in-memory SQLite (same StaticPool harness as test_reservation_lifecycle.py).
Set PAYOUTS_TEST_DB_URL=postgresql+asyncpg://… to additionally run the
advisory-lock concurrency test (skipped on SQLite — no pg_advisory_lock).
"""

import os
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.v2.schema import metadata as v2_metadata
from grid_api.services.settlement import payouts as P

AIPG = P.AIPG_TOKEN_ADDRESS
WALLET = "0x9da91df1becbab9015fd6ba9e2a2e2d8a90273c1"
HOT = "0x20A82fD11e4A5fC8d4b5A44083C05e4b28dB53B9"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_PG = os.environ.get("PAYOUTS_TEST_DB_URL", "")


@pytest_asyncio.fixture
async def db():
    if _PG.startswith("postgresql"):
        engine = create_async_engine(_PG)
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
                                      connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(v2_metadata.create_all)
    old = database._session_factory
    database._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield engine
    finally:
        database._session_factory = old
        async with engine.begin() as conn:
            await conn.run_sync(v2_metadata.drop_all)
        await engine.dispose()


# ── fake web3 ctx (lets us drive _settle_one deterministically) ─────────────
class _Hash(str):
    def hex(self):
        return str(self)


def _hash_for(nonce):
    return _Hash("0x" + format(int(nonce), "064x"))


class _FakeEth:
    def __init__(self):
        self.mined = 0
        self.pending = 0
        self.base_fee = 5_000_000
        self.receipts = {}     # hash -> receipt
        self.broadcasts = []
        self.send_raises = None
        self.wait_timeout = False

    def get_transaction_count(self, addr, block_identifier="latest"):
        return self.pending if block_identifier == "pending" else self.mined

    def get_block(self, _which):
        return {"baseFeePerGas": self.base_fee}

    def get_transaction_receipt(self, h):
        r = self.receipts.get(str(h))
        if r is None:
            raise Exception("not found")
        return r

    def send_raw_transaction(self, raw):
        if self.send_raises:
            raise ValueError(self.send_raises)
        self.broadcasts.append(raw)
        return raw

    def wait_for_transaction_receipt(self, h, timeout=90, poll_latency=2):
        if self.wait_timeout or str(h) not in self.receipts:
            raise Exception("timeout")
        return self.receipts[str(h)]


class _FakeW3:
    def __init__(self, eth):
        self.eth = eth

    def to_wei(self, v, _unit):
        return int(float(v) * 1_000_000_000)

    def keccak(self, text=None):
        return _Hash(TRANSFER_TOPIC)


class _FakeWeb3:
    @staticmethod
    def to_checksum_address(a):
        return a


class _FakeFns:
    def transfer(self, to, amount):
        self._to, self._amt = to, amount
        return self

    def build_transaction(self, params):
        return {"to": self._to, "amount": self._amt, **params}


class _FakeToken:
    def __init__(self):
        self.functions = _FakeFns()


class _FakeAcct:
    address = HOT

    def sign_transaction(self, tx):
        h = _hash_for(tx["nonce"])  # deterministic from nonce → replacement keeps the hash
        return type("Signed", (), {"hash": h, "raw_transaction": h})()


def _ctx(eth):
    return (_FakeWeb3, _FakeW3(eth), _FakeAcct(), _FakeToken(), 18)


def _transfer_receipt(to, aipg, status=1, token=AIPG):
    val = int(round(aipg * 10 ** 18))
    to_topic = "0x" + ("0" * 24) + to.lower().replace("0x", "")
    return {"status": status, "logs": [{
        "address": token,
        "topics": [TRANSFER_TOPIC, "0x" + "0" * 64, to_topic],
        "data": "0x" + format(val, "064x"),
    }]}


# ── 1/2. nonce uniqueness ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_unique_nonce_index_rejects_duplicate(db):
    await P._write("p1", uuid.uuid4(), address=WALLET, den=1, aipg=1, status="pending", tx_hash="0xaa", nonce=42)
    with pytest.raises(Exception):
        await P._write("p2", uuid.uuid4(), address=WALLET, den=1, aipg=1, status="pending", tx_hash="0xbb", nonce=42)


@pytest.mark.asyncio
async def test_max_assigned_nonce_monotonic(db):
    assert await P._max_assigned_nonce() == -1
    await P._write("p1", uuid.uuid4(), address=WALLET, den=1, aipg=1, status="pending", tx_hash="0x1", nonce=7)
    assert await P._max_assigned_nonce() == 7


# ── 3. proof gate (pure) ─────────────────────────────────────────────────────
def test_receipt_proof_requires_matching_transfer():
    w3 = _FakeW3(_FakeEth())
    wei = int(1.0 * 10 ** 18)
    assert P._receipt_proves_transfer(w3, _transfer_receipt(WALLET, 1.0), WALLET, wei) is True
    assert P._receipt_proves_transfer(w3, _transfer_receipt(WALLET, 1.0, status=0), WALLET, wei) is False
    assert P._receipt_proves_transfer(w3, {"status": 1, "logs": []}, WALLET, wei) is False
    assert P._receipt_proves_transfer(w3, _transfer_receipt(HOT, 1.0), WALLET, wei) is False
    assert P._receipt_proves_transfer(w3, _transfer_receipt(WALLET, 2.0), WALLET, wei) is False
    assert P._receipt_proves_transfer(w3, _transfer_receipt(WALLET, 1.0, token="0xdead"), WALLET, wei) is False


@pytest.mark.asyncio
async def test_settle_marks_sent_only_with_transfer_proof(db):
    eth = _FakeEth()
    acct = uuid.uuid4()
    eth.receipts[str(_hash_for(0))] = _transfer_receipt(WALLET, 1.0)
    st = await P._settle_one(_ctx(eth), period_id="h1", account_id=acct, address=WALLET,
                             den=1.0, aipg=1.0, stored_nonce=None)
    assert st == "sent"
    assert (await P._row("h1", acct))["status"] == "sent"


@pytest.mark.asyncio
async def test_status1_without_transfer_is_manual_review(db):
    eth = _FakeEth()
    acct = uuid.uuid4()
    eth.receipts[str(_hash_for(0))] = {"status": 1, "logs": []}
    st = await P._settle_one(_ctx(eth), period_id="h1", account_id=acct, address=WALLET,
                             den=1.0, aipg=1.0, stored_nonce=None)
    assert st == "manual_review"


# ── 4. consumed nonce → proof decides sent vs manual_review ─────────────────
@pytest.mark.asyncio
async def test_consumed_nonce_without_proof_is_manual_review(db):
    eth = _FakeEth(); eth.mined = 6
    acct = uuid.uuid4()
    await P._write("h1", acct, address=WALLET, den=1, aipg=1, status="pending", tx_hash=str(_hash_for(5)), nonce=5)
    st = await P._settle_one(_ctx(eth), period_id="h1", account_id=acct, address=WALLET,
                             den=1.0, aipg=1.0, stored_nonce=5, stored_tx=str(_hash_for(5)))
    assert st == "manual_review"
    assert eth.broadcasts == []  # never re-sent


@pytest.mark.asyncio
async def test_consumed_nonce_with_proof_is_sent_no_rebroadcast(db):
    eth = _FakeEth(); eth.mined = 6
    eth.receipts[str(_hash_for(5))] = _transfer_receipt(WALLET, 1.0)
    acct = uuid.uuid4()
    await P._write("h1", acct, address=WALLET, den=1, aipg=1, status="pending", tx_hash=str(_hash_for(5)), nonce=5)
    st = await P._settle_one(_ctx(eth), period_id="h1", account_id=acct, address=WALLET,
                             den=1.0, aipg=1.0, stored_nonce=5, stored_tx=str(_hash_for(5)))
    assert st == "sent"
    assert eth.broadcasts == []  # settled by nonce proof, no second transfer


# ── 5. rerun after 'sent' does not rebroadcast ──────────────────────────────
@pytest.mark.asyncio
async def test_send_period_skips_already_sent(db, monkeypatch):
    acct = uuid.uuid4()
    await P._write("h1", acct, address=WALLET, den=1, aipg=1, status="sent", tx_hash="0xabc", nonce=3, paid=True)

    async def fake_agg(start, end, **kw):
        return [{"account_id": str(acct), "den": 5.0, "payout_address": WALLET}]
    monkeypatch.setattr(P, "aggregate_den_by_account", fake_agg)
    eth = _FakeEth()
    monkeypatch.setattr(P, "_ctx", lambda: _ctx(eth))
    monkeypatch.setattr(P, "BASE_RPC_URL", "x"); monkeypatch.setattr(P, "TREASURY_PK", "x")
    res = await P.send_period(None, None, 100.0, "h1")
    assert res["skipped"] == 1 and res.get("sent", 0) == 0
    assert eth.broadcasts == []


# ── 6. reconcile settles a pending row via the nonce check, no second tx ────
@pytest.mark.asyncio
async def test_reconcile_settles_pending_via_nonce(db, monkeypatch):
    acct = uuid.uuid4()
    await P._write("h1", acct, address=WALLET, den=1, aipg=1.0, status="pending", tx_hash=str(_hash_for(9)), nonce=9)
    eth = _FakeEth(); eth.mined = 10
    eth.receipts[str(_hash_for(9))] = _transfer_receipt(WALLET, 1.0)
    monkeypatch.setattr(P, "_ctx", lambda: _ctx(eth))
    res = await P.reconcile_and_retry()
    assert res["settled"] == 1
    assert eth.broadcasts == []
    assert (await P._row("h1", acct))["status"] == "sent"


# ── 1 (Postgres only). concurrent runners can't both hold the lock ──────────
@pytest.mark.skipif(not _PG.startswith("postgresql"),
                    reason="needs Postgres (pg_advisory_lock + concurrent connections)")
@pytest.mark.asyncio
async def test_advisory_lock_serializes_runners(db):
    s1 = database._session_factory()
    s2 = database._session_factory()
    try:
        assert await P._try_payout_lock(s1) is True
        assert await P._try_payout_lock(s2) is False
    finally:
        await s1.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": P._PAYOUT_LOCK_KEY})
        await s1.commit()
        await s1.close(); await s2.close()

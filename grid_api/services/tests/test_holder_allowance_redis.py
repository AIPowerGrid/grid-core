# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Holder daily allowance against a real, disposable Redis and a fake chain.

Proves the rules that make the allowance safe to switch on: one fixed snapshot
block for every wallet, one claim per wallet and per account, a network-wide
daily budget, and nothing granted when the chain or Redis cannot answer."""

import asyncio
import shutil
import subprocess
import tempfile

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from grid_api.services import credits, free_credits, holdings

UNIT = 10 ** 18
A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40
SNAPSHOT_BLOCK = 777


class Chain:
    """Balances now and at the snapshot block, with read counters."""

    def __init__(self):
        self.now: dict[str, int] = {}
        self.snapshot: dict[str, int] = {}
        self.block_searches = 0
        self.snapshot_reads = 0
        self.fail_snapshot = False

    async def balance_now(self, wallet):
        return self.now.get(wallet, 0) * UNIT

    async def balance_at(self, wallet, block):
        assert block == SNAPSHOT_BLOCK
        self.snapshot_reads += 1
        if self.fail_snapshot:
            raise RuntimeError("rpc down")
        return self.snapshot.get(wallet, 0) * UNIT

    async def block_before(self, ts):
        assert ts == free_credits._day_start_ts()
        self.block_searches += 1
        return SNAPSHOT_BLOCK

    def hold(self, wallet, amount):
        self.now[wallet] = amount
        self.snapshot[wallet] = amount


@pytest_asyncio.fixture
async def redis(monkeypatch):
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server required for holder allowance proof")
    with tempfile.TemporaryDirectory(prefix="aipg-h-", dir="/tmp") as directory:
        path = f"{directory}/redis.sock"
        process = subprocess.Popen(
            [binary, "--port", "0", "--unixsocket", path, "--unixsocketperm", "700",
             "--save", "", "--appendonly", "no", "--dir", directory],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        client = Redis(unix_socket_path=path, decode_responses=True, socket_timeout=3)
        try:
            for _ in range(100):
                assert process.poll() is None
                try:
                    if await client.ping():
                        break
                except ConnectionError:
                    await asyncio.sleep(0.02)
            else:
                pytest.fail("disposable Redis did not start")
            monkeypatch.setattr(free_credits, "get_redis", lambda: client)
            yield client
        finally:
            await client.aclose()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture
def chain(monkeypatch):
    fake = Chain()
    monkeypatch.setattr(holdings, "aipg_balance_raw", fake.balance_now)
    monkeypatch.setattr(holdings, "aipg_balance_raw_at", fake.balance_at)
    monkeypatch.setattr(holdings, "block_before", fake.block_before)
    monkeypatch.setattr(free_credits, "FREE_ENABLED", True)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_TIERS_RAW", "1000:50000,10000:200000,100000:500000")
    monkeypatch.setattr(free_credits, "FREE_HOLDER_DAILY_BUDGET_MICRO", 10_000_000)

    async def no_google(_account_id):
        return False

    monkeypatch.setattr(free_credits, "_has_verified_google", no_google)
    return fake


@pytest.mark.asyncio
async def test_tier_follows_the_lower_of_snapshot_and_current_balance(redis, chain):
    chain.hold(A, 150_000)
    chain.now[B], chain.snapshot[B] = 150_000, 12_000   # bought more today
    chain.now[C], chain.snapshot[C] = 1_500, 150_000    # sold most today
    assert await free_credits.holder_bonus_micro("acct-a", A) == 500_000
    assert await free_credits.holder_bonus_micro("acct-b", B) == 200_000
    assert await free_credits.holder_bonus_micro("acct-c", C) == 50_000


@pytest.mark.asyncio
async def test_tokens_moved_today_do_not_qualify_the_second_wallet(redis, chain):
    chain.hold(A, 100_000)
    assert await free_credits.holder_bonus_micro("acct-a", A) == 500_000
    # The same tokens arrive in a second wallet after the snapshot block.
    chain.now[A], chain.now[B] = 0, 100_000
    assert await free_credits.holder_bonus_micro("acct-b", B) == 0
    # The first account keeps what it claimed for today.
    assert await free_credits.holder_bonus_micro("acct-a", A) == 500_000


@pytest.mark.asyncio
async def test_wallet_bought_today_waits_for_the_next_snapshot(redis, chain):
    chain.now[A] = 500_000
    assert await free_credits.holder_bonus_micro("acct-a", A) == 0
    assert await free_credits.daily_cap_micro("acct-a", A) == 0


@pytest.mark.asyncio
async def test_one_wallet_serves_one_account_per_day(redis, chain):
    chain.hold(A, 10_000)
    assert await free_credits.holder_bonus_micro("acct-1", A) == 200_000
    assert await free_credits.holder_bonus_micro("acct-2", A) == 0
    assert await free_credits.holder_bonus_micro("acct-2", A.upper().replace("0X", "0x")) == 0


@pytest.mark.asyncio
async def test_one_account_claims_once_even_with_a_bigger_wallet_later(redis, chain):
    chain.hold(A, 1_000)
    chain.hold(B, 100_000)
    assert await free_credits.holder_bonus_micro("acct-1", A) == 50_000
    assert await free_credits.holder_bonus_micro("acct-1", B) == 50_000
    assert await redis.get(f"grid:holderbudget:{free_credits._day()}") == "50000"


@pytest.mark.asyncio
async def test_network_budget_is_a_hard_ceiling(redis, chain, monkeypatch):
    monkeypatch.setattr(free_credits, "FREE_HOLDER_DAILY_BUDGET_MICRO", 600_000)
    chain.hold(A, 100_000)
    chain.hold(B, 100_000)
    chain.hold(C, 1_000)
    assert await free_credits.holder_bonus_micro("acct-a", A) == 500_000
    assert await free_credits.holder_bonus_micro("acct-b", B) == 0      # would pass the budget
    assert await free_credits.holder_bonus_micro("acct-c", C) == 50_000  # still fits
    assert await redis.get(f"grid:holderbudget:{free_credits._day()}") == "550000"


@pytest.mark.asyncio
async def test_concurrent_first_reads_claim_once(redis, chain):
    chain.hold(A, 100_000)
    results = await asyncio.gather(*[
        free_credits.holder_bonus_micro(f"acct-{i}", A) for i in range(12)
    ])
    assert sorted(results) == [0] * 11 + [500_000]
    assert await redis.get(f"grid:holderbudget:{free_credits._day()}") == "500000"


@pytest.mark.asyncio
async def test_claimed_day_needs_no_further_chain_reads(redis, chain):
    chain.hold(A, 100_000)
    assert await free_credits.holder_bonus_micro("acct-a", A) == 500_000
    searches, reads = chain.block_searches, chain.snapshot_reads
    chain.fail_snapshot = True
    for _ in range(5):
        assert await free_credits.daily_cap_micro("acct-a", None) == 500_000
    assert (chain.block_searches, chain.snapshot_reads) == (searches, reads)
    # A second wallet on the same day reuses the shared snapshot block.
    chain.fail_snapshot = False
    chain.hold(B, 1_000)
    assert await free_credits.holder_bonus_micro("acct-b", B) == 50_000
    assert chain.block_searches == searches


@pytest.mark.asyncio
async def test_chain_failure_grants_nothing_and_is_retried(redis, chain):
    chain.hold(A, 100_000)
    chain.fail_snapshot = True
    assert await free_credits.holder_bonus_micro("acct-a", A) == 0
    assert await redis.get(f"grid:holderbudget:{free_credits._day()}") is None
    chain.fail_snapshot = False
    assert await free_credits.holder_bonus_micro("acct-a", A) == 500_000


@pytest.mark.asyncio
async def test_slow_chain_grants_nothing(redis, chain, monkeypatch):
    chain.hold(A, 100_000)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_LOOKUP_SECONDS", 0.05)

    async def slow(_wallet, _block):
        await asyncio.sleep(1)
        return 100_000 * UNIT

    monkeypatch.setattr(holdings, "aipg_balance_raw_at", slow)
    assert await free_credits.holder_bonus_micro("acct-a", A) == 0


@pytest.mark.asyncio
async def test_slow_block_search_finishes_for_the_next_caller(redis, chain, monkeypatch):
    chain.hold(A, 100_000)
    monkeypatch.setattr(free_credits, "FREE_HOLDER_LOOKUP_SECONDS", 0.05)
    release = asyncio.Event()

    async def slow_search(_ts):
        chain.block_searches += 1
        await release.wait()
        return SNAPSHOT_BLOCK

    monkeypatch.setattr(holdings, "block_before", slow_search)
    assert await free_credits.holder_bonus_micro("acct-a", A) == 0   # gave up waiting
    assert await free_credits.holder_bonus_micro("acct-a", A) == 0   # joins the same search
    release.set()
    await asyncio.sleep(0.05)
    assert await free_credits.holder_bonus_micro("acct-a", A) == 500_000
    assert chain.block_searches == 1
    assert free_credits._block_searches == {}


@pytest.mark.asyncio
async def test_redis_failure_grants_nothing(chain, monkeypatch):
    def down():
        raise RuntimeError("redis down")

    chain.hold(A, 100_000)
    monkeypatch.setattr(free_credits, "get_redis", down)
    assert await free_credits.holder_bonus_micro("acct-a", A) == 0


@pytest.mark.asyncio
async def test_allowance_is_spendable_through_the_free_bucket(redis, chain):
    chain.hold(A, 10_000)
    assert await free_credits.available_micro("acct-a", A) == 200_000
    assert await free_credits.consume("acct-a", A, 150_000, ref="job-1") == 150_000
    assert await free_credits.consume("acct-a", A, 150_000, ref="job-2") == 50_000
    assert await free_credits.available_micro("acct-a", A) == 0
    assert await free_credits.release("acct-a", "job-1") == 150_000
    assert await free_credits.available_micro("acct-a", A) == 150_000


@pytest.mark.asyncio
async def test_credit_summary_reports_bonus_and_tiers(redis, chain, monkeypatch):
    chain.hold(A, 10_000)

    async def zero(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(credits, "get_balance", zero)
    monkeypatch.setattr(credits.promotions, "available_micro", zero)
    summary = await credits.account_credit_summary({"account_id": "acct-a", "wallet": A})
    free = summary["free"]
    assert free["holder_bonus_active"] is True
    assert free["holder_bonus_micro"] == 200_000
    assert free["daily_cap_micro"] == 200_000
    assert free["holder_tiers"][0] == {"min_aipg": 1_000, "daily_micro": 50_000, "daily_usd": 0.05}
    assert [t["min_aipg"] for t in free["holder_tiers"]] == [1_000, 10_000, 100_000]


@pytest.mark.asyncio
async def test_block_before_finds_last_block_before_time(monkeypatch):
    times = [1000 + 2 * n if n < 500 else 2000 + (n - 500) for n in range(900)]
    calls = 0

    async def rpc(method, params):
        nonlocal calls
        calls += 1
        assert method == "eth_getBlockByNumber"
        n = len(times) - 1 if params[0] == "latest" else int(params[0], 16)
        return {"number": hex(n), "timestamp": hex(times[n])}

    monkeypatch.setattr(holdings, "_rpc", rpc)
    monkeypatch.setattr(holdings, "_block_cache", {})
    assert await holdings.block_before(1500) == 249      # block 250 is exactly 1500
    assert await holdings.block_before(2100) == 599
    before = calls
    assert await holdings.block_before(2100) == 599      # cached
    assert calls == before
    with pytest.raises(RuntimeError):
        await holdings.block_before(times[-1] + 1)       # not in the past
    with pytest.raises(RuntimeError):
        await holdings.block_before(1000)                # before the chain


@pytest.mark.asyncio
async def test_snapshot_balance_reads_the_named_block_and_raises_on_failure(monkeypatch):
    seen = []

    async def eth_call(to, data, block="latest"):
        seen.append((to, data, block))
        return hex(42 * UNIT)

    monkeypatch.setattr(holdings, "_eth_call", eth_call)
    monkeypatch.setattr(holdings, "_snapshot_cache", {})
    assert await holdings.aipg_balance_raw_at(A, 777) == 42 * UNIT
    assert await holdings.aipg_balance_raw_at(A, 777) == 42 * UNIT
    assert seen == [(holdings.AIPG_TOKEN, "0x70a08231" + A[2:].rjust(64, "0"), hex(777))]

    async def broken(*_args, **_kwargs):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(holdings, "_eth_call", broken)
    with pytest.raises(RuntimeError):
        await holdings.aipg_balance_raw_at(B, 777)
    with pytest.raises(ValueError):
        await holdings.aipg_balance_raw_at("nope", 777)

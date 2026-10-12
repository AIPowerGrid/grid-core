# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded nonce correlation with synthetic RPC, not a received payment."""

import asyncio
import copy

import pytest

from grid_api.services import video_payment_proofs as proofs
from grid_api.services.tests import test_video_payment_proofs as support

prepared = support.prepared
admission = support.admission
registry = support.registry
synthetic_dependencies = support.synthetic_dependencies
pytestmark = pytest.mark.asyncio
_END = "0x" + "e" * 64


class SearchRPC(support.RPCFixture):
    def __init__(self, row, account):
        super().__init__(row, account)
        self.logs = [copy.deepcopy(self.receipt["logs"][0])]
        self.end = {"number": "0x6e", "hash": _END}
        self.search_reorg = False

    async def read(self, method, params):
        if method == "eth_getLogs":
            self.calls.append((method, copy.deepcopy(params)))
            if self.before_read:
                await self.before_read(method, params)
            if self.fault:
                raise self.fault
            assert params == [{"fromBlock": "0x50", "toBlock": "0x6e",
                "address": self.typed["domain"]["verifyingContract"],
                "topics": self.receipt["logs"][0]["topics"]}]
            return copy.deepcopy(self.logs)
        if method == "eth_getBlockByNumber" and params[0] == self.end["number"]:
            self.calls.append((method, copy.deepcopy(params)))
            value = copy.deepcopy(self.end)
            if self.search_reorg and any(call[0] == "eth_getLogs" for call in self.calls):
                value["hash"] = "0x" + "f" * 64
            return value
        return await super().read(method, params)


async def locate(row, account, first, second, **overrides):
    async with support.sources(first, second) as sources:
        return await proofs.locate_authorization(row, payer=account.address, sources=sources,
            from_block=overrides.pop("from_block", 80), to_block=overrides.pop("to_block", 110),
            confirmation_tag=overrides.pop("confirmation_tag", "safe"), **overrides)


@pytest.mark.parametrize("tag", ["safe", "finalized"])
async def test_exact_nonce_lookup_returns_only_original_transaction_not_payment_authority(prepared, tag):
    row, account = prepared
    first, second = SearchRPC(row, account), SearchRPC(row, account)
    assert await locate(row, account, first, second, confirmation_tag=tag) == support._TX
    assert sum(method == "eth_getLogs" for method, _ in first.calls) == 1
    assert all(method not in {"eth_sendTransaction", "eth_sendRawTransaction"}
               for method, _ in first.calls + second.calls)
    assert proofs._active == 0


@pytest.mark.parametrize("mutation", ["payer", "nonce", "token", "removed", "data", "block", "hash", "extra-topic", "duplicate"])
async def test_wrong_or_ambiguous_nonce_logs_never_correlate(prepared, mutation):
    row, account = prepared
    first, second = SearchRPC(row, account), SearchRPC(row, account)
    log = first.logs[0]
    if mutation == "payer":
        log["topics"][1] = "0x" + "0" * 24 + "4" * 40
    elif mutation == "nonce":
        log["topics"][2] = "0x" + "4" * 64
    elif mutation == "token":
        log["address"] = "0x" + "4" * 40
    elif mutation == "removed":
        log["removed"] = True
    elif mutation == "data":
        log["data"] = "0x00"
    elif mutation == "block":
        log["blockNumber"] = "0x4f"
    elif mutation == "hash":
        log["blockHash"] = _END
    elif mutation == "extra-topic":
        log["topics"].append(_END)
    else:
        first.logs.append(copy.deepcopy(log))
    with pytest.raises(proofs.VideoPaymentProofError):
        await locate(row, account, first, second)
    assert proofs._active == 0


async def test_two_empty_searches_are_pending_not_absence_or_retry_permission(prepared):
    row, account = prepared
    first, second = SearchRPC(row, account), SearchRPC(row, account)
    first.logs = second.logs = []
    with pytest.raises(proofs.VideoPaymentPending, match="retain attempt"):
        await locate(row, account, first, second)


@pytest.mark.parametrize("change", ["one-empty", "other-tx", "chain", "end-anchor", "reorg", "unconfirmed"])
async def test_search_disagreement_and_changed_anchors_fail_closed(prepared, change):
    row, account = prepared
    first, second = SearchRPC(row, account), SearchRPC(row, account)
    if change == "one-empty":
        second.logs = []
    elif change == "other-tx":
        second.logs[0]["transactionHash"] = _END
    elif change == "chain":
        second.chain = "0x1"
    elif change == "end-anchor":
        second.end["hash"] = "0x" + "f" * 64
    elif change == "reorg":
        second.search_reorg = True
    else:
        second.anchor["number"] = "0x6d"
    with pytest.raises(proofs.VideoPaymentProofError):
        await locate(row, account, first, second)


@pytest.mark.parametrize("overrides", [{"from_block": True}, {"to_block": False}, {"from_block": -1},
    {"from_block": 111}, {"from_block": 0, "to_block": 1024}, {"to_block": 2**64}, {"confirmation_tag": "latest"}])
async def test_bad_search_policy_rejects_before_network(prepared, overrides):
    row, account = prepared
    first, second = SearchRPC(row, account), SearchRPC(row, account)
    with pytest.raises(proofs.VideoPaymentProofError):
        await locate(row, account, first, second, **overrides)
    assert not first.calls and not second.calls


async def test_search_capacity_and_cancellation_release_the_shared_proof_slot(prepared, monkeypatch):
    row, account = prepared
    first, second = SearchRPC(row, account), SearchRPC(row, account)
    monkeypatch.setattr(proofs, "_active", 2)
    with pytest.raises(proofs.VideoPaymentUnavailable):
        await locate(row, account, first, second)
    assert proofs._active == 2 and not first.calls
    monkeypatch.setattr(proofs, "_active", 0)
    entered, wait = asyncio.Event(), asyncio.Event()

    async def block(method, _params):
        if method == "eth_getLogs":
            entered.set()
            await wait.wait()

    first.before_read = block
    task = asyncio.create_task(locate(row, account, first, second))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert proofs._active == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert proofs._active == 0

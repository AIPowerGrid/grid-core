# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Historical RPC/ABI/receipt controls; no real chain or funded admission."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from datetime import timedelta

import httpx
import pytest
from eth_abi import decode, encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from web3 import Web3
from x402.mechanisms.evm.constants import TRANSFER_WITH_AUTHORIZATION_VRS_ABI

from grid_api import database
from grid_api.services import video_eip3009 as bindings
from grid_api.services import video_payment_proofs as proofs
from grid_api.services.tests import test_video_eip3009 as binding_tests

prepared = binding_tests.prepared
admission = binding_tests.admission
registry = binding_tests.registry
synthetic_dependencies = binding_tests.synthetic_dependencies
pytestmark = pytest.mark.asyncio

_TX = "0x" + "a" * 64
_BLOCK = "0x" + "b" * 64
_ANCHOR = "0x" + "c" * 64
_RELAYER = "0x" + "3" * 40
_TYPES = ["address", "address", "uint256", "uint256", "uint256", "bytes32", "uint8", "bytes32", "bytes32"]


class RPCFixture:
    def __init__(self, row, account):
        self.row, self.account = row, account
        request = bindings.signing_request(row, payer=account.address)
        auth, typed = request["authorization"], request["typed_data"]
        self.typed = typed
        signed = account.sign_message(encode_typed_data(full_message=typed))
        values = [Web3.to_checksum_address(auth["from"]), Web3.to_checksum_address(auth["to"]),
                  int(auth["value"]), int(auth["validAfter"]), int(auth["validBefore"]),
                  bytes.fromhex(auth["nonce"][2:]), signed.v, signed.r.to_bytes(32, "big"), signed.s.to_bytes(32, "big")]
        # Use the locked official SDK ABI and actual Web3 encoder, not the verifier's constants.
        contract = Web3().eth.contract(abi=TRANSFER_WITH_AUTHORIZATION_VRS_ABI)
        calldata = contract.encode_abi("transferWithAuthorization", args=values)
        chain = row["snapshot"]["payment"]["eip712_domain"]["chainId"]
        token = row["snapshot"]["payment"]["asset"]
        common = {"blockNumber": "0x64", "blockHash": _BLOCK, "transactionIndex": "0x2"}
        self.tx = {**common, "hash": _TX, "chainId": hex(chain), "value": "0x0", "to": token,
                   "from": _RELAYER, "input": calldata}
        log = {**common, "transactionHash": _TX, "removed": False, "address": token}
        payer_topic, recipient_topic = "0x" + "0" * 24 + auth["from"][2:], "0x" + "0" * 24 + auth["to"][2:]
        self.receipt = {**common, "transactionHash": _TX, "status": "0x1", "to": token, "from": _RELAYER,
            "logs": [
                {**log, "logIndex": "0x4", "topics": ["0x" + keccak(text="AuthorizationUsed(address,bytes32)").hex(),
                                                       payer_topic, auth["nonce"]], "data": "0x"},
                {**log, "logIndex": "0x5", "topics": ["0x" + keccak(text="Transfer(address,address,uint256)").hex(),
                                                       payer_topic, recipient_topic],
                 "data": "0x" + int(auth["value"]).to_bytes(32, "big").hex()},
            ]}
        self.block = {"number": "0x64", "hash": _BLOCK, "timestamp": hex(row["snapshot"]["created"] + 1)}
        self.anchor = {"number": "0x78", "hash": _ANCHOR}
        self.chain = hex(chain)
        self.token_code, self.payer_code = "0x6000", "0x"
        self.domain = "0x" + encode_typed_data(full_message=typed).header.hex()
        self.decimals, self.state = "0x" + (6).to_bytes(32, "big").hex(), "0x" + (1).to_bytes(32, "big").hex()
        self.calls, self.reorg, self.fault = [], False, None
        self.before_read = None

    async def read(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if self.before_read:
            await self.before_read(method, params)
        if self.fault:
            raise self.fault
        if method == "eth_chainId":
            return self.chain
        if method == "eth_getTransactionByHash":
            return copy.deepcopy(self.tx)
        if method == "eth_getTransactionReceipt":
            return copy.deepcopy(self.receipt)
        if method == "eth_getBlockByNumber":
            if params[0] in {"safe", "finalized"} or params[0] == self.anchor["number"]:
                return copy.deepcopy(self.anchor)
            value = copy.deepcopy(self.block)
            if self.reorg and any(call[0] == "eth_call" for call in self.calls):
                value["hash"] = "0x" + "d" * 64
            return value
        if method in {"eth_getCode", "eth_call"}:
            assert params[1] == {"blockHash": _BLOCK, "requireCanonical": True}
        if method == "eth_getCode":
            return self.token_code if params[0] == self.typed["domain"]["verifyingContract"] else self.payer_code
        if method == "eth_call":
            assert params[0]["to"] == self.typed["domain"]["verifyingContract"]
            data = params[0]["data"]
            if data == "0x" + keccak(text="DOMAIN_SEPARATOR()")[:4].hex():
                return self.domain
            if data == "0x" + keccak(text="decimals()")[:4].hex():
                return self.decimals
            assert data == "0x" + (keccak(text="authorizationState(address,bytes32)")[:4] + encode(
                ["address", "bytes32"], [self.typed["message"]["from"], bytes.fromhex(self.typed["message"]["nonce"][2:])])).hex()
            return self.state
        raise AssertionError("unexpected RPC method")


    async def handle(self, request):
        assert request.url.scheme == "https"
        assert request.url.host in {"rpc-one.example", "rpc-two.example"}
        message = json.loads(request.content)
        assert message["jsonrpc"] == "2.0" and message["id"] == 1
        result = await self.read(message["method"], message["params"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


@asynccontextmanager
async def sources(first, second):
    async with httpx.AsyncClient(transport=httpx.MockTransport(first.handle)) as one:
        async with httpx.AsyncClient(transport=httpx.MockTransport(second.handle)) as two:
            yield (proofs.PaymentRPC("https://rpc-one.example/", one),
                   proofs.PaymentRPC("https://rpc-two.example/", two))


async def prove(row, account, first, second, **kwargs):
    async with sources(first, second) as rpc_sources:
        return await proofs.prove_transfer(row, payer=account.address, transaction_hash=_TX,
            sources=rpc_sources, confirmation_tag=kwargs.pop("confirmation_tag", "safe"),
            min_confirmations=kwargs.pop("min_confirmations", 12), **kwargs)


@pytest.mark.parametrize("tag", ["safe", "finalized"])
async def test_actual_signature_sdk_abi_exact_events_and_dual_anchor(prepared, tag, monkeypatch):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)

    async def no_sql():
        raise AssertionError("receipt evidence has no database authority")

    monkeypatch.setattr(database, "new_session", no_sql)
    result = await prove(row, account, first, second, confirmation_tag=tag)
    assert result.amount_micro == row["amount_micro"] == 7500
    assert result.authorization_nonce == row["authorization_nonce"]
    assert result.payer == account.address.lower() != first.tx["from"]
    assert result.confirmation_tag == tag and result.confirmation_block_hash == _ANCHOR
    assert result.transfer_log_index == 5 and result.authorization_log_index == 4
    assert not hasattr(result, "funded") and not hasattr(result, "signature")
    signature = binding_tests.sign(row, account)["signature"]
    assert signature not in repr(result) and first.tx["input"] not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.amount_micro = 1
    assert all(method.startswith("eth_") and method != "eth_sendRawTransaction" for method, _ in first.calls + second.calls)


async def test_success_remains_reconcilable_after_signing_window_expires(prepared, monkeypatch):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    payload = binding_tests.sign(row, account)
    monkeypatch.setattr(bindings, "_now", lambda: row["expires"] + timedelta(days=1))
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.verify(row, payload, expected_payer=account.address)
    result = await prove(row, account, first, second)
    assert result.block_timestamp < int(row["expires"].timestamp())


@pytest.mark.parametrize("target,field,value", [
    ("tx", "chainId", "0x1"), ("tx", "to", "0x" + "2" * 40), ("tx", "value", "0x1"),
    ("tx", "hash", "0x" + "e" * 64), ("tx", "blockHash", "0x" + "e" * 64),
    ("tx", "blockNumber", "0x63"), ("tx", "transactionIndex", "0x3"),
    ("tx", "from", "0x" + "4" * 40), ("receipt", "status", "0x0"),
    ("receipt", "transactionHash", "0x" + "e" * 64), ("receipt", "to", "0x" + "2" * 40),
    ("block", "hash", "0x" + "e" * 64), ("block", "number", "0x63"),
    ("receipt", "transactionIndex", True), ("tx", "value", "0x00"),
])
async def test_wrong_or_malformed_transaction_binding_is_not_payment_proof(prepared, target, field, value):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    getattr(first, target)[field] = value
    with pytest.raises(proofs.VideoPaymentProofError):
        await prove(row, account, first, second)


@pytest.mark.parametrize("mutation", ["no_transfer", "no_nonce", "other_nonce", "other_payer", "other_recipient",
    "other_amount", "other_token", "duplicated_transfer", "duplicated_nonce", "duplicate_index", "removed",
    "wrong_block", "wrong_transaction", "wrong_index", "extra_topic", "bad_padding", "huge_logs", "extra_usdc_transfer"])
async def test_successful_receipt_alone_cannot_bypass_event_binding(prepared, mutation):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    logs = first.receipt["logs"]
    if mutation == "no_transfer":
        logs.pop()
    elif mutation == "no_nonce":
        logs.pop(0)
    elif mutation == "other_nonce":
        logs[0]["topics"][2] = "0x" + "e" * 64
    elif mutation == "other_payer":
        logs[0]["topics"][1] = "0x" + "0" * 24 + "2" * 40
    elif mutation == "other_recipient":
        logs[1]["topics"][2] = "0x" + "0" * 24 + "2" * 40
    elif mutation == "other_amount":
        logs[1]["data"] = "0x" + (7501).to_bytes(32, "big").hex()
    elif mutation == "other_token":
        logs[1]["address"] = "0x" + "2" * 40
    elif mutation in {"duplicated_transfer", "duplicated_nonce", "extra_usdc_transfer"}:
        extra = copy.deepcopy(logs[0 if mutation == "duplicated_nonce" else 1])
        extra["logIndex"] = "0x6"
        if mutation == "extra_usdc_transfer":
            extra["topics"][2] = "0x" + "0" * 24 + "2" * 40
        logs.append(extra)
    elif mutation == "duplicate_index":
        logs[1]["logIndex"] = logs[0]["logIndex"]
    elif mutation == "removed":
        logs[1]["removed"] = True
    elif mutation == "wrong_block":
        logs[1]["blockHash"] = "0x" + "e" * 64
    elif mutation == "wrong_transaction":
        logs[1]["transactionHash"] = "0x" + "e" * 64
    elif mutation == "wrong_index":
        logs[1]["transactionIndex"] = "0x3"
    elif mutation == "extra_topic":
        logs[0]["topics"].append("0x" + "e" * 64)
    elif mutation == "bad_padding":
        logs[1]["topics"][1] = "0x" + "e" * 24 + account.address[2:]
    else:
        first.receipt["logs"] *= 65
    with pytest.raises(proofs.VideoPaymentProofError):
        await prove(row, account, first, second)


@pytest.mark.parametrize("attribute,value", [("chain", "0x1"), ("domain", "0x" + "0" * 64),
    ("decimals", "0x" + (18).to_bytes(32, "big").hex()), ("state", "0x" + "0" * 64),
    ("token_code", "0x"), ("payer_code", "0x6000"), ("payer_code", "0xef0100" + "2" * 40), ("reorg", True)])
async def test_live_contract_and_canonical_block_controls(prepared, attribute, value):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    setattr(second, attribute, value)
    with pytest.raises(proofs.VideoPaymentProofError):
        await prove(row, account, first, second)


@pytest.mark.parametrize("kind", ["other_signer", "amount", "nonce", "selector", "suffix", "truncated"])
async def test_calldata_must_match_quote_and_its_real_signature(prepared, kind):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    raw = bytes.fromhex(first.tx["input"][2:])
    values = list(decode(_TYPES, raw[4:]))
    if kind == "other_signer":
        signed = Account.create().sign_message(encode_typed_data(full_message=first.typed))
        values[6:] = [signed.v, signed.r.to_bytes(32, "big"), signed.s.to_bytes(32, "big")]
    elif kind == "amount":
        values[2] += 1
    elif kind == "nonce":
        values[5] = bytes(32)
    raw = raw[:4] + encode(_TYPES, values)
    if kind == "selector":
        raw = bytes(4) + raw[4:]
    elif kind == "suffix":
        raw += bytes(32)
    elif kind == "truncated":
        raw = raw[:-1]
    first.tx["input"] = "0x" + raw.hex()
    with pytest.raises(proofs.VideoPaymentProofError) as error:
        await prove(row, account, first, second)
    assert first.tx["input"] not in str(error.value)


@pytest.mark.parametrize("kind", ["missing_tx", "missing_receipt", "unmined", "confirmation"])
async def test_pending_is_not_terminal_or_permission_for_another_payment(prepared, kind):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    if kind == "missing_tx":
        first.tx = None
    elif kind == "missing_receipt":
        first.receipt = None
    elif kind == "unmined":
        first.tx["blockNumber"] = None
    else:
        first.anchor["number"] = "0x65"
    with pytest.raises(proofs.VideoPaymentPending):
        await prove(row, account, first, second)


async def test_future_or_prequote_mining_cannot_satisfy_the_authorization(prepared):
    row, account = prepared
    for timestamp in (row["snapshot"]["created"] - 1, row["snapshot"]["expires"]):
        first, second = RPCFixture(row, account), RPCFixture(row, account)
        first.block["timestamp"] = hex(timestamp)
        with pytest.raises(proofs.VideoPaymentProofError):
            await prove(row, account, first, second)


async def test_two_hosts_must_agree_despite_safe_head_advancing(prepared):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    second.anchor = {"number": "0x79", "hash": "0x" + "d" * 64}
    read = second.read

    async def advancing(method, params):
        if method == "eth_getBlockByNumber" and params[0] == "0x78":
            return {"number": "0x78", "hash": _ANCHOR}
        return await read(method, params)

    second.read = advancing
    assert (await prove(row, account, first, second)).confirmation_block_number == 120
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    second.receipt["logs"][1]["logIndex"] = "0x6"
    with pytest.raises(proofs.VideoPaymentProofError):
        await prove(row, account, first, second)


@pytest.mark.parametrize("origin", ["https://RPC-ONE.example.:443", "http://rpc-two.example/",
    "https://user:credential@rpc-two.example", "not-a-url"])
async def test_source_selection_is_operator_only_and_distinct(prepared, origin):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    async with sources(first, second) as rpc_sources:
        with pytest.raises(proofs.VideoPaymentProofError):
            await proofs.prove_transfer(row, payer=account.address, transaction_hash=_TX,
                sources=(rpc_sources[0], proofs.PaymentRPC(origin, rpc_sources[1].client)),
                confirmation_tag="safe", min_confirmations=12)
    assert not first.calls and not second.calls


async def test_idna_aliases_cannot_count_as_two_rpc_hosts(prepared):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    async with sources(first, second) as rpc_sources:
        with pytest.raises(proofs.VideoPaymentProofError):
            await proofs.prove_transfer(row, payer=account.address, transaction_hash=_TX,
                sources=(proofs.PaymentRPC("https://b\u00fccher.example", rpc_sources[0].client),
                         proofs.PaymentRPC("https://xn--bcher-kva.example", rpc_sources[1].client)),
                confirmation_tag="safe", min_confirmations=12)
    assert not first.calls and not second.calls


@pytest.mark.parametrize("tag,depth", [("latest", 12), ("safe", 0), ("safe", True), ("finalized", 257)])
async def test_no_implicit_confirmation_policy_downgrade(prepared, tag, depth):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    with pytest.raises(proofs.VideoPaymentProofError):
        await prove(row, account, first, second, confirmation_tag=tag, min_confirmations=depth)
    assert not first.calls and not second.calls


async def test_corrupt_or_legacy_quote_has_no_historical_proof_authority(prepared):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    corrupt = copy.deepcopy(row)
    corrupt["amount_micro"] += 1
    from grid_api.services import video_quotes as quotes
    legacy = copy.deepcopy(row["snapshot"])
    legacy["schema"] = "aipg.video-quote-binding.v1"
    legacy["payment"].pop("eip712_domain")
    for invalid in (corrupt, quotes.PreparedVideoQuote(quotes._canonical(legacy)).values):
        with pytest.raises(proofs.VideoPaymentProofError):
            await prove(invalid, account, first, second)
    assert not first.calls and not second.calls


async def test_input_snapshot_cannot_mutate_while_rpc_reads_yield(prepared):
    row, account = prepared
    original = copy.deepcopy(row)
    first, second = RPCFixture(row, account), RPCFixture(row, account)

    async def mutate(method, params):
        row["snapshot"]["payment"]["amount_micro"] = 1

    first.before_read = mutate
    assert (await prove(row, account, first, second)).amount_micro == original["amount_micro"]


async def test_transport_failure_is_private_and_capacity_recovers(prepared):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    first.fault = RuntimeError("rpc password and signed spending capability")
    with pytest.raises(proofs.VideoPaymentUnavailable) as error:
        await prove(row, account, first, second)
    assert "password" not in str(error.value) and proofs._active == 0
    first.fault = None
    assert (await prove(row, account, first, second)).amount_micro == 7500


async def test_cancelled_reader_releases_its_local_capacity(prepared):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    entered = asyncio.Event()

    async def wait(method, params):
        entered.set()
        await asyncio.Event().wait()

    first.before_read = wait
    task = asyncio.create_task(prove(row, account, first, second))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        assert proofs._active == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proofs._active == 0
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_capacity_is_bounded_without_waiting_or_rpc(prepared, monkeypatch):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    monkeypatch.setattr(proofs, "_active", 2)
    with pytest.raises(proofs.VideoPaymentUnavailable):
        await prove(row, account, first, second)
    assert proofs._active == 2 and not first.calls and not second.calls


@pytest.mark.parametrize("kind", ["redirect", "error_status", "rpc_error", "wrong_id", "bool_id", "duplicate",
    "nested_duplicate", "nonfinite", "overflow_real", "not_json", "oversize_header", "oversize_body"])
async def test_actual_http_envelope_is_bounded_unambiguous_and_private(prepared, kind):
    row, account = prepared
    calls = []
    streams = []

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b" " * (proofs._MAX_REPLY_BYTES + 1)

        async def aclose(self):
            self.closed = True

    async def handler(request):
        calls.append(str(request.url))
        if kind == "redirect":
            return httpx.Response(307, headers={"location": "https://unexpected.example/"})
        if kind == "error_status":
            return httpx.Response(500, text="private endpoint error")
        if kind == "oversize_header":
            return httpx.Response(200, content=b"{}", headers={"content-length": str(proofs._MAX_REPLY_BYTES + 1)})
        if kind == "oversize_body":
            stream = Body()
            streams.append(stream)
            return httpx.Response(200, stream=stream)
        raw = {
            "rpc_error": '{"jsonrpc":"2.0","id":1,"error":{"message":"private endpoint error"}}',
            "wrong_id": '{"jsonrpc":"2.0","id":2,"result":"0x2105"}',
            "bool_id": '{"jsonrpc":"2.0","id":true,"result":"0x2105"}',
            "duplicate": '{"jsonrpc":"2.0","id":1,"id":1,"result":"0x2105"}',
            "nested_duplicate": '{"jsonrpc":"2.0","id":1,"result":{"hash":"a","hash":"b"}}',
            "nonfinite": '{"jsonrpc":"2.0","id":1,"result":NaN}',
            "overflow_real": '{"jsonrpc":"2.0","id":1,"result":1e400}',
            "not_json": "private endpoint error",
        }[kind]
        return httpx.Response(200, content=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(proofs.VideoPaymentUnavailable) as error:
            await proofs.prove_transfer(row, payer=account.address, transaction_hash=_TX,
                sources=(proofs.PaymentRPC("https://rpc-one.example", client), proofs.PaymentRPC("https://rpc-two.example", client)),
                confirmation_tag="safe", min_confirmations=12)
    assert len(calls) == 1 and calls[0] == "https://rpc-one.example"
    assert "private endpoint error" not in str(error.value) and proofs._active == 0
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("limit", ["_RPC_SECONDS", "_PROOF_SECONDS"])
async def test_real_deadline_cancels_reader_and_preserves_unknown_outcome(prepared, monkeypatch, limit):
    row, account = prepared
    first, second = RPCFixture(row, account), RPCFixture(row, account)
    cancelled = asyncio.Event()

    async def wait(method, params):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    first.before_read = wait
    monkeypatch.setattr(proofs, limit, 0.02)
    with pytest.raises(proofs.VideoPaymentUnavailable):
        await prove(row, account, first, second)
    assert cancelled.is_set() and proofs._active == 0


async def test_two_live_readers_bound_a_third_without_an_unbounded_queue(prepared):
    row, account = prepared
    entered, gate = asyncio.Queue(), asyncio.Event()

    async def wait(method, params):
        await entered.put(True)
        await gate.wait()

    first, second = RPCFixture(row, account), RPCFixture(row, account)
    first.before_read = wait
    tasks = [asyncio.create_task(prove(row, account, first, second)) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.get(), 1)
        await asyncio.wait_for(entered.get(), 1)
        assert proofs._active == 2
        rejected = RPCFixture(row, account)
        with pytest.raises(proofs.VideoPaymentUnavailable):
            await prove(row, account, rejected, second)
        assert not rejected.calls
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert proofs._active == 0

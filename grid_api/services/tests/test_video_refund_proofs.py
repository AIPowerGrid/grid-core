# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Synthetic Safe RPC plus independent Solidity hash/ABI; not real refunds."""

import asyncio
import copy
from datetime import timedelta

import pytest
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import keccak
from web3 import Web3

from grid_api.services import video_payment_proofs as rpc
from grid_api.services import video_refund_proofs as proofs
from grid_api.services.tests import test_video_payment_proofs as support

prepared = support.prepared
admission = support.admission
registry = support.registry
synthetic_dependencies = support.synthetic_dependencies
pytestmark = pytest.mark.asyncio
_TX = "0x" + "d" * 64
_ZERO = "0x" + "0" * 40
_EXEC_ABI = [{"type": "function", "name": "execTransaction", "stateMutability": "payable",
    "inputs": [{"name": name, "type": kind} for name, kind in [
        ("to", "address"), ("value", "uint256"), ("data", "bytes"), ("operation", "uint8"),
        ("safeTxGas", "uint256"), ("baseGas", "uint256"), ("gasPrice", "uint256"),
        ("gasToken", "address"), ("refundReceiver", "address"), ("signatures", "bytes")]],
    "outputs": [{"name": "success", "type": "bool"}]}]


def solidity_hash(intent):
    """Safe 1.4.1 encodeTransactionData arithmetic, independent of EIP-712 SDK."""
    domain = keccak(encode(["bytes32", "uint256", "address"],
        [keccak(text="EIP712Domain(uint256 chainId,address verifyingContract)"), intent["chain_id"], intent["safe_address"]]))
    transfer = keccak(text="transfer(address,uint256)")[:4] + encode(["address", "uint256"],
        [intent["payer"], intent["amount_micro"]])
    body = keccak(encode(["bytes32", "address", "uint256", "bytes32", "uint8", "uint256", "uint256", "uint256",
        "address", "address", "uint256"],
        [keccak(text="SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,"
                     "uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)"),
         intent["asset"], 0, keccak(transfer), 0, 0, 0, 0, _ZERO, _ZERO, intent["safe_nonce"]]))
    return "0x" + keccak(b"\x19\x01" + domain + body).hex()


def intent_for(row, account):
    created = row["created"] + timedelta(seconds=3)
    return proofs.build_intent(row, {"payer": account.address.lower(), "proof_hash": "c" * 64,
        "transaction_hash": support._TX}, {"refund_due_at": created - timedelta(seconds=1),
        "refund_reason": "admission_closed", "receipt_claim_key": "e" * 64, "amount_micro": row["amount_micro"]},
        safe_nonce=7, created=created)


class RefundRPC:
    def __init__(self, intent):
        self.intent = intent
        self.chain = hex(intent["chain_id"])
        self.hash = solidity_hash(intent)
        assert self.hash == intent["safe_transaction_hash"]
        self.block = {"number": "0x64", "hash": support._BLOCK, "timestamp": hex(intent["prepared"] + 1)}
        self.anchor = {"number": "0x78", "hash": support._ANCHOR}
        self.version, self.decimals, self.nonce = "1.4.1", 6, intent["safe_nonce"] + 1
        self.code, self.calls, self.fault, self.reorg, self.before_read = "0x6000", [], None, False, None
        transfer = keccak(text="transfer(address,uint256)")[:4] + encode(["address", "uint256"],
            [intent["payer"], intent["amount_micro"]])
        signers = [Account.create(), Account.create()]
        signatures = b""
        for signer in sorted(signers, key=lambda account: account.address.lower()):
            signed = signer.sign_message(encode_defunct(primitive=bytes.fromhex(self.hash[2:])))
            signatures += signed.r.to_bytes(32, "big") + signed.s.to_bytes(32, "big") + bytes([signed.v + 4])
        values = [Web3.to_checksum_address(intent["asset"]), 0, transfer, 0, 0, 0, 0, _ZERO, _ZERO, signatures]
        self.exec_values = values
        common = {"blockNumber": "0x64", "blockHash": support._BLOCK, "transactionIndex": "0x2"}
        self.tx = {**common, "hash": _TX, "chainId": self.chain, "value": "0x0", "to": intent["safe_address"],
            "from": support._RELAYER, "input": Web3().eth.contract(abi=_EXEC_ABI).encode_abi("execTransaction", args=values)}
        log = {**common, "transactionHash": _TX, "removed": False}
        self.receipt = {**common, "transactionHash": _TX, "status": "0x1", "to": intent["safe_address"],
            "from": support._RELAYER, "logs": [
                {**log, "address": intent["asset"], "logIndex": "0x4", "topics": [
                    "0x" + keccak(text="Transfer(address,address,uint256)").hex(),
                    "0x" + "0" * 24 + intent["safe_address"][2:], "0x" + "0" * 24 + intent["payer"][2:]],
                 "data": "0x" + encode(["uint256"], [intent["amount_micro"]]).hex()},
                {**log, "address": intent["safe_address"], "logIndex": "0x5",
                 "topics": ["0x" + keccak(text="ExecutionSuccess(bytes32,uint256)").hex()],
                 "data": "0x" + encode(["bytes32", "uint256"], [bytes.fromhex(self.hash[2:]), 0]).hex()},
            ]}

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
            if params[0] in {"safe", "finalized", self.anchor["number"]}:
                return copy.deepcopy(self.anchor)
            value = copy.deepcopy(self.block)
            if self.reorg and any(call[0] == "eth_call" for call in self.calls):
                value["hash"] = "0x" + "f" * 64
            return value
        assert params[1] == {"blockHash": support._BLOCK, "requireCanonical": True}
        if method == "eth_getCode":
            return self.code
        assert method == "eth_call"
        target, data = params[0]["to"], params[0]["data"]
        if data == "0x" + keccak(text="VERSION()")[:4].hex():
            assert target == self.intent["safe_address"]
            return "0x" + encode(["string"], [self.version]).hex()
        if data == "0x" + keccak(text="decimals()")[:4].hex():
            assert target == self.intent["asset"]
            return "0x" + encode(["uint256"], [self.decimals]).hex()
        if data == "0x" + keccak(text="nonce()")[:4].hex():
            return "0x" + encode(["uint256"], [self.nonce]).hex()
        assert target == self.intent["safe_address"]
        expected = keccak(text="getTransactionHash(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,uint256)")[:4]
        expected += encode(["address", "uint256", "bytes", "uint8", "uint256", "uint256", "uint256", "address", "address", "uint256"],
                           [*self.exec_values[:-1], self.intent["safe_nonce"]])
        assert data == "0x" + expected.hex()
        return self.hash

    handle = support.RPCFixture.handle


async def prove(intent, one, two, **kwargs):
    async with support.sources(one, two) as sources:
        return await proofs.prove_refund(intent, transaction_hash=_TX, sources=sources,
            confirmation_tag=kwargs.pop("confirmation_tag", "safe"), min_confirmations=kwargs.pop("min_confirmations", 12), **kwargs)


@pytest.mark.parametrize("tag", ["safe", "finalized"])
async def test_exact_refund_sdk_abi_solidity_hash_and_two_anchors(prepared, tag):
    row, account = prepared
    intent = intent_for(row, account)
    one, two = RefundRPC(intent), RefundRPC(intent)
    result = await prove(intent, one, two, confirmation_tag=tag)
    assert result.intent_hash == proofs.quotes._hash(intent)
    assert result.amount_micro == row["amount_micro"] and result.payer == account.address.lower()
    assert result.safe_nonce == 7 and result.safe_transaction_hash == solidity_hash(intent)
    assert result.transfer_log_index == 4 and result.execution_log_index == 5
    assert not hasattr(result, "signature") and rpc._active == 0
    assert all(method not in {"eth_sendTransaction", "eth_sendRawTransaction"} for method, _ in one.calls + two.calls)


@pytest.mark.parametrize("fault", ["amount", "payer", "sender", "token", "no-transfer", "failure", "wrong-safe-hash",
    "execution-payment", "duplicate", "removed", "reordered", "calldata", "delegatecall", "gas-reimbursement",
    "suffix", "reverted", "wrong-safe", "wrong-chain", "early", "version", "decimals", "nonce", "hash-view",
    "missing-code", "reorg", "anchor", "unconfirmed"])
async def test_wrong_or_ambiguous_refund_never_passes(prepared, fault):
    row, account = prepared
    intent = intent_for(row, account)
    one, two = RefundRPC(intent), RefundRPC(intent)
    transfer, execution = one.receipt["logs"]
    if fault in {"amount", "execution-payment"}:
        target = transfer if fault == "amount" else execution
        target["data"] = "0x" + encode(["uint256"], [1]).hex() if fault == "amount" else (
            "0x" + encode(["bytes32", "uint256"], [bytes.fromhex(one.hash[2:]), 1]).hex())
    elif fault in {"payer", "sender"}:
        transfer["topics"][2 if fault == "payer" else 1] = "0x" + "0" * 24 + "3" * 40
    elif fault == "token":
        transfer["address"] = "0x" + "3" * 40
    elif fault == "no-transfer":
        one.receipt["logs"].pop(0)
    elif fault == "failure":
        execution["topics"] = ["0x" + keccak(text="ExecutionFailure(bytes32,uint256)").hex()]
    elif fault == "wrong-safe-hash":
        execution["data"] = "0x" + encode(["bytes32", "uint256"], [b"\x01" * 32, 0]).hex()
    elif fault == "duplicate":
        extra = copy.deepcopy(transfer)
        extra["logIndex"] = "0x6"
        one.receipt["logs"].append(extra)
    elif fault == "removed":
        transfer["removed"] = True
    elif fault == "reordered":
        transfer["logIndex"], execution["logIndex"] = "0x5", "0x4"
    elif fault in {"calldata", "delegatecall", "gas-reimbursement"}:
        values = copy.deepcopy(one.exec_values)
        values[2 if fault == "calldata" else 3 if fault == "delegatecall" else 6] = b"\x01" if fault == "calldata" else 1
        one.tx["input"] = Web3().eth.contract(abi=_EXEC_ABI).encode_abi("execTransaction", args=values)
    elif fault == "suffix":
        one.tx["input"] += "00"
    elif fault == "reverted":
        one.receipt["status"] = "0x0"
    elif fault == "wrong-safe":
        one.tx["to"] = "0x" + "3" * 40
    elif fault == "wrong-chain":
        one.chain = "0x1"
    elif fault == "early":
        one.block["timestamp"] = hex(intent["prepared"] - 1)
    elif fault == "version":
        one.version = "1.3.0"
    elif fault == "decimals":
        one.decimals = 18
    elif fault == "nonce":
        one.nonce = intent["safe_nonce"]
    elif fault == "hash-view":
        one.hash = "0x" + "f" * 64
    elif fault == "missing-code":
        one.code = "0x"
    elif fault == "reorg":
        one.reorg = True
    elif fault == "anchor":
        two.anchor["hash"] = "0x" + "f" * 64
    else:
        one.anchor["number"] = "0x65"
    with pytest.raises(rpc.VideoPaymentProofError):
        await prove(intent, one, two)
    assert rpc._active == 0


async def test_transport_pending_and_cancellation_leave_no_authority(prepared, monkeypatch):
    row, account = prepared
    intent = intent_for(row, account)
    one, two = RefundRPC(intent), RefundRPC(intent)
    one.tx = None
    with pytest.raises(rpc.VideoPaymentPending):
        await prove(intent, one, two)
    one = RefundRPC(intent)
    one.fault = TimeoutError("private provider response")
    with pytest.raises(rpc.VideoPaymentUnavailable):
        await prove(intent, one, two)
    one = RefundRPC(intent)
    entered, wait = asyncio.Event(), asyncio.Event()
    async def pause(_method, _params):
        entered.set()
        await wait.wait()
    one.before_read = pause
    task = asyncio.create_task(prove(intent, one, two))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert rpc._active == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert rpc._active == 0
    monkeypatch.setattr(rpc, "_active", 2)
    with pytest.raises(rpc.VideoPaymentUnavailable):
        await prove(intent, RefundRPC(intent), two)
    assert rpc._active == 2

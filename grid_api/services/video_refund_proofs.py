# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unsigned Safe refund intent and read-only exact execution evidence.

No signer, transaction submission, HTTP route or database lives here. An intent
does not prove money moved; two matching RPC observations are not a chain proof.
"""

from __future__ import annotations

import asyncio
import copy
import math
from dataclasses import dataclass

from eth_abi import decode, encode
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from . import video_payment_proofs as rpc
from . import video_quotes as quotes

_ZERO = "0x" + "0" * 40
_SUCCESS = "0x" + keccak(text="ExecutionSuccess(bytes32,uint256)").hex()
_FAILURE = "0x" + keccak(text="ExecutionFailure(bytes32,uint256)").hex()
_EXEC_TYPES = ["address", "uint256", "bytes", "uint8", "uint256", "uint256", "uint256", "address", "address", "bytes"]
_EXEC_SELECTOR = keccak(text="execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)")[:4]
_HASH_TYPES = _EXEC_TYPES[:-1] + ["uint256"]
_HASH_SELECTOR = keccak(text="getTransactionHash(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,uint256)")[:4]
_MESSAGE_TYPES = list(zip(["to", "value", "data", "operation", "safeTxGas", "baseGas", "gasPrice", "gasToken", "refundReceiver", "nonce"],
                         _HASH_TYPES, strict=True))


def transaction(intent: dict) -> dict:
    """Human-reviewable unsigned single-token transfer; no gas reimbursement."""
    return {"to": intent["asset"], "value": 0,
        "data": "0x" + (keccak(text="transfer(address,uint256)")[:4] + encode(["address", "uint256"],
            [intent["payer"], intent["amount_micro"]])).hex(),
        "operation": 0, "safeTxGas": 0, "baseGas": 0, "gasPrice": 0,
        "gasToken": _ZERO, "refundReceiver": _ZERO, "nonce": intent["safe_nonce"]}


def transaction_hash(intent: dict) -> str:
    typed = {"types": {"EIP712Domain": [{"name": "chainId", "type": "uint256"},
                                       {"name": "verifyingContract", "type": "address"}],
                      "SafeTx": [{"name": name, "type": kind} for name, kind in _MESSAGE_TYPES]},
        "primaryType": "SafeTx", "domain": {"chainId": intent["chain_id"], "verifyingContract": intent["safe_address"]},
        "message": transaction(intent)}
    message = encode_typed_data(full_message=typed)
    return "0x" + keccak(b"\x19" + message.version + message.header + message.body).hex()


def build_intent(quote: dict, attempt: dict, payment: dict, *, safe_nonce: int, created) -> dict:
    snapshot = quotes._validate_row(quote)
    domain = quotes.payment_domain(snapshot["payment"]["network"])
    if (snapshot["payment"].get("eip712_domain") != domain or payment["refund_due_at"] is None
            or type(safe_nonce) is not int or not 0 <= safe_nonce < 2**63):
        raise rpc.VideoPaymentProofError("owned refund and bounded reviewed Safe nonce required")
    intent = {"schema": "aipg.video-refund-intent.v1", "quote_id": str(quote["id"]),
        "quote_commitment": quote["commitment"], "original_proof_hash": attempt["proof_hash"],
        "original_claim_key": payment["receipt_claim_key"], "original_transaction_hash": attempt["transaction_hash"],
        "chain_id": domain["chainId"], "asset": domain["verifyingContract"],
        "safe_address": snapshot["payment"]["pay_to"], "safe_version": "1.4.1",
        "payer": attempt["payer"], "amount_micro": payment["amount_micro"], "safe_nonce": safe_nonce,
        "prepared": math.ceil(quotes._aware(created).timestamp()),
        "refund_due": math.ceil(quotes._aware(payment["refund_due_at"]).timestamp()),
        "refund_reason": payment["refund_reason"]}
    intent["safe_transaction_hash"] = transaction_hash(intent)
    return intent


def validate_intent(intent: dict) -> None:
    if (type(intent) is not dict or set(intent) != {"schema", "quote_id", "quote_commitment", "original_proof_hash",
            "original_claim_key", "original_transaction_hash", "chain_id", "asset", "safe_address", "safe_version",
            "payer", "amount_micro", "safe_nonce", "prepared", "refund_due", "refund_reason", "safe_transaction_hash"}
            or intent["schema"] != "aipg.video-refund-intent.v1" or intent["safe_version"] != "1.4.1"
            or type(intent["safe_nonce"]) is not int or not 0 <= intent["safe_nonce"] < 2**63
            or type(intent["amount_micro"]) is not int or not 0 < intent["amount_micro"] < 2**63
            or type(intent["prepared"]) is not int or type(intent["refund_due"]) is not int
            or not 0 < intent["refund_due"] <= intent["prepared"]
            or intent["refund_reason"] not in {"quote_expired", "admission_closed", "capacity_exceeded", "execution_failed"}
            or type(intent["chain_id"]) is not int
            or intent["asset"] != quotes.payment_domain(f"eip155:{intent['chain_id']}")["verifyingContract"]
            or intent["safe_address"] != quotes._address(intent["safe_address"])
            or intent["payer"] != quotes._address(intent["payer"]) or intent["payer"] == intent["safe_address"]
            or str(quotes._uuid(intent["quote_id"])) != intent["quote_id"]
            or intent["safe_transaction_hash"] != transaction_hash(intent)):
        raise rpc.VideoPaymentProofError("stored Safe refund intent changed")
    for key in ("quote_commitment", "original_proof_hash", "original_claim_key"):
        quotes._digest(intent[key])
    rpc._hex(intent["original_transaction_hash"], 32)


@dataclass(frozen=True)
class VerifiedSafeRefund:
    quote_id: str
    intent_hash: str
    chain_id: int
    asset: str
    safe_address: str
    payer: str
    amount_micro: int
    safe_nonce: int
    safe_transaction_hash: str
    transaction_hash: str
    transaction_index: int
    block_number: int
    block_hash: str
    block_timestamp: int
    transfer_log_index: int
    execution_log_index: int
    confirmation_tag: str
    confirmation_block_number: int
    confirmation_block_hash: str


async def _observe(source, intent, tx_hash, tag, depth):
    if rpc._number(await rpc._read(source, "eth_chainId", [])) != intent["chain_id"]:
        raise rpc.VideoPaymentProofError("refund RPC chain mismatch")
    tx = await rpc._read(source, "eth_getTransactionByHash", [tx_hash])
    receipt = await rpc._read(source, "eth_getTransactionReceipt", [tx_hash])
    if tx is None or receipt is None:
        raise rpc.VideoPaymentPending("refund transaction not yet available")
    tx, receipt = rpc._mapping(tx), rpc._mapping(receipt)
    if tx.get("blockNumber") is None or receipt.get("blockNumber") is None:
        raise rpc.VideoPaymentPending("refund transaction not yet mined")
    number, block_hash = rpc._number(receipt["blockNumber"]), rpc._hex(receipt["blockHash"], 32)
    index = rpc._number(receipt["transactionIndex"])
    block = rpc._mapping(await rpc._read(source, "eth_getBlockByNumber", [hex(number), False]))
    timestamp = rpc._number(block["timestamp"])
    if (rpc._hex(tx["hash"], 32) != tx_hash or rpc._hex(receipt["transactionHash"], 32) != tx_hash
            or rpc._number(tx["chainId"]) != intent["chain_id"] or rpc._number(tx["value"]) != 0
            or rpc._hex(tx["to"], 20) != intent["safe_address"] or rpc._hex(receipt["to"], 20) != intent["safe_address"]
            or rpc._hex(tx["from"], 20) != rpc._hex(receipt["from"], 20) or rpc._number(receipt["status"]) != 1
            or rpc._number(tx["blockNumber"]) != number or rpc._hex(tx["blockHash"], 32) != block_hash
            or rpc._number(tx["transactionIndex"]) != index or rpc._number(block["number"]) != number
            or rpc._hex(block["hash"], 32) != block_hash or timestamp < intent["prepared"]):
        raise rpc.VideoPaymentProofError("refund transaction binding mismatch")
    encoded = bytes.fromhex(rpc._hex(tx["input"], maximum=32 * 1024)[2:])
    if encoded[:4] != _EXEC_SELECTOR:
        raise rpc.VideoPaymentProofError("direct Safe refund execution required")
    values = decode(_EXEC_TYPES, encoded[4:], strict=True)
    expected = transaction(intent)
    if (encode(_EXEC_TYPES, values) != encoded[4:] or not 65 <= len(values[-1]) <= 8192
            or values[:-1] != (expected["to"], 0, bytes.fromhex(expected["data"][2:]), 0, 0, 0, 0, _ZERO, _ZERO)):
        raise rpc.VideoPaymentProofError("refund calldata changed")
    logs = receipt["logs"]
    if type(logs) is not list or not 1 <= len(logs) <= 128:
        raise rpc.VideoPaymentProofError("refund log bound exceeded")
    transfer, success, seen = [], [], set()
    for raw in logs:
        log = rpc._mapping(raw)
        offset = rpc._number(log["logIndex"])
        if (offset in seen or log.get("removed", False) is not False
                or rpc._hex(log["transactionHash"], 32) != tx_hash or rpc._number(log["transactionIndex"]) != index
                or rpc._number(log["blockNumber"]) != number or rpc._hex(log["blockHash"], 32) != block_hash):
            raise rpc.VideoPaymentProofError("refund log anchor changed")
        seen.add(offset)
        address = rpc._hex(log["address"], 20)
        topics = log["topics"]
        if type(topics) is not list or not 1 <= len(topics) <= 4:
            raise rpc.VideoPaymentProofError("invalid refund topics")
        topics = [rpc._hex(value, 32) for value in topics]
        data = rpc._hex(log["data"], maximum=16 * 1024)
        if address == intent["asset"] and topics[0] == rpc._TRANSFER:
            if (len(topics) != 3 or rpc._topic_address(topics[1]) != intent["safe_address"]
                    or rpc._topic_address(topics[2]) != intent["payer"] or len(data) != 66
                    or int(data, 16) != intent["amount_micro"]):
                raise rpc.VideoPaymentProofError("refund token transfer mismatch")
            transfer.append(offset)
        if address == intent["safe_address"] and topics[0] in {_SUCCESS, _FAILURE}:
            if (topics != [_SUCCESS] or data != "0x" + encode(["bytes32", "uint256"],
                    [bytes.fromhex(intent["safe_transaction_hash"][2:]), 0]).hex()):
                raise rpc.VideoPaymentProofError("Safe refund execution mismatch")
            success.append(offset)
    if len(transfer) != 1 or len(success) != 1 or transfer[0] >= success[0]:
        raise rpc.VideoPaymentProofError("one exact refund transfer and Safe execution required")
    anchor = rpc._mapping(await rpc._read(source, "eth_getBlockByNumber", [tag, False]))
    anchor = {"number": rpc._number(anchor["number"]), "hash": rpc._hex(anchor["hash"], 32)}
    if anchor["number"] - number + 1 < depth:
        raise rpc.VideoPaymentPending("refund confirmation policy not satisfied")
    selector = {"blockHash": block_hash, "requireCanonical": True}
    for address in (intent["safe_address"], intent["asset"]):
        if rpc._hex(await rpc._read(source, "eth_getCode", [address, selector])) == "0x":
            raise rpc.VideoPaymentProofError("refund contract code unavailable")

    async def call(address, signature, args=b""):
        return rpc._hex(await rpc._read(source, "eth_call", [{"to": address,
            "data": "0x" + (keccak(text=signature)[:4] + args).hex()}, selector]))

    if (await call(intent["safe_address"], "VERSION()") != "0x" + encode(["string"], [intent["safe_version"]]).hex()
            or await call(intent["asset"], "decimals()") != "0x" + encode(["uint256"], [6]).hex()
            or int(rpc._hex(await call(intent["safe_address"], "nonce()"), 32), 16) <= intent["safe_nonce"]):
        raise rpc.VideoPaymentProofError("refund contract state mismatch")
    args = encode(_HASH_TYPES, (*values[:-1], intent["safe_nonce"]))
    hash_call = "0x" + (_HASH_SELECTOR + args).hex()
    observed_hash = await rpc._read(source, "eth_call", [{"to": intent["safe_address"], "data": hash_call}, selector])
    if rpc._hex(observed_hash, 32) != intent["safe_transaction_hash"]:
        raise rpc.VideoPaymentProofError("Safe refund hash mismatch")
    return anchor, {"quote_id": intent["quote_id"], "intent_hash": quotes._hash(intent), "chain_id": intent["chain_id"],
        "asset": intent["asset"], "safe_address": intent["safe_address"], "payer": intent["payer"],
        "amount_micro": intent["amount_micro"], "safe_nonce": intent["safe_nonce"],
        "safe_transaction_hash": intent["safe_transaction_hash"],
        "transaction_hash": tx_hash, "transaction_index": index, "block_number": number, "block_hash": block_hash,
        "block_timestamp": timestamp, "transfer_log_index": transfer[0], "execution_log_index": success[0]}


async def prove_refund(intent: dict, *, transaction_hash: str, sources: tuple[rpc.PaymentRPC, rpc.PaymentRPC],
                       confirmation_tag: str, min_confirmations: int) -> VerifiedSafeRefund:
    """Bounded dual-source evidence; shared slots and no SQL or signing."""
    if rpc._active >= 2:
        raise rpc.VideoPaymentUnavailable("video payment proof capacity exhausted")
    rpc._active += 1
    try:
        async with asyncio.timeout(rpc._PROOF_SECONDS):
            if (confirmation_tag not in {"safe", "finalized"} or type(min_confirmations) is not int
                    or not 1 <= min_confirmations <= 256):
                raise rpc.VideoPaymentProofError("reviewed refund confirmation policy required")
            rpc._require_sources(sources)
            intent = copy.deepcopy(intent)
            validate_intent(intent)
            tx_hash = rpc._hex(transaction_hash, 32)
            if tx_hash == intent["original_transaction_hash"]:
                raise rpc.VideoPaymentProofError("received payment is not its refund")
            first_anchor, first = await _observe(sources[0], intent, tx_hash, confirmation_tag, min_confirmations)
            second_anchor, second = await _observe(sources[1], intent, tx_hash, confirmation_tag, min_confirmations)
            if first != second:
                raise rpc.VideoPaymentProofError("refund RPC evidence disagrees")
            anchor = min((first_anchor, second_anchor), key=lambda value: value["number"])
            for source in sources:
                for number, expected in ((first["block_number"], first["block_hash"]), (anchor["number"], anchor["hash"])):
                    block = rpc._mapping(await rpc._read(source, "eth_getBlockByNumber", [hex(number), False]))
                    if rpc._number(block["number"]) != number or rpc._hex(block["hash"], 32) != expected:
                        raise rpc.VideoPaymentProofError("refund block anchor changed")
            return VerifiedSafeRefund(**first, confirmation_tag=confirmation_tag,
                confirmation_block_number=anchor["number"], confirmation_block_hash=anchor["hash"])
    except (TimeoutError, asyncio.IncompleteReadError):
        raise rpc.VideoPaymentUnavailable("video refund proof deadline exceeded") from None
    except rpc.VideoPaymentProofError:
        raise
    except Exception:
        raise rpc.VideoPaymentProofError("video refund evidence could not be proved") from None
    finally:
        rpc._active -= 1

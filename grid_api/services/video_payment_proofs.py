# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only historical EIP-3009 payment evidence, without admission authority.

Only direct canonical-USDC v/r/s transfers are supported. Two operator-selected
RPC hosts must agree; a receipt or facilitator assertion alone is insufficient.
There is no HTTP route, sender, replay claim, reservation or refund here. Neither
an error nor quote expiry proves absence of payment or extinguishes a liability.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from eth_abi import decode, encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from . import video_eip3009 as bindings
from . import video_quotes as quotes

_TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
_USED = "0x" + keccak(text="AuthorizationUsed(address,bytes32)").hex()
_SELECTOR = keccak(text="transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)")[:4]
_ABI = ["address", "address", "uint256", "uint256", "uint256", "bytes32", "uint8", "bytes32", "bytes32"]
_QUANTITY = re.compile(r"^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$")
_RPC_SECONDS = 5
_PROOF_SECONDS = 30
_MAX_REPLY_BYTES = 512 * 1024
_active = 0


class VideoPaymentProofError(ValueError):
    """Unproven or inconsistent evidence; never a permission to resend/refund."""


class VideoPaymentPending(VideoPaymentProofError):
    """Transaction missing or not covered by the selected confirmation policy."""


class VideoPaymentUnavailable(VideoPaymentProofError):
    """RPC, capacity or transport uncertainty; retain the original attempt."""


@dataclass(frozen=True)
class PaymentRPC:
    """Background-only operator source, never selected by a payment request.

    Requests use this exact HTTPS destination through the caller-owned client.
    The future background adapter must manage its authenticated, TLS-verifying
    client lifecycle. Distinct hosts are defense in depth, not independence proof.
    """

    origin: str = field(repr=False)
    client: httpx.AsyncClient = field(repr=False)


@dataclass(frozen=True)
class VerifiedVideoTransfer:
    """Private receipt evidence, not a funded job or cross-rail ownership claim."""

    quote_id: str
    job_id: str
    chain_id: int
    asset: str
    payer: str
    recipient: str
    amount_micro: int
    authorization_nonce: str
    transaction_hash: str
    transaction_index: int
    block_number: int
    block_hash: str
    block_timestamp: int
    transfer_log_index: int
    authorization_log_index: int
    confirmation_tag: str
    confirmation_block_number: int
    confirmation_block_hash: str


def _hex(value, size: int | None = None, *, maximum: int = 512 * 1024) -> str:
    if (type(value) is not str or not value.startswith("0x") or len(value) > 2 + maximum * 2
            or len(value) % 2 or not re.fullmatch(r"0x[0-9a-fA-F]*", value)
            or (size is not None and len(value) != 2 + size * 2)):
        raise VideoPaymentProofError("invalid bounded hexadecimal evidence")
    return value.lower()


def _number(value) -> int:
    if type(value) is not str or len(value) > 18 or not _QUANTITY.fullmatch(value):
        raise VideoPaymentProofError("invalid bounded RPC quantity")
    return int(value, 16)


def _mapping(value) -> dict:
    if type(value) is not dict:
        raise VideoPaymentProofError("missing structured payment evidence")
    return value


def _topic_address(value) -> str:
    value = _hex(value, 32)
    if value[2:26] != "0" * 24:
        raise VideoPaymentProofError("noncanonical address topic")
    return quotes._address("0x" + value[26:])


def _expectation(row: dict, payer: str) -> tuple[dict, dict]:
    snapshot = quotes._validate_row(row)
    if snapshot["schema"] != "aipg.video-quote-binding.v2":
        raise VideoPaymentProofError("quote has no frozen payment domain")
    authorization = bindings._authorization(snapshot, payer)
    # Deliberately no current-window check: this is historical reconciliation.
    return snapshot, bindings._typed_data(snapshot, authorization)


def _verify_transaction(tx: dict, receipt: dict, block: dict, typed: dict, snapshot: dict, tx_hash: str) -> tuple[int, str, int, int]:
    message, domain = typed["message"], typed["domain"]
    number, block_hash = _number(receipt["blockNumber"]), _hex(receipt["blockHash"], 32)
    index = _number(receipt["transactionIndex"])
    timestamp = _number(block["timestamp"])
    if (_hex(tx["hash"], 32) != tx_hash or _hex(receipt["transactionHash"], 32) != tx_hash
            or _number(tx["chainId"]) != domain["chainId"]
            or _number(tx["blockNumber"]) != number or _hex(tx["blockHash"], 32) != block_hash
            or _number(tx["transactionIndex"]) != index
            or _number(block["number"]) != number or _hex(block["hash"], 32) != block_hash
            or _number(receipt["status"]) != 1 or _number(tx["value"]) != 0
            or _hex(tx["to"], 20) != domain["verifyingContract"]
            or _hex(receipt["to"], 20) != domain["verifyingContract"]
            or _hex(tx["from"], 20) != _hex(receipt["from"], 20)
            or not snapshot["created"] <= timestamp < int(message["validBefore"])):
        raise VideoPaymentProofError("transaction does not bind the quoted payment")
    calldata = bytes.fromhex(_hex(tx["input"], 292)[2:])
    if calldata[:4] != _SELECTOR:
        raise VideoPaymentProofError("unsupported payment transaction shape")
    values = decode(_ABI, calldata[4:], strict=True)
    if (encode(_ABI, values) != calldata[4:]
            or values[:6] != (message["from"], message["to"], int(message["value"]),
                               int(message["validAfter"]), int(message["validBefore"]),
                               bytes.fromhex(message["nonce"][2:]))):
        raise VideoPaymentProofError("transaction authorization mismatch")
    signature = bindings._signature("0x" + (values[7] + values[8] + bytes([values[6]])).hex())
    signer = Account.recover_message(encode_typed_data(full_message=typed), signature=signature)
    if signer.lower() != message["from"]:
        raise VideoPaymentProofError("transaction signature mismatch")
    return number, block_hash, timestamp, index


def _verify_logs(receipt: dict, typed: dict, tx_hash: str, number: int, block_hash: str, index: int) -> tuple[int, int]:
    logs = receipt["logs"]
    if type(logs) is not list or not 1 <= len(logs) <= 128:
        raise VideoPaymentProofError("receipt log bound exceeded")
    transfer, used, seen = [], [], set()
    message, asset = typed["message"], typed["domain"]["verifyingContract"]
    for raw in logs:
        log = _mapping(raw)
        log_index = _number(log["logIndex"])
        if (log_index in seen or log.get("removed", False) is not False
                or _hex(log["transactionHash"], 32) != tx_hash
                or _number(log["transactionIndex"]) != index
                or _number(log["blockNumber"]) != number or _hex(log["blockHash"], 32) != block_hash):
            raise VideoPaymentProofError("receipt log anchor mismatch")
        seen.add(log_index)
        topics = log["topics"]
        if type(topics) is not list or not 1 <= len(topics) <= 4:
            raise VideoPaymentProofError("invalid receipt topics")
        topics = [_hex(topic, 32) for topic in topics]
        data = _hex(log["data"], maximum=16 * 1024)
        if _hex(log["address"], 20) != asset:
            continue
        if topics[0] == _TRANSFER:
            if (len(topics) != 3 or _topic_address(topics[1]) != message["from"]
                    or _topic_address(topics[2]) != message["to"]
                    or len(data) != 66 or int(data, 16) != int(message["value"])):
                raise VideoPaymentProofError("canonical-USDC transfer mismatch")
            transfer.append(log_index)
        if topics[0] == _USED:
            if (len(topics) != 3 or _topic_address(topics[1]) != message["from"]
                    or topics[2] != message["nonce"] or data != "0x"):
                raise VideoPaymentProofError("authorization event mismatch")
            used.append(log_index)
    if len(transfer) != 1 or len(used) != 1:
        raise VideoPaymentProofError("one exact transfer and authorization event required")
    return transfer[0], used[0]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise VideoPaymentUnavailable("ambiguous payment RPC response")
        result[key] = value
    return result


def _reject_constant(_value: str):
    raise VideoPaymentUnavailable("nonfinite payment RPC response")


def _finite_real(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise VideoPaymentUnavailable("nonfinite payment RPC response")
    return number


async def _rpc_response(source: PaymentRPC, method: str, params: list):
    if method not in {"eth_chainId", "eth_getTransactionByHash", "eth_getTransactionReceipt",
                      "eth_getBlockByNumber", "eth_getCode", "eth_call", "eth_getLogs"}:
        raise VideoPaymentUnavailable("unsupported payment RPC read")
    async with source.client.stream("POST", source.origin, follow_redirects=False, timeout=_RPC_SECONDS,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as response:
        if response.status_code != 200:
            raise VideoPaymentUnavailable("payment RPC unavailable")
        declared = response.headers.get("content-length")
        if declared is not None and (not declared.isdecimal() or int(declared) > _MAX_REPLY_BYTES):
            raise VideoPaymentUnavailable("payment RPC response bound exceeded")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_REPLY_BYTES:
                raise VideoPaymentUnavailable("payment RPC response bound exceeded")
            body.extend(chunk)
        reply = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_reject_constant, parse_float=_finite_real)
        if (type(reply) is not dict or set(reply) != {"jsonrpc", "id", "result"}
                or reply["jsonrpc"] != "2.0" or type(reply["id"]) is not int or reply["id"] != 1):
            raise VideoPaymentUnavailable("payment RPC response unavailable")
        return reply["result"]


async def _read(source: PaymentRPC, method: str, params: list):
    try:
        return await asyncio.wait_for(_rpc_response(source, method, params), timeout=_RPC_SECONDS)
    except Exception:
        raise VideoPaymentUnavailable("payment RPC unavailable") from None


async def _observe(source: PaymentRPC, snapshot: dict, typed: dict, tx_hash: str, tag: str, depth: int) -> tuple[dict, dict]:
    domain = typed["domain"]
    if _number(await _read(source, "eth_chainId", [])) != domain["chainId"]:
        raise VideoPaymentProofError("payment RPC chain mismatch")
    tx = await _read(source, "eth_getTransactionByHash", [tx_hash])
    receipt = await _read(source, "eth_getTransactionReceipt", [tx_hash])
    if tx is None or receipt is None:
        raise VideoPaymentPending("payment transaction not yet available")
    tx, receipt = _mapping(tx), _mapping(receipt)
    if receipt.get("blockNumber") is None or tx.get("blockNumber") is None:
        raise VideoPaymentPending("payment transaction not yet mined")
    block = _mapping(await _read(source, "eth_getBlockByNumber", [hex(_number(receipt["blockNumber"])), False]))
    number, block_hash, timestamp, index = _verify_transaction(tx, receipt, block, typed, snapshot, tx_hash)
    transfer, used = _verify_logs(receipt, typed, tx_hash, number, block_hash, index)
    anchor = _mapping(await _read(source, "eth_getBlockByNumber", [tag, False]))
    anchor_number, anchor_hash = _number(anchor["number"]), _hex(anchor["hash"], 32)
    if anchor_number - number + 1 < depth:
        raise VideoPaymentPending("payment confirmation policy not yet satisfied")
    selector = {"blockHash": block_hash, "requireCanonical": True}
    token = domain["verifyingContract"]
    if _hex(await _read(source, "eth_getCode", [token, selector])) == "0x":
        raise VideoPaymentProofError("canonical payment token has no code")
    if _hex(await _read(source, "eth_getCode", [typed["message"]["from"], selector])) != "0x":
        raise VideoPaymentProofError("payer is not an undelegated EOA at the payment block")
    calls = [
        ("DOMAIN_SEPARATOR()", b"", "0x" + encode_typed_data(full_message=typed).header.hex()),
        ("decimals()", b"", "0x" + (6).to_bytes(32, "big").hex()),
        ("authorizationState(address,bytes32)", encode(["address", "bytes32"],
            [typed["message"]["from"], bytes.fromhex(typed["message"]["nonce"][2:])]),
         "0x" + (1).to_bytes(32, "big").hex()),
    ]
    for signature, args, expected in calls:
        data = "0x" + (keccak(text=signature)[:4] + args).hex()
        if _hex(await _read(source, "eth_call", [{"to": token, "data": data}, selector]), 32) != expected:
            raise VideoPaymentProofError("payment contract state mismatch")
    return {"number": anchor_number, "hash": anchor_hash}, {
        "quote_id": snapshot["quote_id"], "job_id": snapshot["job_id"], "chain_id": domain["chainId"],
        "asset": token, "payer": typed["message"]["from"], "recipient": typed["message"]["to"],
        "amount_micro": int(typed["message"]["value"]), "authorization_nonce": typed["message"]["nonce"],
        "transaction_hash": tx_hash, "transaction_index": index, "block_number": number,
        "block_hash": block_hash, "block_timestamp": timestamp, "transfer_log_index": transfer,
        "authorization_log_index": used,
    }


def _require_sources(sources: tuple[PaymentRPC, PaymentRPC]) -> None:
    if (type(sources) is not tuple or len(sources) != 2
            or any(not isinstance(source, PaymentRPC) for source in sources)):
        raise VideoPaymentProofError("two operator RPC sources required")
    hosts = []
    for source in sources:
        origin = urlsplit(source.origin)
        if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                or origin.query or origin.fragment or not isinstance(source.client, httpx.AsyncClient)):
            raise VideoPaymentProofError("operator HTTPS RPC sources required")
        hosts.append(httpx.URL(source.origin).raw_host.decode("ascii").rstrip(".").lower())
    if len(set(hosts)) != 2:
        raise VideoPaymentProofError("distinct operator RPC hosts required")


async def _locate(source: PaymentRPC, typed: dict, start: int, end: int, tag: str) -> tuple[dict, dict | None]:
    domain, message = typed["domain"], typed["message"]
    if _number(await _read(source, "eth_chainId", [])) != domain["chainId"]:
        raise VideoPaymentProofError("payment RPC chain mismatch")
    confirmed = _mapping(await _read(source, "eth_getBlockByNumber", [tag, False]))
    if _number(confirmed["number"]) < end:
        raise VideoPaymentPending("payment search range is not confirmed")
    boundary = _mapping(await _read(source, "eth_getBlockByNumber", [hex(end), False]))
    anchor = {"number": _number(boundary["number"]), "hash": _hex(boundary["hash"], 32)}
    if anchor["number"] != end:
        raise VideoPaymentProofError("payment search anchor mismatch")
    topics = [_USED, "0x" + "0" * 24 + message["from"][2:], message["nonce"]]
    logs = await _read(source, "eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
        "address": domain["verifyingContract"], "topics": topics}])
    if type(logs) is not list or len(logs) > 1:
        raise VideoPaymentProofError("ambiguous payment authorization logs")
    found = None
    if logs:
        log = _mapping(logs[0])
        number = _number(log["blockNumber"])
        if (log.get("removed", False) is not False or not start <= number <= end
                or _hex(log["address"], 20) != domain["verifyingContract"] or log["data"] != "0x"
                or type(log["topics"]) is not list or len(log["topics"]) != 3
                or [_hex(topic, 32) for topic in log["topics"]] != topics):
            raise VideoPaymentProofError("payment authorization log mismatch")
        found = {"transaction_hash": _hex(log["transactionHash"], 32), "block_number": number,
            "block_hash": _hex(log["blockHash"], 32), "transaction_index": _number(log["transactionIndex"]),
            "log_index": _number(log["logIndex"])}
        block = _mapping(await _read(source, "eth_getBlockByNumber", [hex(number), False]))
        if _number(block["number"]) != number or _hex(block["hash"], 32) != found["block_hash"]:
            raise VideoPaymentProofError("payment authorization block mismatch")
    return anchor, found


async def locate_authorization(row: dict, *, payer: str, sources: tuple[PaymentRPC, PaymentRPC],
                               from_block: int, to_block: int, confirmation_tag: Literal["safe", "finalized"]) -> str:
    """Bounded original-transaction correlation, never payment or absence proof.

    Only an operator-selected confirmed range of at most 1024 blocks is read.
    A miss, disagreement or transport error cannot authorize another send. The
    caller must still prove the entire original transaction before funding.
    """
    global _active
    if _active >= 2:
        raise VideoPaymentUnavailable("video payment proof capacity exhausted")
    _active += 1
    try:
        async with asyncio.timeout(_PROOF_SECONDS):
            _require_sources(sources)
            if (confirmation_tag not in {"safe", "finalized"} or type(from_block) is not int
                    or type(to_block) is not int or not 0 <= from_block <= to_block < 2**64
                    or to_block - from_block >= 1024):
                raise VideoPaymentProofError("bounded reviewed payment search required")
            _snapshot, typed = _expectation(copy.deepcopy(row), payer)
            first_anchor, first = await _locate(sources[0], typed, from_block, to_block, confirmation_tag)
            second_anchor, second = await _locate(sources[1], typed, from_block, to_block, confirmation_tag)
            if first_anchor != second_anchor or first != second:
                raise VideoPaymentProofError("payment search evidence disagrees")
            for source in sources:
                selected = [(to_block, first_anchor["hash"])]
                if first is not None:
                    selected.append((first["block_number"], first["block_hash"]))
                for number, expected in selected:
                    current = _mapping(await _read(source, "eth_getBlockByNumber", [hex(number), False]))
                    if _number(current["number"]) != number or _hex(current["hash"], 32) != expected:
                        raise VideoPaymentProofError("payment search anchor changed")
            if first is None:
                raise VideoPaymentPending("original payment transaction not located; retain attempt")
            return first["transaction_hash"]
    except (TimeoutError, asyncio.IncompleteReadError):
        raise VideoPaymentUnavailable("video payment search deadline exceeded") from None
    except VideoPaymentProofError:
        raise
    except Exception:
        raise VideoPaymentProofError("video payment transaction could not be located") from None
    finally:
        _active -= 1


async def prove_transfer(row: dict, *, payer: str, transaction_hash: str, sources: tuple[PaymentRPC, PaymentRPC],
                         confirmation_tag: Literal["safe", "finalized"], min_confirmations: int) -> VerifiedVideoTransfer:
    """Reconcile one original transaction, even after quote/config expiry.

    Caller supplies an owner-loaded immutable quote and reviewed RPC/policy.
    The returned evidence still needs a durable prior attempt, shared cross-rail
    claims and atomic funded admission/refund handling. No imported runtime
    path calls this function. Safe and finalized are explicitly different bases;
    there is no implicit latest-block fallback or provider-selected policy.
    """
    global _active
    if _active >= 2:
        raise VideoPaymentUnavailable("video payment proof capacity exhausted")
    _active += 1
    try:
        async with asyncio.timeout(_PROOF_SECONDS):
            if (confirmation_tag not in {"safe", "finalized"} or type(min_confirmations) is not int
                    or not 1 <= min_confirmations <= 256):
                raise VideoPaymentProofError("reviewed confirmation policy and two RPC sources required")
            _require_sources(sources)
            # A caller cannot change its snapshot while the RPC reads yield.
            snapshot, typed = _expectation(copy.deepcopy(row), payer)
            tx_hash = _hex(transaction_hash, 32)
            first_anchor, first = await _observe(sources[0], snapshot, typed, tx_hash, confirmation_tag, min_confirmations)
            second_anchor, second = await _observe(sources[1], snapshot, typed, tx_hash, confirmation_tag, min_confirmations)
            if first != second:
                raise VideoPaymentProofError("payment RPC evidence disagrees")
            anchor = min((first_anchor, second_anchor), key=lambda item: item["number"])
            for source in sources:
                for number, expected in ((first["block_number"], first["block_hash"]), (anchor["number"], anchor["hash"])):
                    current = _mapping(await _read(source, "eth_getBlockByNumber", [hex(number), False]))
                    if _number(current["number"]) != number or _hex(current["hash"], 32) != expected:
                        raise VideoPaymentProofError("payment block anchor changed")
            return VerifiedVideoTransfer(**first, confirmation_tag=confirmation_tag,
                confirmation_block_number=anchor["number"], confirmation_block_hash=anchor["hash"])
    except (TimeoutError, asyncio.IncompleteReadError):
        raise VideoPaymentUnavailable("video payment proof deadline exceeded") from None
    except VideoPaymentProofError:
        raise
    except Exception:
        raise VideoPaymentProofError("video payment evidence could not be proved") from None
    finally:
        _active -= 1

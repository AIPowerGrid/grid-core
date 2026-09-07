# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Explicit, default-off validator payments, isolated from worker den payouts.

One approved allocation binds one nonce and one signed transaction. Retries
broadcast identical bytes; no automatic replacement, new nonce or new recipient.
"""

from __future__ import annotations

import re
from datetime import timedelta

import sqlalchemy as sa
from eth_abi import encode
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound

from ...config import get_settings
from ...database import new_session
from ...v2 import schema as tables
from .. import validator_compensation as comp
from .. import validator_compensation_recipients as consent
from . import payouts, sanctions

payments = tables.validator_compensation_payments
SCHEMA = "aipg.validator.compensation.payment.v1"
GAS_LIMIT = 120_000
MAX_FEE = 2_000_000_000
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)")
REQUEST_FIELDS = {
    "campaign_id",
    "operator_group_id",
    "sender",
    "max_fee_per_gas",
    "max_priority_fee_per_gas",
    "approved_until",
    "approval_ref",
}


def _request(value):
    if not isinstance(value, dict) or set(value) != REQUEST_FIELDS:
        raise comp.CompensationError("invalid payment request")
    result = dict(value)
    consent._address(result["sender"])
    for key in ("max_fee_per_gas", "max_priority_fee_per_gas"):
        if not isinstance(result[key], str) or not re.fullmatch(r"[1-9][0-9]{0,9}", result[key]) or int(result[key]) > MAX_FEE:
            raise comp.CompensationError("explicit positive gas cap at most 2 gwei required")
    if int(result["max_priority_fee_per_gas"]) > int(result["max_fee_per_gas"]):
        raise comp.CompensationError("priority fee exceeds total fee cap")
    if not isinstance(result["approval_ref"], str) or not re.fullmatch(r"approval:[A-Za-z0-9_-]{3,96}", result["approval_ref"]):
        raise comp.CompensationError("explicit transfer approval reference required")
    result["approved_until"] = comp._time(result["approved_until"]).isoformat()
    return result


def _data(plan):
    return bytes.fromhex("a9059cbb") + encode(["address", "uint256"], [plan["recipient"], int(plan["amount_atomic"])])


def _transaction(plan, nonce):
    return {
        "type": 2,
        "chainId": 8453,
        "nonce": nonce,
        "to": Web3.to_checksum_address(plan["token_address"]),
        "value": 0,
        "data": _data(plan),
        "gas": GAS_LIMIT,
        "maxFeePerGas": int(plan["max_fee_per_gas"]),
        "maxPriorityFeePerGas": int(plan["max_priority_fee_per_gas"]),
    }


def _check_record(row):
    plan = row["plan"]
    _request({key: plan[key] for key in REQUEST_FIELDS})
    if plan["schema"] != SCHEMA or plan["gas_limit"] != GAS_LIMIT:
        raise comp.CompensationError("payment policy changed")
    if comp._hash(plan) != row["plan_hash"] or row["allocation_hash"] != plan["allocation_hash"]:
        raise comp.CompensationError("payment commitment changed")
    if row["sender"] != plan["sender"] or row["chain_id"] != plan["chain_id"] or row["chain_id"] != 8453:
        raise comp.CompensationError("payment sender or chain changed")
    raw = HexBytes(row["raw_transaction"])
    try:
        recovered = Account.recover_transaction(raw).lower()
        decoded = TypedTransaction.from_bytes(raw).as_dict()
        expected = _transaction(plan, row["nonce"])
        matches = not decoded.get("accessList") and all(
            HexBytes(decoded[key]) == HexBytes(value) if key in {"to", "data"} else decoded[key] == value for key, value in expected.items()
        )
    except Exception:
        matches, recovered = False, ""
    if not matches or recovered != row["sender"] or Web3.to_hex(Web3.keccak(raw)) != row["tx_hash"]:
        raise comp.CompensationError("signed transaction does not match approved payment")


async def _existing(session, request):
    allocation_hash = await session.scalar(
        sa.select(tables.validator_compensation_allocations.c.allocation_hash).where(
            tables.validator_compensation_allocations.c.campaign_id == request["campaign_id"],
            tables.validator_compensation_allocations.c.operator_group_id == request["operator_group_id"],
        ),
    )
    row = (await session.execute(sa.select(payments).where(payments.c.allocation_hash == allocation_hash))).mappings().first()
    if row:
        _check_record(row)
        if any(row["plan"].get(key) != value for key, value in request.items()):
            raise comp.CompensationError("payment is immutable; resume the original approved request")
    return row


async def _plan(session, request, *, apply):
    snapshot = await consent._snapshot(session, request["campaign_id"], request["operator_group_id"], apply=apply)
    binding = (
        (
            await session.execute(
                sa.select(tables.validator_compensation_recipients).where(
                    tables.validator_compensation_recipients.c.allocation_hash == snapshot["allocation_hash"],
                ),
            )
        )
        .mappings()
        .first()
    )
    if not binding or comp._hash(binding["proof"]) != binding["proof_hash"]:
        raise comp.CompensationError("committed recipient consent required")
    proof = binding["proof"]["consent"]
    consent.consent_message(proof)
    if any(proof[key] != value for key, value in snapshot.items()) or proof["recipient"] != binding["recipient"]:
        raise comp.CompensationError("recipient binding changed")
    if not comp._time(proof["issued_at"]) <= comp._time(binding["created"]) < comp._time(proof["expires_at"]):
        raise comp.CompensationError("recipient was not bound during its signing window")
    if not comp._now() < comp._time(request["approved_until"]) <= comp._now() + timedelta(days=1):
        raise comp.CompensationError("new transfer approval must expire within 24 hours")
    if request["sender"] == binding["recipient"]:
        raise comp.CompensationError("treasury cannot pay itself")
    return {
        "schema": SCHEMA,
        **request,
        "allocation_hash": snapshot["allocation_hash"],
        "recipient_proof_hash": binding["proof_hash"],
        "chain_id": 8453,
        "token_address": snapshot["token_address"],
        "recipient": binding["recipient"],
        "amount_atomic": snapshot["amount_atomic"],
        "gas_limit": GAS_LIMIT,
    }


async def preview_payment(request):
    request = _request(request)
    async with await new_session() as session:
        await comp._transaction(session, False)
        row = await _existing(session, request)
        plan = row["plan"] if row else await _plan(session, request, apply=False)
        return {"plan": plan, "digest": comp._hash(plan), "status": row["status"] if row else "preview", "dry_run": True}


def _context(plan):
    if not payouts.BASE_RPC_URL or not payouts.TREASURY_PK:
        raise comp.CompensationError("payout RPC and treasury signer are not configured")
    w3 = Web3(Web3.HTTPProvider(payouts.BASE_RPC_URL, request_kwargs={"timeout": 15}))
    account = Account.from_key(payouts.TREASURY_PK)
    token = w3.eth.contract(address=Web3.to_checksum_address(plan["token_address"]), abi=payouts._ERC20_ABI)
    if (
        w3.eth.chain_id != 8453
        or account.address.lower() != plan["sender"]
        or payouts.AIPG_TOKEN_ADDRESS.lower() != plan["token_address"]
        or not w3.eth.get_code(token.address)
        or token.functions.decimals().call() != 18
    ):
        raise comp.CompensationError("configured chain, signer or token differs from approved payment")
    return w3, account


def _preflight(w3, plan):
    latest = w3.eth.get_block("latest")
    if int(latest["baseFeePerGas"]) + int(plan["max_priority_fee_per_gas"]) > int(plan["max_fee_per_gas"]):
        raise comp.CompensationError("approved gas cap is below current base fee")
    if w3.eth.get_balance(Web3.to_checksum_address(plan["sender"])) < GAS_LIMIT * int(plan["max_fee_per_gas"]):
        raise comp.CompensationError("treasury gas balance is below approved L2 maximum")
    estimate = w3.eth.estimate_gas(
        {
            "from": Web3.to_checksum_address(plan["sender"]),
            "to": Web3.to_checksum_address(plan["token_address"]),
            "data": _data(plan),
            "value": 0,
        },
    )
    if estimate > GAS_LIMIT:
        raise comp.CompensationError("transfer estimate exceeds approved gas limit")


async def _screen(plan):
    if sanctions.ORACLE_ADDRESS and not sanctions._oracle_configured():
        return "review_sanctions"
    return sanctions.payable_status(await sanctions.screen(plan["recipient"]))


async def _bind(plan, digest, w3, account):
    pending_nonce = w3.eth.get_transaction_count(account.address, "pending")
    async with await new_session() as session:
        if session.get_bind().dialect.name != "postgresql":
            raise comp.CompensationError("validator transfers require PostgreSQL")
        # Same lock as the legacy whole-run sender, but held by the transaction
        # that actually assigns the nonce. Losing this connection rolls it back.
        await session.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": payouts._PAYOUT_LOCK_KEY})
        await comp._lock(session)
        request = {key: plan[key] for key in REQUEST_FIELDS}
        row = await _existing(session, request)
        if row:
            if row["plan_hash"] != digest:
                raise comp.CompensationError("existing payment differs from approved digest")
            return dict(row)
        current = await _plan(session, request, apply=True)
        if comp._hash(current) != digest:
            raise comp.CompensationError("payment changed while waiting for its nonce")
        nonce = max(pending_nonce, await payouts._max_assigned_nonce(session) + 1)
        signed = account.sign_transaction(_transaction(plan, nonce))
        now = comp._now()
        row = {
            "allocation_hash": plan["allocation_hash"],
            "plan": plan,
            "plan_hash": digest,
            "chain_id": 8453,
            "sender": plan["sender"],
            "nonce": nonce,
            "tx_hash": Web3.to_hex(signed.hash),
            "raw_transaction": bytes(signed.raw_transaction),
            "status": "pending",
            "reason": "prepared",
            "receipt": None,
            "created": now,
            "updated": now,
        }
        _check_record(row)
        await session.execute(sa.insert(payments).values(**row))
        await session.commit()
        return row


def _receipt(w3, row):
    """Return proof only after Base finalization and exact sender/token/value checks."""
    plan = row["plan"]
    try:
        receipt = w3.eth.get_transaction_receipt(row["tx_hash"])
    except TransactionNotFound:
        consumed = w3.eth.get_transaction_count(Web3.to_checksum_address(plan["sender"]), "latest") > row["nonce"]
        return ("manual_review", "nonce_consumed_unproven", None) if consumed else ("pending", "not_mined", None)
    if Web3.to_hex(receipt["transactionHash"]) != row["tx_hash"]:
        return "manual_review", "receipt_hash_mismatch", None
    block = w3.eth.get_block(receipt["blockNumber"])
    if HexBytes(block["hash"]) != HexBytes(receipt["blockHash"]):
        return "pending", "receipt_reorged", None
    if w3.eth.get_block("finalized")["number"] < receipt["blockNumber"]:
        return "pending", "awaiting_finality", None
    if receipt["status"] != 1:
        return "manual_review", "transaction_reverted", None
    for log in receipt.get("logs", []):
        try:
            topics = [HexBytes(topic) for topic in log["topics"]]
            if (
                not log.get("removed", False)
                and log["address"].lower() == plan["token_address"]
                and len(topics) == 3
                and topics[0] == TRANSFER_TOPIC
                and topics[1] == HexBytes(encode(["address"], [plan["sender"]]))
                and topics[2] == HexBytes(encode(["address"], [plan["recipient"]]))
                and HexBytes(log["data"]) == HexBytes(encode(["uint256"], [int(plan["amount_atomic"])]))
            ):
                return (
                    "sent",
                    "transfer_finalized",
                    {
                        "transaction_hash": row["tx_hash"],
                        "block_number": receipt["blockNumber"],
                        "block_hash": Web3.to_hex(receipt["blockHash"]),
                        "token_address": plan["token_address"],
                        "sender": plan["sender"],
                        "recipient": plan["recipient"],
                        "amount_atomic": plan["amount_atomic"],
                    },
                )
        except (KeyError, ValueError, TypeError):
            continue
    return "manual_review", "matching_transfer_missing", None


async def _status(row, status, reason, receipt=None):
    async with await new_session() as session:
        # Concurrent observers cannot downgrade a finalized payment or clear a
        # manual hold. Only actual finalized proof can resolve that hold.
        allowed = ["pending", "manual_review"] if status == "sent" else ["pending"]
        await session.execute(
            sa.update(payments)
            .where(
                payments.c.allocation_hash == row["allocation_hash"],
                payments.c.tx_hash == row["tx_hash"],
                payments.c.status.in_(allowed),
            )
            .values(status=status, reason=reason, receipt=receipt, updated=comp._now()),
        )
        await session.commit()
        saved = (await session.execute(sa.select(payments).where(payments.c.allocation_hash == row["allocation_hash"]))).mappings().one()
        return {
            "status": saved["status"],
            "reason": saved["reason"],
            "tx_hash": saved["tx_hash"],
            "digest": saved["plan_hash"],
            "dry_run": False,
        }


async def send_payment(request, *, expected_digest):
    if not get_settings().validator_compensation_send_enabled:
        raise comp.CompensationError("validator compensation sending is disabled")
    prepared = await preview_payment(request)
    plan, digest = prepared["plan"], prepared["digest"]
    if expected_digest != digest:
        raise comp.CompensationError("exact reviewed payment digest required")
    w3, account = _context(plan)
    async with await new_session() as session:
        row = await _existing(session, _request(request))
    if row is None:
        if await _screen(plan):
            raise comp.CompensationError("recipient screening requires review")
        _preflight(w3, plan)
        row = await _bind(plan, digest, w3, account)
    try:
        status, reason, receipt = _receipt(w3, row)
    except Exception:
        return await _status(row, "pending", "receipt_unavailable")
    if status != "pending" or reason != "not_mined" or row["status"] != "pending":
        return await _status(row, status, reason, receipt)
    blocked = await _screen(plan)
    if blocked:
        return await _status(row, "manual_review", blocked)
    try:
        submitted = w3.eth.send_raw_transaction(row["raw_transaction"])
    except Exception:
        # The RPC may have accepted the transfer before losing the response.
        # Keep its original bytes/hash/nonce, never an exception in tx_hash.
        return await _status(row, "pending", "broadcast_unknown")
    if Web3.to_hex(submitted) != row["tx_hash"]:
        return await _status(row, "manual_review", "broadcast_hash_mismatch")
    try:
        status, reason, receipt = _receipt(w3, row)
    except Exception:
        return await _status(row, "pending", "receipt_unavailable")
    return await _status(row, status, reason, receipt)

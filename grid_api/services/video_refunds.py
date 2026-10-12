# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Owned, reviewed Safe refund intents and atomic verified-receipt closure.

No signer, sender, API or loop calls this private candidate. Stored Safe nonces
are never replaced or retried automatically; every uncertain outcome stays owed.
"""

import dataclasses
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..database import new_session
from ..v2.schema import ledger, reservations, video_refunds
from ..v2.schema import media_jobs as jobs
from . import payment_receipt_claims, video_prepaid
from . import video_payment_attempts as attempts
from . import video_payment_proofs as rpc
from . import video_quotes as quotes
from . import video_refund_proofs as proofs


class VideoRefundError(ValueError):
    """Opaque refund failure; never an instruction to send another payment."""


class VideoRefundConflict(VideoRefundError):
    """Immutable Safe nonce, intent, policy or receipt ownership conflict."""


def _now():
    return datetime.now(UTC)


def _purpose(row):
    return {"schema": "aipg.video-refund-receipt.v1", "intent_hash": row["intent_hash"],
        "original_claim_key": row["intent"]["original_claim_key"], "amount_micro": row["intent"]["amount_micro"],
        "safe_transaction_hash": row["safe_transaction_hash"]}


def _receipt_ref(quote_id):
    return f"refund:{quote_id}"


def _check_proof(proof, row):
    if type(proof) is not dict or proof.get("schema") != "aipg.video-safe-refund-proof.v1":
        raise VideoRefundError("stored refund proof changed")
    fields = dict(proof)
    fields.pop("schema")
    try:
        observed = proofs.VerifiedSafeRefund(**fields)
        intent = row["intent"]
        if (any(getattr(observed, key) != intent[key] for key in ("quote_id", "chain_id", "asset", "safe_address",
                "payer", "amount_micro", "safe_nonce", "safe_transaction_hash"))
                or observed.intent_hash != row["intent_hash"] or observed.transaction_hash != row["transaction_hash"]
                or observed.transaction_hash == intent["original_transaction_hash"]
                or observed.confirmation_tag != row["confirmation_tag"]
                or any(type(value) is not int or value < 0 for value in (observed.chain_id, observed.amount_micro,
                    observed.safe_nonce, observed.block_number, observed.block_timestamp, observed.transaction_index,
                    observed.transfer_log_index, observed.execution_log_index, observed.confirmation_block_number))
                or observed.block_timestamp < intent["prepared"]
                or observed.transfer_log_index >= observed.execution_log_index
                or observed.confirmation_block_number - observed.block_number + 1 < row["min_confirmations"]):
            raise VideoRefundError("stored refund proof does not bind its intent")
        for value in (observed.transaction_hash, observed.block_hash, observed.confirmation_block_hash):
            rpc._hex(value, 32)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise VideoRefundError("stored refund proof does not bind its intent") from None


async def _owned(session, account_id, quote_id, *, lock=False):
    quote, attempt = await attempts._owned(session, account_id, quote_id, lock=lock)
    payment = await video_prepaid._payment(session, quote, attempt)
    if payment is None or payment["refund_due_at"] is None:
        raise VideoRefundError("owned closed refund obligation unavailable")
    if payment["admitted_at"] is None:
        for table in (reservations, jobs, ledger):
            identity = quotes._uuid(payment["job_id"]) if table is ledger else payment["job_id"]
            if await session.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.job_id == identity)):
                raise VideoRefundError("unadmitted refund has executable or completion state")
    query = sa.select(video_refunds).where(video_refunds.c.quote_id == quote["id"])
    if lock:
        query = query.with_for_update()
    row = (await session.execute(query)).mappings().one_or_none()
    if row is not None:
        row = dict(row)
        row["created"] = quotes._aware(row["created"])
        expected = proofs.build_intent(quote, attempt, payment, safe_nonce=row["safe_nonce"], created=row["created"])
        proofs.validate_intent(row["intent"])
        if (row["intent"] != expected or row["intent_hash"] != quotes._hash(expected)
                or row["chain_id"] != expected["chain_id"] or row["safe_address"] != expected["safe_address"]
                or row["safe_transaction_hash"] != expected["safe_transaction_hash"]
                or row["confirmation_tag"] not in {"safe", "finalized"} or type(row["min_confirmations"]) is not int
                or not 1 <= row["min_confirmations"] <= 256 or row["created"] < payment["refund_due_at"]):
            raise VideoRefundError("stored refund binding changed")
        if row["status"] == "prepared":
            if any(row[key] is not None for key in ("transaction_hash", "proof", "proof_hash", "receipt_claim_key", "refunded_at")):
                raise VideoRefundError("prepared refund state changed")
        elif row["status"] == "refunded":
            _check_proof(row["proof"], row)
            if (row["proof_hash"] != quotes._hash(row["proof"]) or row["refunded_at"] is None
                    or quotes._aware(row["refunded_at"]) < row["created"]):
                raise VideoRefundError("stored refund receipt changed")
            row["refunded_at"] = quotes._aware(row["refunded_at"])
            claim = await payment_receipt_claims.require_owned_in_session(session, chain_id=row["chain_id"],
                asset=expected["asset"], transaction_hash=row["transaction_hash"],
                owner_kind="video_x402", owner_ref=_receipt_ref(quote["id"]))
            if claim["claim_key"] != row["receipt_claim_key"] or claim["purpose"] != _purpose(row):
                raise VideoRefundError("stored refund ownership changed")
        else:
            raise VideoRefundError("stored refund status changed")
    return quote, attempt, payment, row


async def lookup(account_id, quote_id):
    """Owner-only recovery; expired admission never destroys the obligation."""
    try:
        async with await new_session() as session:
            _quote, _attempt, _payment, row = await _owned(session, account_id, quote_id)
            return row
    except Exception:
        raise VideoRefundError("video refund recovery unavailable") from None


async def prepare(account_id, quote_id, *, safe_nonce: int, confirmation_tag: str, min_confirmations: int):
    """Persist one unsigned, reviewed nonce/intent; no broadcast permission.

    The operator must select/review the original treasury Safe nonce, including
    its other queued transactions. This is not automatic nonce allocation.
    """
    try:
        if (type(safe_nonce) is not int or not 0 <= safe_nonce < 2**63 or confirmation_tag not in {"safe", "finalized"}
                or type(min_confirmations) is not int or not 1 <= min_confirmations <= 256):
            raise VideoRefundError("bounded reviewed Safe refund policy required")
        async with await new_session() as session:
            quote, attempt, payment, existing = await _owned(session, account_id, quote_id, lock=True)
            if existing is not None:
                if (existing["safe_nonce"] != safe_nonce or existing["confirmation_tag"] != confirmation_tag
                        or existing["min_confirmations"] != min_confirmations):
                    raise VideoRefundConflict("refund intent or policy cannot change")
                return existing
            now = _now()
            intent = proofs.build_intent(quote, attempt, payment, safe_nonce=safe_nonce, created=now)
            proofs.validate_intent(intent)
            row = {"quote_id": quote["id"], "chain_id": intent["chain_id"], "safe_address": intent["safe_address"],
                "safe_nonce": safe_nonce, "safe_transaction_hash": intent["safe_transaction_hash"], "intent": intent,
                "intent_hash": quotes._hash(intent), "confirmation_tag": confirmation_tag, "min_confirmations": min_confirmations,
                "status": "prepared", "transaction_hash": None, "proof": None, "proof_hash": None,
                "receipt_claim_key": None, "refunded_at": None, "created": now}
            await session.execute(video_refunds.insert().values(**row))
            await session.commit()
            return row
    except VideoRefundError:
        raise
    except IntegrityError:
        raise VideoRefundConflict("Safe refund nonce or identity is already reserved") from None
    except Exception:
        raise VideoRefundError("video refund preparation unavailable") from None


async def reconcile(account_id, quote_id, *, transaction_hash: str, sources: tuple[rpc.PaymentRPC, rpc.PaymentRPC]):
    """Prove outside locks, then commit shared ownership and refund closure once."""
    try:
        tx = rpc._hex(transaction_hash, 32)
        async with await new_session() as session:
            _quote, _attempt, _payment, row = await _owned(session, account_id, quote_id)
        if row is None:
            raise VideoRefundError("prepared refund unavailable")
        if row["status"] == "refunded":
            if row["transaction_hash"] != tx:
                raise VideoRefundConflict("refund already has its original transaction")
            return row
        observed = await proofs.prove_refund(row["intent"], transaction_hash=tx, sources=sources,
            confirmation_tag=row["confirmation_tag"], min_confirmations=row["min_confirmations"])
        proof = {"schema": "aipg.video-safe-refund-proof.v1", **dataclasses.asdict(observed)}
        _check_proof(proof, {**row, "transaction_hash": tx})
        async with await new_session() as session:
            quote, _attempt, _payment, current = await _owned(session, account_id, quote_id, lock=True)
            if current is None or current["intent_hash"] != row["intent_hash"]:
                raise VideoRefundConflict("refund changed during reconciliation")
            if current["status"] == "refunded":
                if current["transaction_hash"] != tx:
                    raise VideoRefundConflict("refund already has another transaction")
                return current
            _check_proof(proof, {**current, "transaction_hash": tx})
            await payment_receipt_claims.claim_in_session(session, chain_id=current["chain_id"], asset=current["intent"]["asset"],
                transaction_hash=tx, owner_kind="video_x402", owner_ref=_receipt_ref(quote["id"]), purpose=_purpose(current))
            claim = await payment_receipt_claims.require_owned_in_session(session, chain_id=current["chain_id"],
                asset=current["intent"]["asset"], transaction_hash=tx, owner_kind="video_x402", owner_ref=_receipt_ref(quote["id"]))
            values = {"status": "refunded", "transaction_hash": tx, "proof": proof, "proof_hash": quotes._hash(proof),
                      "receipt_claim_key": claim["claim_key"], "refunded_at": _now()}
            await session.execute(video_refunds.update().where(video_refunds.c.quote_id == quote["id"]).values(**values))
            await session.commit()
            return {**current, **values}
    except (VideoRefundError, rpc.VideoPaymentProofError):
        raise
    except payment_receipt_claims.ReceiptClaimConflict:
        raise VideoRefundConflict("refund receipt belongs to another economic purpose; retain obligation") from None
    except Exception:
        raise VideoRefundError("video refund reconciliation unavailable; retain obligation") from None

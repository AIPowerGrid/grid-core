# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Quote-bound pre-send journal and historical proof, without funded admission.

No route or background sender calls this candidate. Exactly one begin caller can
win; every replay is observation, never permission to contact a facilitator.
Only the authorization's fingerprint is stored, not its spending signature.
Cross-rail receipt ownership and atomic external hold/outbox remain separate.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime

import sqlalchemy as sa

from ..database import new_session
from ..v2.schema import accounts
from ..v2.schema import video_payment_attempts as attempts
from . import identities
from . import video_eip3009 as bindings
from . import video_payment_proofs as proofs
from . import video_quotes as quotes


class VideoAttemptError(ValueError):
    """Private generic failure; never include a capability or SQL value."""


class VideoAttemptConflict(VideoAttemptError):
    """Immutable attempt identity, payer, policy or transaction conflict."""


def _now() -> datetime:
    return datetime.now(UTC)


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(quotes._canonical(payload).encode("ascii")).hexdigest()


def _validate(row: dict, quote: dict) -> dict:
    snapshot = quotes._validate_row(quote)
    domain = snapshot["payment"].get("eip712_domain")
    if (domain is None or row["quote_id"] != quote["id"] or row["quote_commitment"] != quote["commitment"]
            or row["payer"] != quotes._address(row["payer"])
            or row["authorization_identity"] != bindings._identity(domain, row["payer"], quote["authorization_nonce"])
            or row["confirmation_tag"] not in {"safe", "finalized"}
            or type(row["min_confirmations"]) is not int or not 1 <= row["min_confirmations"] <= 256
            or not quotes._aware(quote["created"]) <= quotes._aware(row["created"]) < quotes._aware(quote["expires"])):
        raise VideoAttemptError("stored video attempt binding changed")
    quotes._digest(row["payload_hash"])
    status, tx, proof = row["status"], row["transaction_hash"], row["proof"]
    if (status not in {"settling", "reported", "proven", "manual_review"}
            or (status == "settling" and tx is not None)
            or (status in {"reported", "proven"} and tx is None)
            or (status == "manual_review" and row["review_code"] not in {"receipt_unproven", "receipt_claim_conflict"})
            or (status != "manual_review" and row["review_code"] is not None)):
        raise VideoAttemptError("stored video attempt state changed")
    if tx is not None:
        proofs._hex(tx, 32)
    if proof is None:
        if row["proof_hash"] is not None or row["proven_at"] is not None or status == "proven":
            raise VideoAttemptError("stored video proof is missing")
    else:
        if (status not in {"proven", "manual_review"} or row["proven_at"] is None
                or row["proof_hash"] != quotes._hash(proof)):
            raise VideoAttemptError("stored video proof changed")
        _check_proof(proof, quote, row)
    return snapshot


def _check_proof(proof: dict, quote: dict, attempt: dict) -> None:
    if type(proof) is not dict or proof.get("schema") != "aipg.video-transfer-proof.v1":
        raise VideoAttemptError("invalid historical video proof")
    fields = dict(proof)
    fields.pop("schema")
    try:
        evidence = proofs.VerifiedVideoTransfer(**fields)
        snapshot = quote["snapshot"]
        payment = snapshot["payment"]
        if (evidence.quote_id != str(quote["id"]) or evidence.job_id != str(quote["job_id"])
                or evidence.chain_id != payment["eip712_domain"]["chainId"] or evidence.asset != payment["asset"]
                or evidence.payer != attempt["payer"] or evidence.recipient != payment["pay_to"]
                or type(evidence.amount_micro) is not int or evidence.amount_micro != quote["amount_micro"]
                or type(evidence.chain_id) is not int or evidence.authorization_nonce != quote["authorization_nonce"]
                or evidence.transaction_hash != attempt["transaction_hash"]
                or evidence.confirmation_tag != attempt["confirmation_tag"]
                or any(type(value) is not int or value < 0 for value in (
                    evidence.block_number, evidence.block_timestamp, evidence.transaction_index,
                    evidence.transfer_log_index, evidence.authorization_log_index, evidence.confirmation_block_number))
                or evidence.confirmation_block_number - evidence.block_number + 1 < attempt["min_confirmations"]
                or evidence.transfer_log_index == evidence.authorization_log_index
                or not int(quotes._aware(attempt["created"]).timestamp()) <= evidence.block_timestamp < snapshot["expires"]):
            raise VideoAttemptError("historical video proof does not bind its prior attempt")
        proofs._hex(evidence.block_hash, 32)
        proofs._hex(evidence.confirmation_block_hash, 32)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise VideoAttemptError("historical video proof does not bind its prior attempt") from None


async def _owned(session, account_id, quote_id, *, lock: bool = False) -> tuple[dict, dict | None]:
    quote = await quotes._lookup(session, account_id, quote_id=quote_id)
    if quote is None:
        raise VideoAttemptError("video quote or attempt unavailable")
    if lock:
        canonical = await identities.canonical_account_id(account_id, session=session)
        ids = {quotes._uuid(account_id), canonical, quote["account_id"]}
        locked = (await session.execute(sa.select(accounts.c.id).where(accounts.c.id.in_(ids))
            .order_by(accounts.c.id).with_for_update())).scalars().all()
        if canonical not in locked or await identities.canonical_account_id(account_id, session=session) != canonical:
            raise VideoAttemptConflict("video attempt owner changed")
        # Account locks fence merges; the quote lock serializes duplicate callers.
        from ..v2.schema import video_quotes as quote_table
        await session.execute(sa.select(quote_table.c.id).where(quote_table.c.id == quote["id"]).with_for_update())
        quote = await quotes._lookup(session, account_id, quote_id=quote_id)
        if quote is None:
            raise VideoAttemptConflict("video attempt owner changed")
    query = sa.select(attempts).where(attempts.c.quote_id == quote["id"])
    if lock:
        query = query.with_for_update()
    row = (await session.execute(query)).mappings().one_or_none()
    row = dict(row) if row is not None else None
    if row is not None:
        row["created"] = quotes._aware(row["created"])
        if row["proven_at"] is not None:
            row["proven_at"] = quotes._aware(row["proven_at"])
        _validate(row, quote)
    return quote, row


async def lookup(account_id, quote_id) -> dict | None:
    """Owner-loaded private recovery; no deadline gate, signature or network."""
    try:
        async with await new_session() as session:
            _quote, row = await _owned(session, account_id, quote_id)
            return row
    except VideoAttemptError:
        raise
    except Exception:
        raise VideoAttemptError("video attempt recovery unavailable") from None


async def begin(account_id, quote_id, payload: dict, *, expected_payer: str,
                confirmation_tag: str, min_confirmations: int, admission_until: datetime) -> tuple[dict, bool]:
    """Commit before any send; only the winning return may attempt settlement.

    This is not an HTTP/pay feature. Its caller must enforce authentication,
    pilot/model/runtime/source gates and stage the original private plan. No
    spending capability is retained for automatic retry after a crash.
    """
    try:
        if (confirmation_tag not in {"safe", "finalized"} or type(min_confirmations) is not int
                or not 1 <= min_confirmations <= 256 or not isinstance(admission_until, datetime)
                or admission_until.tzinfo is None):
            raise VideoAttemptError("explicit reviewed attempt policy required")
        payer, digest = quotes._address(expected_payer), _payload_hash(payload)
        async with await new_session() as session:
            quote, existing = await _owned(session, account_id, quote_id, lock=True)
            if existing is not None:
                if (existing["payer"] != payer or existing["payload_hash"] != digest
                        or existing["confirmation_tag"] != confirmation_tag or existing["min_confirmations"] != min_confirmations):
                    raise VideoAttemptConflict("video attempt is already bound to another authorization or policy")
                return existing, False
            if admission_until <= _now():
                raise VideoAttemptError("video payment admission expired")
            verified = bindings.verify(quote, payload, expected_payer=payer)
            created = _now().replace(microsecond=0)
            if created >= quotes._aware(quote["expires"]) or admission_until <= _now():
                raise VideoAttemptError("video payment admission expired")
            values = {"quote_id": quote["id"], "quote_commitment": quote["commitment"],
                "authorization_identity": verified.authorization_identity, "payload_hash": digest, "payer": payer,
                "confirmation_tag": confirmation_tag, "min_confirmations": min_confirmations,
                "status": "settling", "transaction_hash": None, "proof": None, "proof_hash": None,
                "proven_at": None, "review_code": None, "created": created}
            _validate(values, quote)
            await session.execute(attempts.insert().values(**values))
            if _now() >= quotes._aware(quote["expires"]) or admission_until <= _now():
                raise VideoAttemptError("video payment admission expired before commit")
            await session.commit()
            return values, True
    except VideoAttemptError:
        raise
    except Exception:
        raise VideoAttemptError("video payment attempt unavailable") from None


async def report(account_id, quote_id, transaction_hash: str) -> dict:
    """Record one immutable facilitator correlation, not a receipt or funding."""
    try:
        tx = proofs._hex(transaction_hash, 32)
        async with await new_session() as session:
            quote, row = await _owned(session, account_id, quote_id, lock=True)
            if row is None:
                raise VideoAttemptError("video quote or attempt unavailable")
            if row["transaction_hash"] is not None:
                if row["transaction_hash"] != tx:
                    raise VideoAttemptConflict("video payment already has another transaction")
                return row
            if row["status"] != "settling":
                raise VideoAttemptConflict("video payment cannot accept a report")
            row.update(status="reported", transaction_hash=tx)
            _validate(row, quote)
            await session.execute(attempts.update().where(attempts.c.quote_id == row["quote_id"])
                .values(status="reported", transaction_hash=tx))
            await session.commit()
            return row
    except VideoAttemptError:
        raise
    except Exception:
        raise VideoAttemptError("video payment report unavailable") from None


async def reconcile(account_id, quote_id, *, sources: tuple[proofs.PaymentRPC, proofs.PaymentRPC]) -> dict:
    """Persist historical evidence without a hold, claim, payout or dispatch.

    Network calls occur outside SQL/account/quote locks. Proven replay reads the
    original winning proof without network or updated anchors. Pending/transport
    uncertainty leaves the attempt unchanged; failed proof goes to manual review.
    This journal cannot resolve an unknown transaction without the future bounded
    nonce watcher or manual correlation, and never resends its authorization.
    """
    try:
        async with await new_session() as session:
            quote, row = await _owned(session, account_id, quote_id)
        if row is None or row["transaction_hash"] is None:
            raise VideoAttemptError("video payment has no original transaction to reconcile")
        if row["status"] == "proven":
            return row
        failed = False
        try:
            observed = await proofs.prove_transfer(quote, payer=row["payer"], transaction_hash=row["transaction_hash"],
                sources=sources, confirmation_tag=row["confirmation_tag"], min_confirmations=row["min_confirmations"])
            proof = {"schema": "aipg.video-transfer-proof.v1", **dataclasses.asdict(observed)}
            _check_proof(proof, quote, row)
        except (proofs.VideoPaymentPending, proofs.VideoPaymentUnavailable):
            raise
        except (proofs.VideoPaymentProofError, VideoAttemptError):
            failed = True
        async with await new_session() as session:
            current_quote, current = await _owned(session, account_id, quote_id, lock=True)
            if current is None or current["transaction_hash"] != row["transaction_hash"]:
                raise VideoAttemptConflict("video payment changed during reconciliation")
            if current["status"] == "proven":
                return current
            if failed:
                current.update(status="manual_review", review_code="receipt_unproven")
            else:
                current.update(status="proven", proof=proof, proof_hash=quotes._hash(proof), proven_at=_now(), review_code=None)
                from . import payment_receipt_claims as receipt_claims

                try:
                    await receipt_claims.claim_in_session(session, chain_id=proof["chain_id"], asset=proof["asset"],
                        transaction_hash=proof["transaction_hash"], owner_kind="video_x402", owner_ref=str(current["quote_id"]),
                        observed_nonces=((proof["payer"], proof["authorization_nonce"]),),
                        purpose={"schema": "aipg.video-receipt.v1", "quote_commitment": current["quote_commitment"],
                                 "authorization_identity": current["authorization_identity"], "amount_micro": proof["amount_micro"]})
                except receipt_claims.ReceiptClaimConflict:
                    current.update(status="manual_review", review_code="receipt_claim_conflict")
            _validate(current, current_quote)
            await session.execute(attempts.update().where(attempts.c.quote_id == current["quote_id"])
                .values(**{key: current[key] for key in ("status", "proof", "proof_hash", "proven_at", "review_code")}))
            await session.commit()
            return current
    except (VideoAttemptError, proofs.VideoPaymentProofError):
        raise
    except Exception:
        raise VideoAttemptError("video payment reconciliation unavailable") from None

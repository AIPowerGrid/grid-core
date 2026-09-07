# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reviewed, allocation-specific payout consent. No transaction signing/sending.

The node signs a destination instruction; the destination signs acceptance of
the same message. Neither a visibility pairing nor an account wallet fallback
is payment authority. Stored consent is immutable, including on retries.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import timedelta

import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct

from ..database import new_session
from ..v2 import schema as tables
from . import validator_compensation as comp
from . import wallet_proofs

SCHEMA = "aipg.validator.compensation.recipient.v1"
AUDIENCE = "https://api.aipowergrid.io"
FIELDS = {
    "schema",
    "audience",
    "campaign_id",
    "contract_hash",
    "allocation_hash",
    "operator_group_id",
    "account_id",
    "validator_id",
    "signing_wallet",
    "chain_id",
    "token_address",
    "asset",
    "decimals",
    "amount_atomic",
    "recipient",
    "issued_at",
    "expires_at",
}
recipients = tables.validator_compensation_recipients


def _address(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-f]{40}", value) or int(value, 16) == 0:
        raise comp.CompensationError("nonzero, lowercase Base address required")
    return value


def consent_message(consent):
    """Exact EIP-191 text used by both node and recipient wallet clients."""
    if not isinstance(consent, dict) or set(consent) != FIELDS:
        raise comp.CompensationError("invalid recipient consent fields")
    if (
        consent["schema"] != SCHEMA
        or consent["audience"] != AUDIENCE
        or type(consent["chain_id"]) is not int
        or consent["chain_id"] != 8453
    ):
        raise comp.CompensationError("wrong recipient consent domain or chain")
    if consent["asset"] != "AIPG" or type(consent["decimals"]) is not int or consent["decimals"] != 18:
        raise comp.CompensationError("wrong recipient consent asset or decimals")
    for key in ("recipient", "signing_wallet", "token_address"):
        _address(consent[key])
    if consent["recipient"] in {consent["signing_wallet"], consent["token_address"]}:
        raise comp.CompensationError("select a payout wallet, not the node signer or token contract")
    if not isinstance(consent["amount_atomic"], str) or not re.fullmatch(r"[1-9][0-9]{0,77}", consent["amount_atomic"]):
        raise comp.CompensationError("positive integer allocation required")
    issued, expires = comp._time(consent["issued_at"]), comp._time(consent["expires_at"])
    if not timedelta(0) < expires - issued <= timedelta(days=1):
        raise comp.CompensationError("recipient consent expires within 24 hours")
    try:
        encoded = json.dumps(consent, sort_keys=True, indent=2, allow_nan=False)
    except (TypeError, ValueError):
        raise comp.CompensationError("invalid recipient consent encoding") from None
    if len(encoded.encode()) > 4096:
        raise comp.CompensationError("recipient consent too large")
    whole, fraction = divmod(int(consent["amount_atomic"]), 10**18)
    amount = f"{whole}.{fraction:018d}".rstrip("0").rstrip(".")
    return (
        "AI Power Grid validator compensation payout consent\n"
        f"Reward: {amount} AIPG on Base (chain 8453)\n"
        f"Destination: {consent['recipient']}\n"
        "Authorize only the exact earned allocation below to this Base recipient.\n"
        "This is not a token approval, login, or authorization for other rewards.\n\n" + encoded
    )


async def _snapshot(session, campaign_id, operator_group_id, *, apply):
    campaign = (
        (
            await session.execute(
                sa.select(tables.validator_compensation_campaigns).where(
                    tables.validator_compensation_campaigns.c.id == campaign_id,
                ),
            )
        )
        .mappings()
        .first()
    )
    if not campaign or campaign["status"] != "finalized":
        raise comp.CompensationError("finalized campaign required")
    contract = campaign["contract"]
    if comp._hash(contract) != campaign["contract_hash"]:
        raise comp.CompensationError("campaign contract commitment changed")
    await comp._committed_result(session, campaign)
    allocation = (
        (
            await session.execute(
                sa.select(tables.validator_compensation_allocations).where(
                    tables.validator_compensation_allocations.c.campaign_id == campaign_id,
                    tables.validator_compensation_allocations.c.operator_group_id == operator_group_id,
                ),
            )
        )
        .mappings()
        .first()
    )
    if not allocation or int(allocation["amount_atomic"]) <= 0:
        raise comp.CompensationError("positive finalized allocation required")
    member = next(item for item in contract["members"] if item["operator_group_id"] == operator_group_id)
    query = sa.select(tables.validators).where(tables.validators.c.id == member["validator_id"])
    node = (await session.execute(query.with_for_update() if apply else query)).mappings().first()
    if not node or str(node["account_id"]) != member["account_id"] or node["signing_wallet"] != member["signing_wallet"]:
        raise comp.CompensationError("beneficiary node identity changed; manual recovery required")
    query = sa.select(tables.accounts.c.id).where(tables.accounts.c.id == node["account_id"])
    account = await session.scalar(query.with_for_update() if apply else query)
    retired = await session.scalar(
        sa.select(tables.account_aliases.c.source_account_id).where(
            tables.account_aliases.c.source_account_id == node["account_id"],
        ),
    )
    if not account or retired:
        raise comp.CompensationError("beneficiary account changed; manual recovery required")
    return {
        "schema": SCHEMA,
        "audience": AUDIENCE,
        "campaign_id": campaign_id,
        "contract_hash": campaign["contract_hash"],
        "allocation_hash": allocation["allocation_hash"],
        "operator_group_id": operator_group_id,
        "account_id": member["account_id"],
        "validator_id": member["validator_id"],
        "signing_wallet": member["signing_wallet"],
        "chain_id": contract["chain_id"],
        "token_address": contract["token_address"],
        "asset": contract["asset"],
        "decimals": contract["decimals"],
        "amount_atomic": str(int(allocation["amount_atomic"])),
    }


async def prepare_recipient(campaign_id, operator_group_id, recipient):
    """Private, read-only signing request; contains no secret key or authority."""
    _address(recipient)
    async with await new_session() as session:
        await comp._transaction(session, False)
        consent = await _snapshot(session, campaign_id, operator_group_id, apply=False)
    now = comp._now()
    consent.update(recipient=recipient, issued_at=now.isoformat(), expires_at=(now + timedelta(days=1)).isoformat())
    return {"consent": consent, "message": consent_message(consent), "sendable": False}


async def bind_recipient(request, *, apply=False, expected_digest=None):
    if not isinstance(request, dict) or set(request) != {"consent", "node_signature", "recipient_signature", "approval_ref"}:
        raise comp.CompensationError("invalid signed recipient request")
    if not isinstance(request["approval_ref"], str) or not re.fullmatch(r"approval:[A-Za-z0-9_-]{3,96}", request["approval_ref"]):
        raise comp.CompensationError("explicit recipient review reference required")
    consent = request["consent"]
    message = consent_message(consent)
    for field in ("node_signature", "recipient_signature"):
        if not isinstance(request[field], str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2}){1,8192}", request[field]):
            raise comp.CompensationError("bounded hexadecimal signature required")
    # Freeze caller-owned input before the first database/RPC await.
    request = copy.deepcopy(request)
    consent = request["consent"]
    digest = comp._hash(request)
    if apply and expected_digest != digest:
        raise comp.CompensationError("recipient approval digest missing or changed")
    async with await new_session() as session:
        await comp._transaction(session, apply)
        snapshot = await _snapshot(session, consent["campaign_id"], consent["operator_group_id"], apply=apply)
        if any(consent[key] != value for key, value in snapshot.items()):
            raise comp.CompensationError("recipient consent does not match earned allocation")
        existing = (
            (
                await session.execute(
                    sa.select(recipients).where(
                        recipients.c.campaign_id == consent["campaign_id"],
                        recipients.c.operator_group_id == consent["operator_group_id"],
                    ),
                )
            )
            .mappings()
            .first()
        )
        if existing:
            if (
                comp._hash(existing["proof"]) != existing["proof_hash"]
                or existing["proof_hash"] != digest
                or existing["recipient"] != consent["recipient"]
                or existing["allocation_hash"] != consent["allocation_hash"]
            ):
                raise comp.CompensationError("recipient is immutable or stored commitment changed")
            # An acknowledged commit can be replayed after its signing deadline;
            # it must never create a replacement recipient or a second payment.
            return {"status": "bound", "digest": digest, "dry_run": not apply, "sendable": False}
        now = comp._now()
        if not comp._time(consent["issued_at"]) <= now < comp._time(consent["expires_at"]):
            raise comp.CompensationError("recipient consent is not currently valid")
        try:
            recovered = Account.recover_message(encode_defunct(text=message), signature=request["node_signature"]).lower()
        except Exception:
            recovered = ""
        if recovered != consent["signing_wallet"]:
            raise comp.CompensationError("node payout consent signature is invalid")
        # EOA proofs need no RPC. Contract-wallet proofs additionally pin the
        # RPC chain before using the existing fail-closed EIP-1271 verifier.
        try:
            recipient_signer = Account.recover_message(encode_defunct(text=message), signature=request["recipient_signature"]).lower()
        except Exception:
            recipient_signer = ""
        if recipient_signer != consent["recipient"]:
            try:
                chain = await wallet_proofs._rpc("eth_chainId", [])
                valid = chain == "0x2105" and await wallet_proofs.verify_personal_signature(
                    message=message,
                    signature=request["recipient_signature"],
                    address=consent["recipient"],
                )
            except Exception:
                valid = False
            if not valid:
                raise comp.CompensationError("recipient wallet consent is invalid or unverifiable")
        if comp._now() >= comp._time(consent["expires_at"]):
            raise comp.CompensationError("recipient consent expired during verification")
        if apply:
            await session.execute(
                sa.insert(recipients).values(
                    campaign_id=consent["campaign_id"],
                    operator_group_id=consent["operator_group_id"],
                    allocation_hash=consent["allocation_hash"],
                    recipient=consent["recipient"],
                    proof=request,
                    proof_hash=digest,
                    created=comp._now(),
                ),
            )
            await session.commit()
        return {"status": "bound" if apply else "preview", "digest": digest, "dry_run": not apply, "sendable": False}

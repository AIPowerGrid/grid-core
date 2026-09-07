# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private operator status and dual-signature collection, never payment approval."""

from __future__ import annotations

import copy
import re
import secrets
from datetime import timedelta
from uuid import UUID

import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from sqlalchemy.dialects.postgresql import JSONB

from ..config import get_settings
from ..database import new_session
from ..v2 import schema as tables
from . import validator_compensation as comp
from . import validator_compensation_recipients as recipients
from . import validator_pairing as pairing
from . import wallet_proofs

requests = tables.validator_compensation_requests
SCHEMA = "aipg.validator.compensation.operator.v1"
CONSOLE_URL = "https://console.aipowergrid.io/dashboard/validator-payout/"


class OperatorError(ValueError):
    def __init__(self, code, status_code=409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def _enabled():
    if not get_settings().validator_compensation_operator_enabled:
        raise OperatorError("compensation_unavailable", 503)


async def _begin(session, *, readonly=False):
    _enabled()
    if session.get_bind().dialect.name != "postgresql":
        raise OperatorError("compensation_unavailable", 503)
    if readonly:
        await session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))


async def _node(session, account_id, wallet, *, lock=False):
    aid = UUID(str(account_id))
    query = sa.select(tables.validators).where(tables.validators.c.account_id == aid)
    node = (await session.execute(query.with_for_update() if lock else query)).mappings().first()
    if lock:
        _enabled()
    retired = await session.scalar(
        sa.select(tables.account_aliases.c.source_account_id).where(tables.account_aliases.c.source_account_id == aid),
    )
    if not node or retired or node["status"] not in {"active", "suspended"} or node["signing_wallet"] != (wallet or "").lower():
        raise OperatorError("node_identity_unavailable", 403)
    return node


async def _allocation(session, node, allocation_hash):
    row = (
        (
            await session.execute(
                sa.select(tables.validator_compensation_allocations).where(
                    tables.validator_compensation_allocations.c.allocation_hash == allocation_hash,
                    tables.validator_compensation_allocations.c.account_id == node["account_id"],
                ),
            )
        )
        .mappings()
        .first()
    )
    if not row:
        raise OperatorError("allocation_not_found", 404)
    snapshot = await recipients._snapshot(session, row["campaign_id"], row["operator_group_id"], apply=False)
    if snapshot["validator_id"] != node["id"]:
        raise OperatorError("allocation_not_found", 404)
    return snapshot


async def _link(session, node, *, human_id=None):
    await pairing._canonical_account(session, node["account_id"])
    link = await pairing._live_link(session, node)
    if not link or (human_id is not None and link["operator_account_id"] != UUID(str(human_id))):
        raise OperatorError("account_link_required", 403)
    await pairing._canonical_account(session, link["operator_account_id"])
    return link


async def _binding(session, allocation_hash):
    return (
        (
            await session.execute(
                sa.select(tables.validator_compensation_recipients).where(
                    tables.validator_compensation_recipients.c.allocation_hash == allocation_hash,
                ),
            )
        )
        .mappings()
        .first()
    )


def _view(row):
    expired = comp._time(row["expires_at"]) <= comp._now()
    result = {
        "schema": SCHEMA,
        "request_id": row["id"],
        "allocation_hash": row["allocation_hash"],
        "status": "expired" if expired else row["status"],
        "expires_at": comp._time(row["expires_at"]).isoformat(),
        "approval_url": CONSOLE_URL + row["id"],
        "payment_authorized": False,
    }
    if row["consent"] is not None:
        result.update(consent=row["consent"], message=recipients.consent_message(row["consent"]), review_hash=comp._hash(row["consent"]))
    return result


async def status(account_id, wallet, *, offset=0):
    async with await new_session() as session:
        await _begin(session, readonly=True)
        node = await _node(session, account_id, wallet)
        campaign_rows = (
            (
                await session.execute(
                    sa.select(tables.validator_compensation_campaigns)
                    .where(
                        sa.cast(tables.validator_compensation_campaigns.c.contract, JSONB)["members"].contains(
                            [
                                {"validator_id": node["id"], "account_id": str(node["account_id"])},
                            ],
                        ),
                    )
                    .order_by(tables.validator_compensation_campaigns.c.created.desc(), tables.validator_compensation_campaigns.c.id)
                    .limit(26),
                )
            )
            .mappings()
            .all()
        )
        campaigns = []
        now = comp._now()
        for campaign in campaign_rows[:25]:
            contract = campaign["contract"]
            if comp._hash(contract) != campaign["contract_hash"]:
                raise OperatorError("campaign_requires_review")
            member = next(item for item in contract["members"] if item["validator_id"] == node["id"])
            state = (
                "finalized"
                if campaign["status"] == "finalized"
                else "scheduled"
                if now < comp._time(contract["starts_at"])
                else "earning"
                if now < comp._time(contract["ends_at"])
                else "awaiting_finalization"
            )
            campaigns.append(
                {
                    "campaign_id": campaign["id"],
                    "status": state,
                    "starts_at": contract["starts_at"],
                    "ends_at": contract["ends_at"],
                    "asset": contract["asset"],
                    "decimals": contract["decimals"],
                    "operator_cap_atomic": contract["operator_cap_atomic"],
                    "identity_review_required": member["signing_wallet"] != node["signing_wallet"],
                },
            )
        allocation_table = tables.validator_compensation_allocations
        binding_table = tables.validator_compensation_recipients
        payment_table = tables.validator_compensation_payments
        rows = (
            (
                await session.execute(
                    sa.select(
                        allocation_table,
                        binding_table.c.recipient,
                        payment_table.c.status.label("payment_status"),
                        payment_table.c.tx_hash,
                        requests.c.status.label("request_status"),
                        requests.c.id.label("request_id"),
                        requests.c.expires_at,
                    )
                    .outerjoin(binding_table, binding_table.c.allocation_hash == allocation_table.c.allocation_hash)
                    .outerjoin(payment_table, payment_table.c.allocation_hash == allocation_table.c.allocation_hash)
                    .outerjoin(requests, requests.c.allocation_hash == allocation_table.c.allocation_hash)
                    .where(
                        tables.validator_compensation_allocations.c.account_id == node["account_id"],
                    )
                    .order_by(
                        tables.validator_compensation_allocations.c.created.desc(),
                        tables.validator_compensation_allocations.c.allocation_hash,
                    )
                    .offset(offset)
                    .limit(26),
                )
            )
            .mappings()
            .all()
        )
        items = []
        for allocation in rows[:25]:
            pending = allocation["request_status"]
            if pending and comp._time(allocation["expires_at"]) <= comp._now():
                pending = "expired"
            state = (
                allocation["payment_status"]
                if allocation["payment_status"]
                else "ready_for_payment"
                if allocation["recipient"]
                else pending
                if pending
                else "wallet_required"
            )
            amount = int(allocation["amount_atomic"])
            if amount == 0:
                state = "no_payment_due"
            items.append(
                {
                    "allocation_hash": allocation["allocation_hash"],
                    "campaign_id": allocation["campaign_id"],
                    "amount_atomic": str(amount),
                    "asset": "AIPG",
                    "decimals": 18,
                    "chain_id": 8453,
                    "reviewed_units": allocation["reviewed_units"],
                    "status": state,
                    "recipient": allocation["recipient"],
                    "transaction_hash": allocation["tx_hash"],
                    "request_id": allocation["request_id"],
                },
            )
        return {
            "schema": SCHEMA,
            "validator_id": node["id"],
            "campaigns": campaigns,
            "campaigns_has_more": len(campaign_rows) > 25,
            "items": items,
            "next_offset": offset + 25 if len(rows) > 25 else None,
        }


async def start(account_id, wallet, allocation_hash):
    async with await new_session() as session:
        await _begin(session)
        node = await _node(session, account_id, wallet, lock=True)
        snapshot = await _allocation(session, node, allocation_hash)
        link = await _link(session, node)
        if await _binding(session, allocation_hash):
            raise OperatorError("recipient_already_bound")
        row = (await session.execute(sa.select(requests).where(requests.c.allocation_hash == allocation_hash))).mappings().first()
        now = comp._now()
        if row and row["status"] != "cancelled" and comp._time(row["expires_at"]) > now:
            if row["pairing_id"] != link["pairing_id"] or row["operator_account_id"] != link["operator_account_id"]:
                raise OperatorError("account_link_changed")
            return _view(row)
        row = {
            "allocation_hash": snapshot["allocation_hash"],
            "id": "vpc_" + secrets.token_hex(32),
            "operator_account_id": link["operator_account_id"],
            "pairing_id": link["pairing_id"],
            "status": "awaiting_wallet",
            "consent": None,
            "recipient_signature": None,
            "node_signature": None,
            "created": now,
            "expires_at": now + timedelta(hours=24),
        }
        await session.execute(sa.delete(requests).where(requests.c.allocation_hash == allocation_hash))
        await session.execute(sa.insert(requests).values(**row))
        await session.commit()
        return _view(row)


async def _request(session, request_id, account_id, wallet, *, human=False, lock=False, allow_bound=False):
    # First locate the owner, then lock the node and re-read the replaceable slot.
    found = (
        (
            await session.execute(
                sa.select(requests, tables.validator_compensation_allocations.c.account_id)
                .join(
                    tables.validator_compensation_allocations,
                    requests.c.allocation_hash == tables.validator_compensation_allocations.c.allocation_hash,
                )
                .where(requests.c.id == request_id),
            )
        )
        .mappings()
        .first()
    )
    if not found or (found["operator_account_id"] if human else found["account_id"]) != UUID(str(account_id)):
        raise OperatorError("request_not_found", 404)
    if human:
        registered_wallet = await session.scalar(
            sa.select(tables.validators.c.signing_wallet).where(tables.validators.c.account_id == found["account_id"]),
        )
        node = await _node(session, found["account_id"], registered_wallet, lock=lock)
    else:
        node = await _node(session, account_id, wallet, lock=lock)
    link = await _link(session, node, human_id=account_id if human else None)
    row = (await session.execute(sa.select(requests).where(requests.c.id == request_id))).mappings().first()
    if not row or row["pairing_id"] != link["pairing_id"] or row["operator_account_id"] != link["operator_account_id"]:
        raise OperatorError("account_link_changed")
    snapshot = await _allocation(session, node, row["allocation_hash"])
    if not allow_bound and await _binding(session, row["allocation_hash"]):
        raise OperatorError("recipient_already_bound")
    if row["consent"] and any(row["consent"].get(key) != value for key, value in snapshot.items()):
        raise OperatorError("consent_changed")
    return row, snapshot


def _fresh(row):
    if row["status"] == "cancelled":
        raise OperatorError("consent_cancelled")
    if comp._time(row["expires_at"]) <= comp._now():
        raise OperatorError("consent_expired")


async def inspect(account_id, wallet, request_id, *, human=False):
    async with await new_session() as session:
        await _begin(session, readonly=True)
        row, _ = await _request(session, request_id, account_id, wallet, human=human, allow_bound=True)
        binding = await _binding(session, row["allocation_hash"])
        if binding:
            return {
                "schema": SCHEMA,
                "request_id": request_id,
                "allocation_hash": row["allocation_hash"],
                "status": "recipient_bound",
                "recipient": binding["recipient"],
                "payment_authorized": False,
            }
        return _view(row)


async def cancel(account_id, wallet, request_id):
    async with await new_session() as session:
        await _begin(session)
        row, _ = await _request(session, request_id, account_id, wallet, lock=True)
        if row["status"] == "review_required":
            raise OperatorError("maintainer_review_required")
        await session.execute(sa.update(requests).where(requests.c.id == request_id).values(status="cancelled"))
        await session.commit()
        return _view({**row, "status": "cancelled"})


async def prepare(account_id, request_id, recipient):
    recipients._address(recipient)
    async with await new_session() as session:
        await _begin(session)
        row, snapshot = await _request(session, request_id, account_id, None, human=True, lock=True)
        _fresh(row)
        if row["status"] != "awaiting_wallet":
            raise OperatorError("consent_already_signed")
        proof = {
            **snapshot,
            "recipient": recipient,
            "issued_at": comp._now().isoformat(),
            "expires_at": comp._time(row["expires_at"]).isoformat(),
        }
        recipients.consent_message(proof)
        await session.execute(sa.update(requests).where(requests.c.id == request_id).values(consent=proof))
        await session.commit()
        return _view({**row, "consent": proof})


def _recover(message, signature):
    try:
        return Account.recover_message(encode_defunct(text=message), signature=signature).lower()
    except Exception:
        return None


async def approve(account_id, wallet, request_id, review_hash, signature, *, human=False):
    if not isinstance(signature, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2}){1,8192}", signature):
        raise OperatorError("invalid_signature", 400)
    async with await new_session() as session:
        await _begin(session, readonly=True)
        row, _ = await _request(session, request_id, account_id, wallet, human=human)
        _fresh(row)
        proof = copy.deepcopy(row["consent"])
    if not proof or comp._hash(proof) != review_hash:
        raise OperatorError("consent_changed")
    message = recipients.consent_message(proof)
    target = proof["recipient"] if human else proof["signing_wallet"]
    valid = _recover(message, signature) == target
    if not valid and human:
        try:
            valid = await wallet_proofs._rpc("eth_chainId", []) == "0x2105" and await wallet_proofs.verify_personal_signature(
                message=message,
                signature=signature,
                address=target,
            )
        except Exception:
            valid = False
    if not valid or (not human and len(signature) != 132):
        raise OperatorError("invalid_signature", 400)
    # Potentially slow contract-wallet RPC happens before acquiring node locks.
    async with await new_session() as session:
        await _begin(session)
        row, _ = await _request(session, request_id, account_id, wallet, human=human, lock=True)
        _fresh(row)
        if row["consent"] != proof:
            raise OperatorError("consent_changed")
        field = "recipient_signature" if human else "node_signature"
        expected = "awaiting_wallet" if human else "awaiting_node"
        if row["status"] != expected:
            if row[field] == signature:
                return _view(row)
            raise OperatorError("consent_already_signed")
        values = {field: signature, "status": "awaiting_node" if human else "review_required"}
        await session.execute(sa.update(requests).where(requests.c.id == request_id).values(**values))
        await session.commit()
        return _view({**row, **values})


async def export_for_review(request_id, approval_ref):
    """Private CLI only: collect the exact proof for existing maintainer review."""
    async with await new_session() as session:
        await comp._transaction(session, False)
        row = (await session.execute(sa.select(requests).where(requests.c.id == request_id))).mappings().first()
        if not row or row["status"] != "review_required":
            raise OperatorError("signed_request_required")
        _fresh(row)
        proof = {
            "consent": copy.deepcopy(row["consent"]),
            "node_signature": row["node_signature"],
            "recipient_signature": row["recipient_signature"],
            "approval_ref": approval_ref,
        }
    await recipients.bind_recipient(proof)
    return proof

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Two-party, non-economic association of an existing node and human account.

The node keeps its account, keys and wallet. An association allows only private
account visibility; it is not a login identity, recovery key or independence
review. A single replaceable pairing slot bounds storage per registered node.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import sqlalchemy as sa
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_keys.exceptions import BadSignature

from ..config import get_settings
from ..database import new_session
from ..v2.schema import account_aliases, accounts, validators
from ..v2.schema import validator_account_links as links
from ..v2.schema import validator_pairings as pairings

TTL_SECONDS = 600
PURPOSE = "aipg.validator.account-link.v1"
_PAIR_ID = re.compile(r"^vpa_[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^(?:0x)?[0-9a-fA-F]{130}$")


class PairingError(ValueError):
    status_code = 400


class PairingForbidden(PairingError):
    status_code = 403


class PairingNotFound(PairingError):
    status_code = 404


class PairingConflict(PairingError):
    status_code = 409


def _uuid(value) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise PairingForbidden("Invalid account identity") from exc


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _now(session) -> datetime:
    # Evaluate after acquiring locks; a transaction may have waited past expiry.
    if session.bind.dialect.name == "postgresql":
        return await session.scalar(sa.select(sa.func.clock_timestamp()))
    return datetime.now(UTC)


def _settings() -> tuple[str, str]:
    settings = get_settings()
    values = (settings.validator_pairing_audience, settings.validator_pairing_console_url)
    for value in values:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or len(value) > 255
        ):
            raise PairingError("Validator pairing HTTPS URLs are not configured correctly")
    return values[0].rstrip("/"), values[1].rstrip("/")


async def _canonical_account(session, account_id) -> UUID:
    aid = _uuid(account_id)
    exists = await session.scalar(sa.select(accounts.c.id).where(accounts.c.id == aid))
    retired = await session.scalar(
        sa.select(account_aliases.c.source_account_id).where(account_aliases.c.source_account_id == aid),
    )
    if not exists or retired:
        raise PairingForbidden("Account changed; sign in and start pairing again")
    return aid


async def _node(session, *, account_id=None, wallet=None, validator_id=None):
    query = sa.select(validators)
    if validator_id is not None:
        query = query.where(validators.c.id == validator_id)
    else:
        query = query.where(validators.c.account_id == _uuid(account_id))
    row = (await session.execute(query.with_for_update())).mappings().first()
    if not row or row["status"] not in {"active", "suspended"}:
        raise PairingForbidden("An active or self-suspended registered validator is required")
    if account_id is not None and (row["account_id"] != _uuid(account_id) or row["signing_wallet"] != (wallet or "").lower()):
        raise PairingForbidden("Validator identity changed; register the current signer first")
    await _canonical_account(session, row["account_id"])
    return row


async def _slot(session, node):
    return (
        (
            await session.execute(
                sa.select(pairings).where(pairings.c.validator_id == node["id"]),
            )
        )
        .mappings()
        .first()
    )


async def _live_link(session, node):
    row = (
        (
            await session.execute(
                sa.select(links).where(
                    links.c.validator_id == node["id"],
                    links.c.revoked_at.is_(None),
                    links.c.node_account_id == node["account_id"],
                    links.c.signing_wallet == node["signing_wallet"],
                    ~sa.exists(
                        sa.select(account_aliases.c.source_account_id).where(
                            account_aliases.c.source_account_id == links.c.operator_account_id,
                        ),
                    ),
                ),
            )
        )
        .mappings()
        .first()
    )
    return row


async def _by_id(session, pairing_id):
    if not _PAIR_ID.fullmatch(pairing_id):
        raise PairingNotFound("Pairing not found")
    # This first read only locates the lock. Re-read the slot after locking.
    vid = await session.scalar(sa.select(pairings.c.validator_id).where(pairings.c.id == pairing_id))
    if not vid:
        raise PairingNotFound("Pairing not found")
    node = await _node(session, validator_id=vid)
    row = await _slot(session, node)
    if not row or row["id"] != pairing_id:
        raise PairingNotFound("Pairing was replaced; start again")
    return node, row


def _valid(row, node, now):
    if row["status"] == "cancelled" or _aware(row["expires_at"]) <= now:
        raise PairingConflict("Pairing expired or was cancelled; start again")
    if row["node_account_id"] != node["account_id"] or row["signing_wallet"] != node["signing_wallet"]:
        raise PairingConflict("Node identity changed; start pairing again")
    if row["audience"] != _settings()[0]:
        raise PairingConflict("Pairing audience changed; start again")


def _payload(row) -> dict:
    return {
        "purpose": PURPOSE,
        "audience": row["audience"],
        "pairing_id": row["id"],
        "validator_id": row["validator_id"],
        "node_account_id": str(row["node_account_id"]),
        "operator_account_id": str(row["operator_account_id"]),
        "signing_wallet": row["signing_wallet"],
        "comparison_code": row["comparison_code"],
        "expires_at": int(_aware(row["expires_at"]).timestamp()),
        "permissions": ["validator.account_visibility"],
    }


def _verify_signature(payload, signature, wallet):
    if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
        raise PairingForbidden("A node signature over this exact pairing is required")
    try:
        recovered = Account.recover_message(
            encode_defunct(text=json.dumps(payload, sort_keys=True, separators=(",", ":"))),
            signature=signature,
        )
    except (ValueError, TypeError, OverflowError, BadSignature) as exc:
        raise PairingForbidden("Pairing signature is invalid") from exc
    if recovered.lower() != wallet:
        raise PairingForbidden("Pairing signature does not match the registered node")


def _view(row, now, *, include_approval=False) -> dict:
    status = row["status"]
    if status in {"pending", "approved"} and _aware(row["expires_at"]) <= now:
        status = "expired"
    result = {
        "pairing_id": row["id"],
        "validator_id": row["validator_id"],
        "signing_wallet": row["signing_wallet"],
        "status": status,
        "expires_at": int(_aware(row["expires_at"]).timestamp()),
        "economic_effect": "none",
    }
    if include_approval and status in {"approved", "linked"}:
        result.update(comparison_code=row["comparison_code"], payload=_payload(row))
    return result


async def create(*, account_id, wallet) -> dict:
    audience, console = _settings()
    async with await new_session() as session, session.begin():
        node = await _node(session, account_id=account_id, wallet=wallet)
        now = await _now(session)
        if await _live_link(session, node):
            raise PairingConflict("Node is already associated; remove that association first")
        row = await _slot(session, node)
        reusable = bool(
            row
            and row["status"] in {"pending", "approved"}
            and _aware(row["expires_at"]) > now
            and row["node_account_id"] == node["account_id"]
            and row["signing_wallet"] == node["signing_wallet"]
            and row["audience"] == audience,
        )
        if not reusable:
            values = dict(
                validator_id=node["id"],
                id="vpa_" + secrets.token_hex(32),
                node_account_id=node["account_id"],
                signing_wallet=node["signing_wallet"],
                operator_account_id=None,
                audience=audience,
                status="pending",
                comparison_code=secrets.token_hex(4).upper(),
                created=now,
                expires_at=now.replace(microsecond=0) + timedelta(seconds=TTL_SECONDS),
            )
            if row:
                await session.execute(sa.update(pairings).where(pairings.c.validator_id == node["id"]).values(**values))
            else:
                await session.execute(sa.insert(pairings).values(**values))
            row = values
        result = _view(row, now)
        result["approval_url"] = f"{console}/{row['id']}"
        return result


async def inspect(*, pairing_id, operator_account_id) -> dict:
    async with await new_session() as session, session.begin():
        aid = await _canonical_account(session, operator_account_id)
        node, row = await _by_id(session, pairing_id)
        _valid(row, node, await _now(session))
        if row["operator_account_id"] and row["operator_account_id"] != aid:
            raise PairingConflict("Another account approved this pairing; cancel it on the node")
        return _view(row, await _now(session), include_approval=True)


async def approve(*, pairing_id, operator_account_id) -> dict:
    async with await new_session() as session, session.begin():
        aid = await _canonical_account(session, operator_account_id)
        node, row = await _by_id(session, pairing_id)
        now = await _now(session)
        _valid(row, node, now)
        if aid == node["account_id"]:
            raise PairingConflict("This is the node account; choose your separate personal account")
        if row["operator_account_id"] and row["operator_account_id"] != aid:
            raise PairingConflict("Another account approved this pairing; cancel it on the node")
        if row["status"] == "pending":
            updated = (
                (
                    await session.execute(
                        sa.update(pairings)
                        .where(pairings.c.id == pairing_id, pairings.c.status == "pending")
                        .values(operator_account_id=aid, status="approved")
                        .returning(pairings),
                    )
                )
                .mappings()
                .first()
            )
            if not updated:
                raise PairingConflict("Pairing changed; reload before approving")
            row = updated
        return _view(row, now, include_approval=True)


async def poll(*, account_id, wallet) -> dict:
    async with await new_session() as session, session.begin():
        node = await _node(session, account_id=account_id, wallet=wallet)
        row = await _slot(session, node)
        if not row:
            return {"status": "none"}
        if row["status"] not in {"cancelled"} and _aware(row["expires_at"]) > await _now(session):
            _valid(row, node, await _now(session))
        return _view(row, await _now(session), include_approval=True)


async def confirm(*, pairing_id, account_id, wallet, signature) -> dict:
    async with await new_session() as session, session.begin():
        node = await _node(session, account_id=account_id, wallet=wallet)
        row = await _slot(session, node)
        if not row or row["id"] != pairing_id:
            raise PairingNotFound("Pairing not found")
        now = await _now(session)
        _valid(row, node, now)
        if row["status"] not in {"approved", "linked"}:
            raise PairingConflict("Approve in your account before confirming on the node")
        await _canonical_account(session, row["operator_account_id"])
        payload = _payload(row)
        _verify_signature(payload, signature, node["signing_wallet"])
        existing = (await session.execute(sa.select(links).where(links.c.validator_id == node["id"]))).mappings().first()
        if row["status"] == "linked":
            if not existing or existing["revoked_at"] or existing["pairing_id"] != pairing_id:
                raise PairingConflict("Association changed; start pairing again")
            return _view(row, now)
        if await _live_link(session, node):
            raise PairingConflict("Node is already associated; remove that association first")
        won = await session.scalar(
            sa.update(pairings)
            .where(pairings.c.id == pairing_id, pairings.c.status == "approved")
            .values(status="linked")
            .returning(pairings.c.id),
        )
        if not won:
            raise PairingConflict("Pairing changed; retry from its current state")
        values = dict(
            validator_id=node["id"],
            operator_account_id=row["operator_account_id"],
            node_account_id=node["account_id"],
            signing_wallet=node["signing_wallet"],
            pairing_id=pairing_id,
            payload=payload,
            signature=signature,
            linked_at=now,
            revoked_at=None,
        )
        if existing:
            await session.execute(sa.update(links).where(links.c.validator_id == node["id"]).values(**values))
        else:
            await session.execute(sa.insert(links).values(**values))
        return _view({**row, "status": "linked"}, now)


async def cancel(*, pairing_id, account_id, wallet) -> dict:
    async with await new_session() as session, session.begin():
        node = await _node(session, account_id=account_id, wallet=wallet)
        row = await _slot(session, node)
        if not row or row["id"] != pairing_id:
            raise PairingNotFound("Pairing not found")
        if row["status"] == "linked":
            raise PairingConflict("Pairing is complete; remove the association from your account")
        await session.execute(sa.update(pairings).where(pairings.c.id == pairing_id).values(status="cancelled"))
        return {"status": "cancelled"}


async def list_for_account(*, operator_account_id) -> dict:
    async with await new_session() as session:
        aid = await _canonical_account(session, operator_account_id)
        rows = (
            (
                await session.execute(
                    sa.select(
                        links.c.validator_id,
                        links.c.pairing_id,
                        links.c.signing_wallet,
                        links.c.linked_at,
                        validators.c.status,
                        validators.c.last_heartbeat,
                        validators.c.software_version,
                    )
                    .join(validators, validators.c.id == links.c.validator_id)
                    .where(
                        links.c.operator_account_id == aid,
                        links.c.revoked_at.is_(None),
                        links.c.signing_wallet == validators.c.signing_wallet,
                        links.c.node_account_id == validators.c.account_id,
                        ~sa.exists(
                            sa.select(account_aliases.c.source_account_id).where(
                                account_aliases.c.source_account_id == links.c.node_account_id,
                            ),
                        ),
                    )
                    .order_by(links.c.linked_at.desc())
                    .limit(100),
                )
            )
            .mappings()
            .all()
        )
        return {
            "nodes": [
                {**row, "linked_at": _aware(row["linked_at"]), "last_heartbeat": _aware(row["last_heartbeat"])}
                for row in rows
            ],
            "economic_effect": "none",
        }


async def unlink(*, validator_id, operator_account_id, pairing_id) -> dict:
    async with await new_session() as session, session.begin():
        aid = await _canonical_account(session, operator_account_id)
        # Do not require an active node to remove visibility after revocation.
        await session.execute(sa.select(validators.c.id).where(validators.c.id == validator_id).with_for_update())
        row = (
            (
                await session.execute(
                    sa.select(links).where(
                        links.c.validator_id == validator_id,
                        links.c.operator_account_id == aid,
                        links.c.pairing_id == pairing_id,
                    ),
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise PairingNotFound("Association not found or has changed")
        if not row["revoked_at"]:
            await session.execute(sa.update(links).where(links.c.validator_id == validator_id).values(revoked_at=await _now(session)))
            await session.execute(sa.update(pairings).where(pairings.c.id == pairing_id).values(status="cancelled"))
        return {"status": "unlinked", "validator_id": validator_id}


def _unlink_payload(link, issued_at):
    return {
        "purpose": "aipg.validator.account-unlink.v1",
        "audience": _settings()[0],
        "validator_id": link["validator_id"],
        "pairing_id": link["pairing_id"],
        "node_account_id": str(link["node_account_id"]),
        "operator_account_id": str(link["operator_account_id"]),
        "signing_wallet": link["signing_wallet"],
        "issued_at": issued_at,
        "expires_at": issued_at + TTL_SECONDS,
    }


async def node_link(*, account_id, wallet) -> dict:
    async with await new_session() as session, session.begin():
        node = await _node(session, account_id=account_id, wallet=wallet)
        link = await _live_link(session, node)
        if not link:
            return {"status": "none"}
        return {
            "status": "linked",
            "validator_id": node["id"],
            "operator_account_id": str(link["operator_account_id"]),
            "unlink_payload": _unlink_payload(link, int((await _now(session)).timestamp())),
            "economic_effect": "none",
        }


async def unlink_from_node(*, account_id, wallet, pairing_id, issued_at, signature) -> dict:
    async with await new_session() as session, session.begin():
        node = await _node(session, account_id=account_id, wallet=wallet)
        link = (
            (
                await session.execute(
                    sa.select(links).where(
                        links.c.validator_id == node["id"],
                        links.c.pairing_id == pairing_id,
                        links.c.node_account_id == node["account_id"],
                        links.c.signing_wallet == node["signing_wallet"],
                    ),
                )
            )
            .mappings()
            .first()
        )
        if not link:
            raise PairingNotFound("Association not found or has changed")
        now = await _now(session)
        age = int(now.timestamp()) - issued_at
        if age < 0 or age >= TTL_SECONDS:
            raise PairingConflict("Unlink proof expired; refresh the association first")
        _verify_signature(_unlink_payload(link, issued_at), signature, node["signing_wallet"])
        if not link["revoked_at"]:
            await session.execute(sa.update(links).where(links.c.validator_id == node["id"]).values(revoked_at=now))
            await session.execute(sa.update(pairings).where(pairings.c.id == pairing_id).values(status="cancelled"))
        return {"status": "unlinked", "validator_id": node["id"]}

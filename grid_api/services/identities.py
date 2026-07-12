# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Canonical account identities and proof-authorized account merges."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..database import new_session
from ..v2.schema import (
    account_aliases,
    account_identities,
    accounts,
    api_keys,
    credit_ledger,
    credits,
    identity_events,
    promo_grants,
    promo_spends,
    reservations,
    workers,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(value) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def canonical_subject(kind: str, subject: str) -> str:
    kind = (kind or "").strip().lower()
    value = (subject or "").strip()
    if kind in {"wallet", "email"}:
        value = value.lower()
    if kind in {"google", "github"} and value.lower().startswith(f"{kind}_"):
        value = value[len(kind) + 1:]
    if kind not in {"wallet", "google", "github", "email", "app"} or not value:
        raise ValueError("unsupported or empty identity")
    return value


def subject_hash(kind: str, subject: str) -> str:
    canonical = canonical_subject(kind, subject)
    return hashlib.sha256(f"{kind}:{canonical}".encode()).hexdigest()


async def canonical_account_id(account_id, *, session=None) -> UUID:
    """Resolve a retired account alias, rejecting cycles/corrupt chains."""
    current = _uuid(account_id)
    owns_session = session is None
    if owns_session:
        session = await new_session()
    try:
        seen: set[UUID] = set()
        for _ in range(8):
            if current in seen:
                raise RuntimeError("account alias cycle detected")
            seen.add(current)
            nxt = await session.scalar(
                sa.select(account_aliases.c.canonical_account_id)
                .where(account_aliases.c.source_account_id == current)
            )
            if not nxt:
                return current
            current = _uuid(nxt)
        raise RuntimeError("account alias chain exceeds safety limit")
    finally:
        if owns_session:
            await session.close()


async def account_family_ids(account_id, *, session=None) -> set[UUID]:
    """Return the canonical account and every retired alias beneath it."""
    owns_session = session is None
    if owns_session:
        session = await new_session()
    try:
        canonical = await canonical_account_id(account_id, session=session)
        family = {canonical}
        frontier = {canonical}
        for _ in range(16):
            rows = (await session.execute(
                sa.select(account_aliases.c.source_account_id).where(
                    account_aliases.c.canonical_account_id.in_(frontier)
                )
            )).scalars().all()
            discovered = {_uuid(row) for row in rows} - family
            if not discovered:
                return family
            family.update(discovered)
            frontier = discovered
        raise RuntimeError("account alias family exceeds safety limit")
    finally:
        if owns_session:
            await session.close()


async def resolve_identity(kind: str, subject: str) -> UUID | None:
    digest = subject_hash(kind, subject)
    async with await new_session() as session:
        owner = await session.scalar(
            sa.select(account_identities.c.account_id).where(
                account_identities.c.kind == kind,
                account_identities.c.subject_hash == digest,
                account_identities.c.verified_at.is_not(None),
            )
        )
        return await canonical_account_id(owner, session=session) if owner else None


async def list_identities(account_id) -> list[dict]:
    aid = await canonical_account_id(account_id)
    async with await new_session() as session:
        rows = (await session.execute(
            sa.select(
                account_identities.c.id, account_identities.c.kind,
                account_identities.c.display_hint, account_identities.c.is_primary,
                account_identities.c.verified_at, account_identities.c.created,
            ).where(account_identities.c.account_id == aid)
            .order_by(account_identities.c.created)
        )).mappings().all()
    return [dict(row) for row in rows]


async def attach_identity(account_id, kind: str, subject: str, *, display_hint: str | None = None,
                          metadata: dict | None = None, make_primary: bool = True,
                          ref: str | None = None) -> dict:
    """Attach a newly proved identity, or report the other owning account."""
    aid = await canonical_account_id(account_id)
    canonical = canonical_subject(kind, subject)
    digest = subject_hash(kind, canonical)
    now = _now()
    ref = ref or f"identity-link:{kind}:{digest}:{aid}"
    async with await new_session() as session:
        existing = (await session.execute(
            sa.select(account_identities.c.id, account_identities.c.account_id)
            .where(account_identities.c.kind == kind, account_identities.c.subject_hash == digest)
            .with_for_update()
        )).first()
        if existing:
            owner = await canonical_account_id(existing[1], session=session)
            if owner != aid:
                return {"status": "conflict", "account_id": str(owner), "subject_hash": digest}
            await session.execute(
                sa.update(account_identities).where(account_identities.c.id == existing[0])
                .values(last_used=now, verified_at=now)
            )
            await session.commit()
            return {"status": "already", "account_id": str(aid), "subject_hash": digest}

        if make_primary:
            await session.execute(
                sa.update(account_identities)
                .where(account_identities.c.account_id == aid, account_identities.c.kind == kind)
                .values(is_primary=False)
            )
        try:
            await session.execute(sa.insert(account_identities).values(
                id=uuid4(), account_id=aid, kind=kind, subject_hash=digest,
                display_hint=display_hint, metadata=metadata or {}, verified_at=now,
                is_primary=make_primary, created=now,
            ))
        except IntegrityError:
            await session.rollback()
            owner = await session.scalar(sa.select(account_identities.c.account_id).where(
                account_identities.c.kind == kind,
                account_identities.c.subject_hash == digest,
            ))
            if owner:
                owner = await canonical_account_id(owner, session=session)
                return {"status": "already" if owner == aid else "conflict",
                        "account_id": str(owner), "subject_hash": digest}
            raise
        legacy_values = {}
        if make_primary and kind == "wallet":
            legacy_values["wallet"] = canonical
        elif make_primary and kind == "google":
            legacy_values["oauth_sub"] = canonical
        elif make_primary and kind == "email":
            legacy_values["email"] = canonical
        if legacy_values:
            await session.execute(sa.update(accounts).where(accounts.c.id == aid).values(**legacy_values))
        await session.execute(sa.insert(identity_events).values(
            account_id=aid, actor_account_id=aid, event_type="identity_linked",
            identity_kind=kind, subject_hash=digest,
            event_metadata={"primary": make_primary}, ref=ref, created=now,
        ))
        await session.commit()
    return {"status": "linked", "account_id": str(aid), "subject_hash": digest}


async def merge_accounts(destination_account_id, source_account_id, *, reason: str = "identity_link",
                         merge_ref: str | None = None) -> dict:
    """Merge source into destination after the caller proved both identities.

    Historical job/payout ledgers stay untouched. Purchased credit moves through
    paired ledger entries; duplicate campaign grants collapse to the larger
    remaining amount, never sum. Source keys are revoked and its login resolves
    through grid_account_aliases afterward.
    """
    destination = await canonical_account_id(destination_account_id)
    source = await canonical_account_id(source_account_id)
    if destination == source:
        return {"status": "already", "account_id": str(destination)}
    merge_ref = merge_ref or f"merge:{uuid4()}"
    now = _now()

    async with await new_session() as session:
        ordered = sorted((destination, source), key=str)
        locked = (await session.execute(
            sa.select(accounts).where(accounts.c.id.in_(ordered)).order_by(accounts.c.id).with_for_update()
        )).mappings().all()
        if len(locked) != 2:
            raise ValueError("both accounts must exist")
        by_id = {_uuid(row["id"]): row for row in locked}
        dest_row, source_row = by_id[destination], by_id[source]

        held_reservations = await session.scalar(
            sa.select(sa.func.count()).select_from(reservations).where(
                reservations.c.account_id.in_([destination, source]),
                reservations.c.status == "held",
            )
        )
        held_promos = await session.scalar(
            sa.select(sa.func.count()).select_from(promo_spends).where(
                promo_spends.c.account_id.in_([destination, source]),
                promo_spends.c.status == "held",
            )
        )
        if held_reservations or held_promos:
            raise ValueError("finish in-flight jobs before linking these accounts")

        # Clear source legacy unique columns before promoting any missing values.
        source_payout = source_row["payout_wallet"] or source_row["wallet"]
        await session.execute(
            sa.update(accounts).where(accounts.c.id == source)
            .values(wallet=None, email=None, oauth_sub=None, payout_wallet=source_payout)
        )
        promote = {}
        for field in ("wallet", "email", "oauth_sub"):
            if not dest_row[field] and source_row[field]:
                promote[field] = source_row[field]
        if not dest_row["payout_wallet"] and source_payout:
            promote["payout_wallet"] = source_payout
        if promote:
            await session.execute(sa.update(accounts).where(accounts.c.id == destination).values(**promote))

        source_balance = int((await session.scalar(
            sa.select(credits.c.balance_micro).where(credits.c.account_id == source).with_for_update()
        )) or 0)
        if source_balance > 0:
            dest_balance = await session.scalar(
                sa.select(credits.c.balance_micro).where(credits.c.account_id == destination).with_for_update()
            )
            if dest_balance is None:
                await session.execute(sa.insert(credits).values(
                    account_id=destination, balance_micro=source_balance, updated=now,
                ))
            else:
                await session.execute(
                    sa.update(credits).where(credits.c.account_id == destination)
                    .values(balance_micro=credits.c.balance_micro + source_balance, updated=now)
                )
            await session.execute(
                sa.update(credits).where(credits.c.account_id == source)
                .values(balance_micro=0, updated=now)
            )
            await session.execute(sa.insert(credit_ledger), [
                {"account_id": source, "delta_micro": -source_balance,
                 "reason": "account:merge_out", "ref": f"{merge_ref}:out"},
                {"account_id": destination, "delta_micro": source_balance,
                 "reason": "account:merge_in", "ref": f"{merge_ref}:in"},
            ])

        source_grants = (await session.execute(
            sa.select(promo_grants).where(promo_grants.c.account_id == source).with_for_update()
        )).mappings().all()
        for grant in source_grants:
            dest_grant = (await session.execute(
                sa.select(promo_grants).where(
                    promo_grants.c.account_id == destination,
                    promo_grants.c.campaign_id == grant["campaign_id"],
                ).with_for_update()
            )).mappings().first()
            if dest_grant:
                await session.execute(
                    sa.update(promo_grants).where(promo_grants.c.id == dest_grant["id"])
                    .values(
                        amount_micro=max(int(dest_grant["amount_micro"]), int(grant["amount_micro"])),
                        remaining_micro=max(int(dest_grant["remaining_micro"]), int(grant["remaining_micro"])),
                        updated=now,
                    )
                )
                await session.execute(
                    sa.update(promo_grants).where(promo_grants.c.id == grant["id"])
                    .values(remaining_micro=0, status="merged", updated=now)
                )
            else:
                await session.execute(
                    sa.update(promo_grants).where(promo_grants.c.id == grant["id"])
                    .values(account_id=destination, updated=now)
                )

        await session.execute(
            sa.update(account_identities).where(account_identities.c.account_id == source)
            .values(account_id=destination, is_primary=False)
        )
        await session.execute(
            sa.update(api_keys).where(api_keys.c.account_id == source).values(revoked=True)
        )
        await session.execute(
            sa.update(workers).where(workers.c.account_id == source).values(account_id=destination)
        )
        await session.execute(sa.insert(account_aliases).values(
            source_account_id=source, canonical_account_id=destination,
            merge_ref=merge_ref, reason=reason, created=now,
        ))
        await session.execute(sa.insert(identity_events).values(
            account_id=destination, actor_account_id=destination,
            event_type="accounts_merged", identity_kind=None, subject_hash=None,
            event_metadata={"source_account_id": str(source), "reason": reason},
            ref=f"{merge_ref}:event", created=now,
        ))
        await session.commit()
    return {"status": "merged", "account_id": str(destination), "source_account_id": str(source)}

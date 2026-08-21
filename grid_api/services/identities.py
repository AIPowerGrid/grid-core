# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Canonical account identities and proof-authorized account merges."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..auth import _get_api_key_salt
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


def _normalized_kind(kind: str) -> str:
    normalized = (kind or "").strip().lower()
    if normalized not in {"wallet", "google", "github", "email", "app"}:
        raise ValueError("unsupported or empty identity")
    return normalized


def legacy_subject_hash(kind: str, subject: str) -> str:
    """Return the pre-v2 unkeyed digest for lookup migration only."""
    normalized_kind = _normalized_kind(kind)
    canonical = canonical_subject(normalized_kind, subject)
    return hashlib.sha256(f"{normalized_kind}:{canonical}".encode()).hexdigest()


def subject_hash(kind: str, subject: str) -> str:
    """Return the server-keyed canonical identity lookup digest."""
    normalized_kind = _normalized_kind(kind)
    canonical = canonical_subject(normalized_kind, subject)
    return hashlib.blake2b(
        f"{normalized_kind}:{canonical}".encode("utf-8"),
        key=_get_api_key_salt().encode("utf-8"),
        digest_size=32,
        person=b"aipg-ident-v1",
    ).hexdigest()


def _subject_hash_candidates(kind: str, subject: str) -> tuple[str, ...]:
    current = subject_hash(kind, subject)
    legacy = legacy_subject_hash(kind, subject)
    return (current,) if current == legacy else (current, legacy)


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


async def _canonical_owners(session, account_ids) -> set[UUID]:
    owners: set[UUID] = set()
    for account_id in account_ids:
        owners.add(await canonical_account_id(account_id, session=session))
    return owners


async def resolve_identity(kind: str, subject: str) -> UUID | None:
    kind = _normalized_kind(kind)
    digest = subject_hash(kind, subject)
    candidates = _subject_hash_candidates(kind, subject)
    async with await new_session() as session:
        rows = (
            await session.execute(
                sa.select(
                    account_identities.c.id,
                    account_identities.c.account_id,
                    account_identities.c.subject_hash,
                ).where(
                    account_identities.c.kind == kind,
                    account_identities.c.subject_hash.in_(candidates),
                    account_identities.c.verified_at.is_not(None),
                ).order_by(
                    sa.case((account_identities.c.subject_hash == digest, 0), else_=1),
                ).with_for_update()
            )
        ).all()
        if not rows:
            return None
        owners = await _canonical_owners(session, (row[1] for row in rows))
        if len(owners) != 1:
            raise RuntimeError("identity hash variants resolve to multiple accounts")
        owner = next(iter(owners))
        row = rows[0]
        if row[2] != digest:
            try:
                await session.execute(
                    sa.update(account_identities)
                    .where(account_identities.c.id == row[0])
                    .values(subject_hash=digest, last_used=_now())
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                current_owner = await session.scalar(
                    sa.select(account_identities.c.account_id).where(
                        account_identities.c.kind == kind,
                        account_identities.c.subject_hash == digest,
                        account_identities.c.verified_at.is_not(None),
                    )
                )
                if not current_owner:
                    raise RuntimeError("identity hash migration conflicted without an owner")
                if await canonical_account_id(current_owner, session=session) != owner:
                    raise RuntimeError("identity hash migration resolved to multiple accounts")
        return owner


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


async def verified_wallet_addresses(account_id) -> list[str]:
    """Return wallet addresses whose stored hint matches their verified identity hash."""
    aid = await canonical_account_id(account_id)
    async with await new_session() as session:
        rows = (await session.execute(
            sa.select(
                account_identities.c.display_hint,
                account_identities.c.subject_hash,
                account_identities.c.is_primary,
                account_identities.c.created,
            ).where(
                account_identities.c.account_id == aid,
                account_identities.c.kind == "wallet",
                account_identities.c.verified_at.is_not(None),
            ).order_by(
                account_identities.c.is_primary.desc(),
                account_identities.c.created,
            )
        )).mappings().all()

    wallets: list[str] = []
    for row in rows:
        try:
            wallet = canonical_subject("wallet", row["display_hint"] or "")
        except ValueError:
            continue
        if (
            len(wallet) == 42
            and wallet.startswith("0x")
            and all(char in "0123456789abcdef" for char in wallet[2:])
            and row["subject_hash"] in _subject_hash_candidates("wallet", wallet)
            and wallet not in wallets
        ):
            wallets.append(wallet)
    return wallets


async def attach_identity(account_id, kind: str, subject: str, *, display_hint: str | None = None,
                          metadata: dict | None = None, make_primary: bool = True,
                          ref: str | None = None) -> dict:
    """Attach a newly proved identity, or report the other owning account."""
    aid = await canonical_account_id(account_id)
    kind = _normalized_kind(kind)
    canonical = canonical_subject(kind, subject)
    digest = subject_hash(kind, canonical)
    candidates = _subject_hash_candidates(kind, canonical)
    now = _now()
    ref = ref or f"identity-link:{kind}:{digest}:{aid}"
    async with await new_session() as session:
        existing_rows = (await session.execute(
            sa.select(account_identities.c.id, account_identities.c.account_id)
            .where(
                account_identities.c.kind == kind,
                account_identities.c.subject_hash.in_(candidates),
            )
            .order_by(sa.case((account_identities.c.subject_hash == digest, 0), else_=1))
            .with_for_update()
        )).all()
        if existing_rows:
            owners = await _canonical_owners(session, (row[1] for row in existing_rows))
            if len(owners) != 1:
                raise RuntimeError("identity hash variants resolve to multiple accounts")
            owner = next(iter(owners))
            if owner != aid:
                return {"status": "conflict", "account_id": str(owner), "subject_hash": digest}
            existing = existing_rows[0]
            try:
                await session.execute(
                    sa.update(account_identities).where(account_identities.c.id == existing[0])
                    .values(subject_hash=digest, last_used=now, verified_at=now)
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                current_owner = await session.scalar(
                    sa.select(account_identities.c.account_id).where(
                        account_identities.c.kind == kind,
                        account_identities.c.subject_hash == digest,
                    )
                )
                if not current_owner:
                    raise RuntimeError("identity hash migration conflicted without an owner")
                if await canonical_account_id(current_owner, session=session) != aid:
                    raise RuntimeError("identity hash migration resolved to multiple accounts")
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
            owner_rows = (
                await session.execute(
                    sa.select(account_identities.c.account_id).where(
                        account_identities.c.kind == kind,
                        account_identities.c.subject_hash.in_(candidates),
                    ).order_by(sa.case((account_identities.c.subject_hash == digest, 0), else_=1))
                )
            ).scalars().all()
            if owner_rows:
                owners = await _canonical_owners(session, owner_rows)
                if len(owners) != 1:
                    raise RuntimeError("identity hash variants resolve to multiple accounts")
                owner = next(iter(owners))
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
        # Another merge may have completed while this call waited on the locks.
        # Re-resolve under those locks so opposing concurrent merges cannot create
        # A→B and B→A aliases.
        destination = await canonical_account_id(destination, session=session)
        source = await canonical_account_id(source, session=session)
        if destination == source:
            await session.rollback()
            return {"status": "already", "account_id": str(destination)}
        if destination not in by_id or source not in by_id:
            await session.rollback()
            raise ValueError("account changed during merge; retry")
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

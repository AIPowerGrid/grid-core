# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Maintainer-reviewed common-control grouping for media workers.

The records are private anti-Sybil evidence. They do not affect routing,
rewards, payouts, or slashing; media validation uses them only to refuse a
candidate/reference set that is not demonstrably split across operators.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from web3 import Web3

from ..database import new_session
from ..v2.schema import worker_control_reviews as controls_t
from ..v2.schema import workers as workers_t

DEFAULT_REVIEW_DAYS = 30
MAX_REVIEW_DAYS = 90
VALID_ACTIONS = frozenset({"verify", "reject", "revoke"})
GROUP_RE = re.compile(r"^opg_[A-Za-z0-9_-]{8,88}$")
_REVIEW_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")


class WorkerControlReviewError(ValueError):
    """Raised when a worker-control transition is invalid or stale."""


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        aware = _aware(value)
        return aware.isoformat() if aware else None
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _digest(
    row: dict[str, Any] | None,
    *,
    worker_id: UUID,
    worker_account_id: UUID,
    worker_wallet: str,
) -> str:
    state = {
        "worker_id": worker_id,
        "worker_account_id": worker_account_id,
        "worker_wallet": worker_wallet,
        "review": "missing" if row is None else {
            key: row.get(key)
            for key in (
                "account_id",
                "payout_wallet",
                "operator_group_id",
                "status",
                "reviewed_at",
                "expires_at",
                "review_ref",
                "updated",
            )
        },
    }
    encoded = json.dumps(
        state,
        default=_canonical_value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def fresh_review_reasons(
    row: dict[str, Any] | None,
    *,
    worker_account_id: UUID,
    worker_wallet: str,
    now: datetime,
) -> list[str]:
    """Return fail-closed reasons a control review is not currently usable."""
    if row is None:
        return ["worker control review is missing"]
    reasons: list[str] = []
    current = _aware(now)
    reviewed_at = _aware(row.get("reviewed_at"))
    expires_at = _aware(row.get("expires_at"))
    group_id = str(row.get("operator_group_id") or "")
    wallet = worker_wallet.strip().lower()
    if row.get("status") != "verified":
        reasons.append("worker control review is not verified")
    if not GROUP_RE.fullmatch(group_id):
        reasons.append("worker control group is missing or invalid")
    if row.get("account_id") != worker_account_id:
        reasons.append("worker account differs from control review")
    if str(row.get("payout_wallet") or "").strip().lower() != wallet:
        reasons.append("worker payout wallet differs from control review")
    if reviewed_at is None or reviewed_at > current:
        reasons.append("worker control review timestamp is invalid")
    if expires_at is None or expires_at < current:
        reasons.append("worker control review is expired")
    elif reviewed_at is not None and expires_at < reviewed_at:
        reasons.append("worker control review expiry is invalid")
    return reasons


async def review_worker_control(
    worker_id: str | UUID,
    *,
    action: str,
    operator_group_id: str | None = None,
    review_ref: str,
    review_days: int = DEFAULT_REVIEW_DAYS,
    expected_digest: str | None = None,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Preview or atomically apply one worker common-control review."""
    try:
        normalized_worker_id = worker_id if isinstance(worker_id, UUID) else UUID(str(worker_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkerControlReviewError("worker_id must be a UUID") from exc
    normalized_action = action.strip().lower()
    normalized_group = operator_group_id.strip() if operator_group_id else None
    if normalized_action not in VALID_ACTIONS:
        raise WorkerControlReviewError("action must be verify, reject, or revoke")
    if normalized_group is not None and not GROUP_RE.fullmatch(normalized_group):
        raise WorkerControlReviewError("operator_group_id must be an opaque opg_* identifier")
    if not _REVIEW_REF_RE.fullmatch(review_ref):
        raise WorkerControlReviewError("review_ref must be a non-sensitive opaque reference")
    if review_days < 1 or review_days > MAX_REVIEW_DAYS:
        raise WorkerControlReviewError(
            f"review_days must be between 1 and {MAX_REVIEW_DAYS}",
        )
    current = _aware(now or datetime.now(UTC))
    assert current is not None

    async with await new_session() as session:
        worker_query = sa.select(workers_t).where(workers_t.c.id == normalized_worker_id)
        if apply:
            worker_query = worker_query.with_for_update()
        worker = (await session.execute(worker_query)).mappings().one_or_none()
        if worker is None:
            raise WorkerControlReviewError("worker does not exist")
        account_id = worker["account_id"]
        wallet = str(worker["wallet"] or "").strip().lower()
        if account_id is None or not Web3.is_address(wallet):
            raise WorkerControlReviewError("worker account or payout wallet identity is incomplete")

        review_query = sa.select(controls_t).where(
            controls_t.c.worker_id == normalized_worker_id,
        )
        if apply:
            review_query = review_query.with_for_update()
        existing = (await session.execute(review_query)).mappings().one_or_none()
        row = dict(existing) if existing else None
        current_digest = _digest(
            row,
            worker_id=normalized_worker_id,
            worker_account_id=account_id,
            worker_wallet=wallet,
        )
        if apply and expected_digest != current_digest:
            raise WorkerControlReviewError("worker control state changed; preview again")
        if row and row["status"] == "revoked":
            raise WorkerControlReviewError("revoked worker control review cannot be changed")
        if normalized_action == "revoke" and row is None:
            raise WorkerControlReviewError("worker control review must exist before revocation")

        group_id = normalized_group or (str(row.get("operator_group_id")) if row and row.get("operator_group_id") else None)
        if normalized_action == "verify" and (
            group_id is None or not GROUP_RE.fullmatch(group_id)
        ):
            raise WorkerControlReviewError("verify requires operator_group_id")

        proposed_status = {
            "verify": "verified",
            "reject": "rejected",
            "revoke": "revoked",
        }[normalized_action]
        expires_at = current + timedelta(days=review_days) if normalized_action == "verify" else None
        identity_changed = bool(
            row
            and (
                row["account_id"] != account_id
                or str(row["payout_wallet"] or "").strip().lower() != wallet
            ),
        )
        result = {
            "worker_id": str(normalized_worker_id),
            "action": normalized_action,
            "apply": apply,
            "current_digest": current_digest,
            "current_status": row["status"] if row else "missing",
            "proposed_status": proposed_status,
            "operator_group_id": group_id,
            "review_ref": review_ref,
            "reviewed_at": current.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "identity_changed": identity_changed,
            "economic_effect": "none",
        }
        if not apply:
            return result

        values = {
            "account_id": account_id,
            "payout_wallet": wallet,
            "operator_group_id": group_id,
            "status": proposed_status,
            "reviewed_at": current,
            "expires_at": expires_at,
            "review_ref": review_ref,
            "updated": current,
        }
        if row is None:
            await session.execute(
                sa.insert(controls_t).values(
                    worker_id=normalized_worker_id,
                    created=current,
                    **values,
                ),
            )
        else:
            await session.execute(
                sa.update(controls_t)
                .where(
                    controls_t.c.worker_id == normalized_worker_id,
                    controls_t.c.status == row["status"],
                )
                .values(**values),
            )
        await session.commit()
        result["applied_at"] = current.isoformat()
        return result

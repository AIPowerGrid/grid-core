# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Maintainer-governed, non-economic media reference reviews."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from web3 import Web3

from ..config import GridSettings, get_settings
from ..database import new_session
from ..v2.schema import validator_reference_workers as references_t
from ..v2.schema import workers as workers_t
from .validator_bonds import reviewed_runtime_hash

VALID_MODALITIES = frozenset({"image", "video"})
VALID_ACTIONS = frozenset({"review", "activate", "pause", "revoke"})
QUALITY_MAX_AGE = timedelta(days=7)
BOND_MAX_AGE = timedelta(minutes=30)
_REVIEW_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}$")


class ReferenceReviewError(ValueError):
    """Raised when a reference review or transition is unsafe or stale."""


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _aware(value).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _digest(
    row: dict[str, Any] | None,
    *,
    worker_id: UUID,
    model: str,
    modality: str,
    worker_account_id: UUID,
    worker_wallet: str,
    policy: dict[str, Any],
) -> str:
    state: dict[str, Any] = {
        "worker_id": worker_id,
        "model": model,
        "modality": modality,
        "worker_account_id": worker_account_id,
        "worker_wallet": worker_wallet,
        "activation_policy": policy,
        "reference": "missing" if row is None else {
            key: row.get(key)
            for key in (
                "account_id",
                "payout_wallet",
                "status",
                "status_reason",
                "bond_contract",
                "bond_chain_id",
                "bond_finalized_block",
                "bond_finalized_block_hash",
                "bond_facet_address",
                "bond_facet_runtime_hash",
                "bond_amount_raw",
                "bond_active",
                "bond_slashed",
                "bond_verifier_version",
                "bond_status_reason",
                "bond_verified_at",
                "quality_window_start",
                "quality_window_end",
                "quality_pass_rate",
                "quality_reviewed_at",
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


def _activation_reasons(
    row: dict[str, Any],
    *,
    now: datetime,
    policy: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    quality_reviewed = _aware(row.get("quality_reviewed_at"))
    quality_start = _aware(row.get("quality_window_start"))
    quality_end = _aware(row.get("quality_window_end"))
    bond_verified = _aware(row.get("bond_verified_at"))
    if not quality_start or not quality_end or quality_end < quality_start or quality_end > now:
        reasons.append("quality window is missing or invalid")
    if not quality_reviewed or quality_reviewed < now - QUALITY_MAX_AGE:
        reasons.append("quality review is missing or stale")
    if float(row.get("quality_pass_rate") or 0) < policy["minimum_quality_pass_rate"]:
        reasons.append("quality pass rate is below policy")
    if not row.get("bond_active") or row.get("bond_slashed"):
        reasons.append("bond is inactive or slashed")
    if row.get("bond_status_reason") != "active":
        reasons.append("bond status is not active")
    if not bond_verified or bond_verified < now - BOND_MAX_AGE:
        reasons.append("bond proof is missing or stale")
    for field in (
        "bond_contract",
        "bond_chain_id",
        "bond_finalized_block",
        "bond_finalized_block_hash",
        "bond_facet_address",
        "bond_facet_runtime_hash",
        "bond_verifier_version",
    ):
        if row.get(field) is None:
            reasons.append(f"{field} proof is missing")
    if not policy["bond_contract"] or not Web3.is_address(policy["bond_contract"]):
        reasons.append("reviewed bond contract is not configured")
    elif str(row.get("bond_contract") or "").lower() != policy["bond_contract"]:
        reasons.append("bond contract does not match policy")
    if row.get("bond_chain_id") != policy["chain_id"]:
        reasons.append("bond chain does not match policy")
    if row.get("bond_verifier_version") != policy["verifier_version"]:
        reasons.append("bond verifier does not match policy")
    if not policy["facet_runtime_hash"]:
        reasons.append("bond verifier is not reviewed by this Core release")
    elif str(row.get("bond_facet_runtime_hash") or "").lower() != policy["facet_runtime_hash"]:
        reasons.append("bond facet runtime does not match policy")
    if int(row.get("bond_amount_raw") or 0) < policy["minimum_bond_raw"]:
        reasons.append("bond amount is below policy")
    return reasons


def _activation_policy(settings: GridSettings) -> dict[str, Any]:
    verifier = settings.validator_media_bond_verifier_version.strip()
    return {
        "chain_id": settings.validator_media_bond_chain_id,
        "bond_contract": settings.validator_media_bond_contract.strip().lower(),
        "verifier_version": verifier,
        "facet_runtime_hash": reviewed_runtime_hash(verifier) or "",
        "minimum_bond_raw": settings.validator_media_minimum_bond_raw,
        "minimum_quality_pass_rate": settings.validator_media_minimum_quality_pass_rate,
    }


async def review_reference(
    worker_id: str | UUID,
    *,
    model: str,
    modality: str,
    action: str,
    review_ref: str,
    quality_window_start: datetime | None = None,
    quality_window_end: datetime | None = None,
    quality_pass_rate: float | None = None,
    expected_digest: str | None = None,
    apply: bool = False,
    now: datetime | None = None,
    settings: GridSettings | None = None,
) -> dict[str, Any]:
    """Preview or atomically apply one reference review/state transition."""
    try:
        normalized_worker_id = worker_id if isinstance(worker_id, UUID) else UUID(str(worker_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ReferenceReviewError("worker_id must be a UUID") from exc
    normalized_model = model.strip()
    normalized_modality = modality.strip().lower()
    normalized_action = action.strip().lower()
    if not normalized_model or len(normalized_model) > 255:
        raise ReferenceReviewError("model is required and must be at most 255 characters")
    if normalized_modality not in VALID_MODALITIES:
        raise ReferenceReviewError("modality must be image or video")
    if normalized_action not in VALID_ACTIONS:
        raise ReferenceReviewError("action must be review, activate, pause, or revoke")
    if not _REVIEW_REF_RE.fullmatch(review_ref):
        raise ReferenceReviewError("review_ref must be a non-sensitive opaque reference")
    current = _aware(now or datetime.now(UTC))
    assert current is not None
    policy = _activation_policy(settings or get_settings())

    quality_values: dict[str, Any] = {}
    if normalized_action == "review":
        start = _aware(quality_window_start)
        end = _aware(quality_window_end)
        if not start or not end or end < start or end > current:
            raise ReferenceReviewError("quality window must be complete, ordered, and not future")
        if quality_pass_rate is None or not 0.0 <= quality_pass_rate <= 1.0:
            raise ReferenceReviewError("quality_pass_rate must be between 0 and 1")
        quality_values = {
            "quality_window_start": start,
            "quality_window_end": end,
            "quality_pass_rate": quality_pass_rate,
            "quality_reviewed_at": current,
        }

    async with await new_session() as session:
        worker_query = sa.select(workers_t).where(workers_t.c.id == normalized_worker_id)
        if apply:
            worker_query = worker_query.with_for_update()
        worker = (await session.execute(worker_query)).mappings().one_or_none()
        if worker is None:
            raise ReferenceReviewError("worker does not exist")
        account_id = worker["account_id"]
        wallet = str(worker["wallet"] or "").strip().lower()
        advertised_models = {
            str(item).strip() for item in (worker["models"] or []) if str(item).strip()
        }
        if account_id is None or not Web3.is_address(wallet):
            raise ReferenceReviewError("worker account or payout wallet identity is incomplete")
        if normalized_model not in advertised_models:
            raise ReferenceReviewError("worker does not currently advertise this model")

        ref_query = sa.select(references_t).where(
            references_t.c.worker_id == normalized_worker_id,
            references_t.c.model == normalized_model,
            references_t.c.modality == normalized_modality,
        )
        if apply:
            ref_query = ref_query.with_for_update()
        reference = (await session.execute(ref_query)).mappings().one_or_none()
        row = dict(reference) if reference else None
        current_digest = _digest(
            row,
            worker_id=normalized_worker_id,
            model=normalized_model,
            modality=normalized_modality,
            worker_account_id=account_id,
            worker_wallet=wallet,
            policy=policy,
        )
        if apply and expected_digest != current_digest:
            raise ReferenceReviewError("reference state changed; preview again")
        if row and row["status"] == "revoked":
            raise ReferenceReviewError("revoked reference cannot be changed")

        identity_matches = bool(
            row
            and row["account_id"] == account_id
            and str(row["payout_wallet"] or "").strip().lower() == wallet,
        )
        identity_changed = bool(row and not identity_matches)
        prospective = dict(row or {})
        if normalized_action == "review":
            prospective.update(
                account_id=account_id,
                payout_wallet=wallet,
                **quality_values,
            )
            if identity_changed:
                prospective.update(
                    bond_contract=None,
                    bond_chain_id=None,
                    bond_finalized_block=None,
                    bond_finalized_block_hash=None,
                    bond_facet_address=None,
                    bond_facet_runtime_hash=None,
                    bond_amount_raw=0,
                    bond_active=False,
                    bond_slashed=False,
                    bond_verifier_version=None,
                    bond_status_reason="identity_changed",
                    bond_verified_at=None,
                )
        activation_reasons = _activation_reasons(prospective, now=current, policy=policy)
        if identity_changed:
            activation_reasons.append("worker identity differs from reviewed identity")
        if normalized_action == "review":
            proposed_status = (
                row["status"]
                if row and identity_matches and not activation_reasons
                else "paused"
            )
        else:
            if row is None:
                raise ReferenceReviewError("reference must be reviewed before status changes")
            proposed_status = {
                "activate": "active",
                "pause": "paused",
                "revoke": "revoked",
            }[normalized_action]
            if not identity_matches:
                raise ReferenceReviewError("worker identity changed; submit a fresh review")
            if normalized_action == "activate" and activation_reasons:
                raise ReferenceReviewError(
                    "reference is not activation-ready: " + "; ".join(activation_reasons),
                )

        result = {
            "worker_id": str(normalized_worker_id),
            "model": normalized_model,
            "modality": normalized_modality,
            "action": normalized_action,
            "apply": apply,
            "current_digest": current_digest,
            "current_status": row["status"] if row else "missing",
            "proposed_status": proposed_status,
            "review_ref": review_ref,
            "activation_ready": not activation_reasons,
            "activation_blockers": activation_reasons,
            "activation_policy": policy,
            "economic_effect": "none",
            "bond_evidence_invalidated": identity_changed,
        }
        if not apply:
            return result

        if normalized_action == "review":
            values = {
                "account_id": account_id,
                "payout_wallet": wallet,
                "status": proposed_status,
                "status_reason": review_ref,
                "updated": current,
                **quality_values,
            }
            if identity_changed:
                values.update(
                    bond_contract=None,
                    bond_chain_id=None,
                    bond_finalized_block=None,
                    bond_finalized_block_hash=None,
                    bond_facet_address=None,
                    bond_facet_runtime_hash=None,
                    bond_amount_raw=0,
                    bond_active=False,
                    bond_slashed=False,
                    bond_verifier_version=None,
                    bond_status_reason="identity_changed",
                    bond_verified_at=None,
                )
            if row is None:
                await session.execute(
                    sa.insert(references_t).values(
                        worker_id=normalized_worker_id,
                        model=normalized_model,
                        modality=normalized_modality,
                        bond_active=False,
                        bond_slashed=False,
                        selection_count=0,
                        created=current,
                        **values,
                    ),
                )
            else:
                await session.execute(
                    sa.update(references_t)
                    .where(
                        references_t.c.worker_id == normalized_worker_id,
                        references_t.c.model == normalized_model,
                        references_t.c.modality == normalized_modality,
                    )
                    .values(**values),
                )
        else:
            await session.execute(
                sa.update(references_t)
                .where(
                    references_t.c.worker_id == normalized_worker_id,
                    references_t.c.model == normalized_model,
                    references_t.c.modality == normalized_modality,
                )
                .values(
                    status=proposed_status,
                    status_reason=review_ref,
                    updated=current,
                ),
            )
        await session.commit()
        result["applied_at"] = current.isoformat()
        return result

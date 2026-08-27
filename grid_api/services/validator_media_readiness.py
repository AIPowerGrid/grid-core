# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only rollout readiness for the dark validator media lanes."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ..v2.schema import validators as validators_t
from . import validator_references, validators
from .validator_operators import GROUP_RE

REQUIRED_PREVIEW_VALIDATORS = 5


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _worker_id(worker: dict[str, Any]) -> str:
    return str(worker.get("worker_id") or worker.get("id") or "")


def _capability_counts(rows: list[dict[str, Any]], capability: str, *, now: datetime) -> dict[str, int]:
    fresh_cutoff = now - timedelta(seconds=validators.VALIDATOR_HEARTBEAT_FRESH_SECONDS)
    fresh = 0
    independent_groups: set[str] = set()
    for row in rows:
        heartbeat = _aware(row.get("last_heartbeat"))
        capabilities = {str(value) for value in (row.get("capabilities") or [])}
        if heartbeat is None or heartbeat < fresh_cutoff or capability not in capabilities:
            continue
        fresh += 1
        reviewed_at = _aware(row.get("independence_reviewed_at"))
        review_expiry = _aware(row.get("independence_expires_at"))
        group_id = str(row.get("operator_group_id") or "")
        if (
            row.get("independence_status") == "verified"
            and reviewed_at is not None
            and reviewed_at <= now
            and review_expiry is not None
            and review_expiry >= now
            and review_expiry >= reviewed_at
            and GROUP_RE.fullmatch(group_id)
        ):
            independent_groups.add(group_id)
    return {"fresh": fresh, "verified_independent": len(independent_groups)}


def _without_gate_reasons(reasons: list[str], gate_reasons: set[str]) -> list[str]:
    return [reason for reason in reasons if reason not in gate_reasons]


async def inspect_media_readiness(
    session: AsyncSession,
    active_workers: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inspect live prerequisites without reserving references or changing state.

    The report is advisory. Assignment creation rechecks the same reference
    eligibility under row locks and persists its selected set transactionally.
    """
    current = _aware(now or _now())
    assert current is not None
    validator_rows = [
        dict(row)
        for row in (
            await session.execute(
                sa.select(
                    validators_t.c.capabilities,
                    validators_t.c.last_heartbeat,
                    validators_t.c.operator_group_id,
                    validators_t.c.independence_status,
                    validators_t.c.independence_reviewed_at,
                    validators_t.c.independence_expires_at,
                ).where(validators_t.c.status == "active"),
            )
        )
        .mappings()
        .all()
    ]

    image_policy = validators.media_validation_policy()
    video_policy = validators.video_validation_policy()
    image_config_blockers = _without_gate_reasons(
        list(image_policy["reasons"]),
        {"operator gate disabled"},
    )
    video_config_blockers = _without_gate_reasons(
        list(video_policy["reasons"]),
        {"operator gate disabled", "video probe operator gate disabled"},
    )
    image_validators = _capability_counts(
        validator_rows,
        "image.fidelity.v1",
        now=current,
    )
    video_validators = _capability_counts(
        validator_rows,
        "video.fidelity.v1",
        now=current,
    )

    image_candidates: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"recipe_ids": set(), "worker_ids": set()},
    )
    video_candidates: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"recipe_ids": set(), "worker_ids": set()},
    )
    workers_by_id = {_worker_id(worker): worker for worker in active_workers if _worker_id(worker)}
    for worker_id, worker in workers_by_id.items():
        for recipe in validators.image_validation_recipes_for_worker(worker):
            image_candidates[recipe.model_name]["recipe_ids"].add(str(recipe.recipe_id))
            image_candidates[recipe.model_name]["worker_ids"].add(worker_id)
        for recipe in validators.video_validation_recipes_for_worker(worker):
            video_candidates[recipe.model_name]["recipe_ids"].add(str(recipe.recipe_id))
            video_candidates[recipe.model_name]["worker_ids"].add(worker_id)

    image_models: list[dict[str, Any]] = []
    can_preview_references = not image_config_blockers
    for model in sorted(image_candidates):
        entry = image_candidates[model]
        online_ids = sorted(entry["worker_ids"])
        candidates_with_quorum = 0
        selector_blockers: set[str] = set()
        if can_preview_references:
            for candidate_id in online_ids:
                try:
                    await validator_references.preview_reference_workers(
                        session,
                        model=model,
                        modality="image",
                        candidate_worker_id=candidate_id,
                        online_model_worker_ids=online_ids,
                        expected_chain_id=int(image_policy["chain_id"]),
                        expected_bond_contract=str(image_policy["bond_contract"]),
                        expected_verifier_version=str(image_policy["bond_verifier_version"]),
                        expected_facet_runtime_hash=str(image_policy["bond_facet_runtime_hash"]),
                        minimum_bond_raw=int(image_policy["minimum_bond_raw"]),
                        minimum_quality_pass_rate=float(image_policy["minimum_quality_pass_rate"]),
                        now=current,
                    )
                except (TypeError, ValueError, validator_references.ReferencePoolUnavailable) as exc:
                    selector_blockers.add(str(exc))
                else:
                    candidates_with_quorum += 1
        image_models.append(
            {
                "model": model,
                "governed_recipes": len(entry["recipe_ids"]),
                "online_candidates": len(online_ids),
                "candidates_with_reference_quorum": candidates_with_quorum,
                "ready": candidates_with_quorum > 0,
                "selector_blockers": sorted(selector_blockers),
            },
        )

    video_models: list[dict[str, Any]] = []
    can_preview_video_references = not video_config_blockers
    for model in sorted(video_candidates):
        entry = video_candidates[model]
        online_ids = sorted(entry["worker_ids"])
        candidates_with_quorum = 0
        selector_blockers: set[str] = set()
        if can_preview_video_references:
            for candidate_id in online_ids:
                try:
                    await validator_references.preview_reference_workers(
                        session,
                        model=model,
                        modality="video",
                        candidate_worker_id=candidate_id,
                        online_model_worker_ids=online_ids,
                        expected_chain_id=int(video_policy["chain_id"]),
                        expected_bond_contract=str(video_policy["bond_contract"]),
                        expected_verifier_version=str(
                            video_policy["bond_verifier_version"]
                        ),
                        expected_facet_runtime_hash=str(
                            video_policy["bond_facet_runtime_hash"]
                        ),
                        minimum_bond_raw=int(video_policy["minimum_bond_raw"]),
                        minimum_quality_pass_rate=float(
                            video_policy["minimum_quality_pass_rate"]
                        ),
                        now=current,
                    )
                except (
                    TypeError,
                    ValueError,
                    validator_references.ReferencePoolUnavailable,
                ) as exc:
                    selector_blockers.add(str(exc))
                else:
                    candidates_with_quorum += 1
        video_models.append(
            {
                "model": model,
                "governed_recipes": len(entry["recipe_ids"]),
                "online_candidates": len(online_ids),
                "candidates_with_reference_quorum": candidates_with_quorum,
                "ready": candidates_with_quorum > 0,
                "selector_blockers": sorted(selector_blockers),
            },
        )

    image_blockers = list(image_config_blockers)
    if image_validators["fresh"] < REQUIRED_PREVIEW_VALIDATORS:
        image_blockers.append("fewer than five fresh image-capable validators")
    if image_validators["verified_independent"] < REQUIRED_PREVIEW_VALIDATORS:
        image_blockers.append("fewer than five verified independent image-capable validators")
    if not image_models:
        image_blockers.append("no online worker serves a governed deterministic image recipe")
    elif not any(item["ready"] for item in image_models):
        image_blockers.append("no image candidate has two eligible independent references")

    video_blockers = list(video_config_blockers)
    if video_validators["fresh"] < REQUIRED_PREVIEW_VALIDATORS:
        video_blockers.append("fewer than five fresh video-capable validators")
    if video_validators["verified_independent"] < REQUIRED_PREVIEW_VALIDATORS:
        video_blockers.append("fewer than five verified independent video-capable validators")
    if not video_models:
        video_blockers.append("no online worker serves a governed deterministic video recipe")
    elif not any(item["ready"] for item in video_models):
        video_blockers.append("no video candidate has two eligible independent references")

    return {
        "checked_at": current.isoformat(),
        "economic_effect": "none",
        "advisory_only": True,
        "image": {
            "assignment_gate_enabled": bool(image_policy["enabled"]),
            "ready_to_enable": not image_blockers,
            "validators": image_validators,
            "models": image_models,
            "blockers": sorted(set(image_blockers)),
        },
        "video": {
            "assignment_gate_enabled": bool(video_policy["enabled"]),
            "ready_to_enable": not video_blockers,
            "validators": video_validators,
            "models": video_models,
            "blockers": sorted(set(video_blockers)),
        },
    }

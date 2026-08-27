# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api.services import validator_media_readiness
from grid_api.v2.schema import accounts as accounts_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validators as validators_t

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as database:
        yield database
    await engine.dispose()


async def _validator(session, index, *, group=None, capabilities=None, fresh=True):
    account_id = uuid4()
    wallet = f"0x{index + 1:040x}"
    await session.execute(sa.insert(accounts_t).values(id=account_id, wallet=wallet, flags={}))
    await session.execute(
        sa.insert(validators_t).values(
            id=f"val_media_{index}",
            account_id=account_id,
            signing_wallet=wallet,
            software_version="preview-test",
            capabilities=capabilities or ["image.fidelity.v1"],
            registration_signature="0x" + "11" * 65,
            status="active",
            last_heartbeat=NOW if fresh else NOW - timedelta(hours=1),
            operator_group_id=group or f"opg_media_{index:08d}",
            independence_status="verified",
            independence_reviewed_at=NOW - timedelta(days=1),
            independence_expires_at=NOW + timedelta(days=29),
            created=NOW - timedelta(days=3),
            updated=NOW,
        ),
    )


def _image_policy(*, reasons=None):
    return {
        "enabled": not reasons,
        "reasons": reasons or [],
        "chain_id": 8453,
        "bond_contract": "0x" + "a" * 40,
        "bond_verifier_version": "worker-registry-v2-957685a",
        "bond_facet_runtime_hash": "0x" + "b" * 64,
        "minimum_bond_raw": 1,
        "minimum_quality_pass_rate": 0.95,
    }


def _video_policy(*, reasons=None):
    return {
        **_image_policy(reasons=reasons),
        "enabled": not reasons,
        "reasons": reasons or [],
    }


@pytest.mark.asyncio
async def test_image_readiness_requires_independent_validator_and_reference_quorum(
    monkeypatch,
    session,
):
    for index in range(5):
        await _validator(session, index)
    await session.commit()
    recipe = SimpleNamespace(model_name="krea-2-turbo", recipe_id=42)
    workers = [
        {
            "worker_id": f"00000000-0000-0000-0000-0000000000{index}",
            "models": [recipe.model_name],
            "job_types": ["image"],
        }
        for index in range(10, 13)
    ]

    monkeypatch.setattr(
        validator_media_readiness.validators,
        "media_validation_policy",
        lambda: _image_policy(reasons=["operator gate disabled"]),
    )
    monkeypatch.setattr(
        validator_media_readiness.validators,
        "video_validation_policy",
        lambda: _video_policy(reasons=["operator gate disabled"]),
    )
    monkeypatch.setattr(
        validator_media_readiness.validators,
        "image_validation_recipes_for_worker",
        lambda worker: [recipe],
    )
    monkeypatch.setattr(
        validator_media_readiness.validators,
        "video_validation_recipes_for_worker",
        lambda worker: [],
    )

    calls = []

    async def preview(session, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(worker_id="ref-a"), SimpleNamespace(worker_id="ref-b")]

    monkeypatch.setattr(
        validator_media_readiness.validator_references,
        "preview_reference_workers",
        preview,
    )

    report = await validator_media_readiness.inspect_media_readiness(
        session,
        workers,
        now=NOW,
    )

    assert report["economic_effect"] == "none"
    assert report["advisory_only"] is True
    assert report["image"]["ready_to_enable"] is True
    assert report["image"]["assignment_gate_enabled"] is False
    assert report["image"]["validators"] == {"fresh": 5, "verified_independent": 5}
    assert report["image"]["models"] == [
        {
            "model": recipe.model_name,
            "governed_recipes": 1,
            "online_candidates": 3,
            "candidates_with_reference_quorum": 3,
            "ready": True,
            "selector_blockers": [],
        },
    ]
    assert len(calls) == 3
    assert all(call["online_model_worker_ids"] for call in calls)
    assert report["video"]["ready_to_enable"] is False


@pytest.mark.asyncio
async def test_video_readiness_requires_deterministic_reference_quorum(
    monkeypatch,
    session,
):
    for index in range(5):
        await _validator(
            session,
            index,
            capabilities=["video.fidelity.v1"],
        )
    await session.commit()
    recipe = SimpleNamespace(model_name="ltx-video-deterministic", recipe_id=84)
    workers = [
        {
            "worker_id": f"00000000-0000-0000-0000-0000000000{index}",
            "models": [recipe.model_name],
            "job_types": ["video"],
        }
        for index in range(20, 23)
    ]

    monkeypatch.setattr(
        validator_media_readiness.validators,
        "media_validation_policy",
        lambda: _image_policy(reasons=["operator gate disabled"]),
    )
    monkeypatch.setattr(
        validator_media_readiness.validators,
        "video_validation_policy",
        lambda: _video_policy(
            reasons=["operator gate disabled", "video probe operator gate disabled"],
        ),
    )
    monkeypatch.setattr(
        validator_media_readiness.validators,
        "image_validation_recipes_for_worker",
        lambda worker: [],
    )
    monkeypatch.setattr(
        validator_media_readiness.validators,
        "video_validation_recipes_for_worker",
        lambda worker: [recipe],
    )

    calls = []

    async def preview(session, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(worker_id="ref-a"), SimpleNamespace(worker_id="ref-b")]

    monkeypatch.setattr(
        validator_media_readiness.validator_references,
        "preview_reference_workers",
        preview,
    )

    report = await validator_media_readiness.inspect_media_readiness(
        session,
        workers,
        now=NOW,
    )

    assert report["video"]["ready_to_enable"] is True
    assert report["video"]["assignment_gate_enabled"] is False
    assert report["video"]["validators"] == {
        "fresh": 5,
        "verified_independent": 5,
    }
    assert report["video"]["models"] == [
        {
            "model": recipe.model_name,
            "governed_recipes": 1,
            "online_candidates": 3,
            "candidates_with_reference_quorum": 3,
            "ready": True,
            "selector_blockers": [],
        },
    ]
    assert len(calls) == 3
    assert all(call["modality"] == "video" for call in calls)


@pytest.mark.asyncio
async def test_readiness_reports_config_and_independence_blockers(monkeypatch, session):
    for index in range(5):
        await _validator(session, index, group="opg_same_controller")
    await session.commit()

    monkeypatch.setattr(
        validator_media_readiness.validators,
        "media_validation_policy",
        lambda: _image_policy(
            reasons=["operator gate disabled", "finalized bond sync disabled"],
        ),
    )
    monkeypatch.setattr(
        validator_media_readiness.validators,
        "video_validation_policy",
        lambda: _video_policy(
            reasons=["operator gate disabled", "video probe operator gate disabled"],
        ),
    )
    monkeypatch.setattr(
        validator_media_readiness.validators,
        "image_validation_recipes_for_worker",
        lambda worker: [],
    )
    monkeypatch.setattr(
        validator_media_readiness.validators,
        "video_validation_recipes_for_worker",
        lambda worker: [],
    )

    report = await validator_media_readiness.inspect_media_readiness(session, [], now=NOW)

    assert report["image"]["ready_to_enable"] is False
    assert report["image"]["validators"] == {"fresh": 5, "verified_independent": 1}
    assert "finalized bond sync disabled" in report["image"]["blockers"]
    assert "fewer than five verified independent image-capable validators" in report["image"]["blockers"]
    assert "no online worker serves a governed deterministic image recipe" in report["image"]["blockers"]
    assert "operator gate disabled" not in report["image"]["blockers"]

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from grid_api import database
from grid_api.services import validator_shadow as shadow
from grid_api.services.route_commitments import job_ref as committed_job_ref
from grid_api.v2.schema import ledger as ledger_t
from grid_api.v2.schema import metadata
from grid_api.v2.schema import validator_assignments as assignments_t
from grid_api.v2.schema import validator_attestations as attestations_t
from grid_api.v2.schema import validator_probe_groups as probe_groups_t
from grid_api.v2.schema import validators as validators_t

NOW = datetime(2026, 9, 1, 16, 0, tzinfo=UTC)
RUN_ID = "shadow_2026_09_01_protocol_v1"
ROOT = Path(__file__).resolve().parents[3]


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    old_factory = database._session_factory
    database._session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monkeypatch.setattr(
        shadow,
        "get_settings",
        lambda: SimpleNamespace(
            validator_shadow_observer_enabled=True,
            validator_cohort_baseline_version="v0.1.0-preview.13",
            validator_shadow_sample_seconds=300,
            validator_shadow_route_hmac_secret=SecretStr("s" * 32),
        ),
    )
    monkeypatch.setattr(shadow, "_now", lambda: NOW)
    try:
        yield
    finally:
        database._session_factory = old_factory
        await engine.dispose()


def _candidates():
    return [
        {"worker_id": "worker-a", "model": "model-a", "baseline_rank": 0},
        {"worker_id": "worker-b", "model": "model-b", "baseline_rank": 1},
    ]


def _evidence(
    worker: str,
    model: str,
    outcome: str,
    *,
    commitment_char: str,
    finalized_at: datetime = NOW - timedelta(minutes=5),
    operators: int = 3,
    quorum_status: str = "finalized",
    bindings_valid: bool = True,
    dimension: str = "protocol_conformance",
):
    return {
        "group_commitment": commitment_char * 64,
        "worker_id": worker,
        "model": model,
        "modality": "text",
        "capability": "text.instruction.v1",
        "scoring_policy_id": "text.generated.v8",
        "evidence_dimension": dimension,
        "quorum_status": quorum_status,
        "outcome": outcome,
        "distinct_operator_count": operators,
        "bindings_valid": bindings_valid,
        "finalized_at": finalized_at,
    }


def _gate(*, operators: int = 3, participating: int = 3):
    return {
        "verified_independent_operators": operators,
        "participating_independent_operators": participating,
        "finalized_independent_probe_groups": 4,
        "cohort_monitor_status": "healthy",
        "unresolved_critical_incidents": False,
        "postgres_migration_verified": True,
        "postgres_concurrency_verified": True,
        "replay_verified": True,
        "no_side_effect_verified": True,
        "routing_effect": "none",
        "economic_effect": "none",
    }


def _live_gate_snapshot(gate=None, *, observed_at=NOW):
    snapshot = {
        "schema": "aipg.validator.shadow-live-start-gate.v1",
        "observed_at": observed_at.isoformat(),
        **(gate or _gate()),
    }
    return {**snapshot, "evaluation": shadow.evaluate_start_gate(snapshot)}


def _fake_live_gate(gate=None):
    async def evaluate(**kwargs):
        return _live_gate_snapshot(
            gate,
            observed_at=kwargs.get("observed_at") or NOW,
        )

    return evaluate


def _evaluate(evidence, *, worker="worker-a", model="model-a"):
    return shadow.evaluate_advisory(
        candidates=_candidates(),
        evidence=evidence,
        actual_model=model,
        actual_worker_id=worker,
        modality="text",
        requested_capability="text.instruction.v1",
        observed_at=NOW,
    )


async def _seed_authoritative_group(*, mismatch_validator: str | None = None) -> None:
    group_id = "prg_shadow_authoritative"
    evidence_hash = shadow.commitment({"group": group_id, "evidence": 1})
    group = {
        "id": group_id,
        "target_worker_id": "worker-a",
        "target_worker_name": "Worker A",
        "model": "model-a",
        "modality": "text",
        "capability": "text.instruction.v1",
        "canary_kind": "text.generated",
        "scoring_policy_id": "text.generated.v8",
        "challenge": {"private": True},
        "challenge_hash": shadow.commitment({"group": group_id, "challenge": 1}),
        "status": "finalized",
        "quorum_status": "finalized",
        "quorum_outcome": "healthy",
        "quorum_threshold": 3,
        "target_validator_count": 5,
        "probe_status": "completed",
        "probe_attempts": 1,
        "created": NOW - timedelta(hours=1),
        "expires": NOW - timedelta(minutes=40),
        "finalized": NOW - timedelta(minutes=5),
    }
    validators = [
        ("val_valid_1", "opg_valid_1", "verified", NOW - timedelta(minutes=1), "v0.1.0-preview.13"),
        ("val_valid_2", "opg_valid_2", "verified", NOW - timedelta(minutes=1), "0.1.0-preview.13"),
        ("val_valid_3", "opg_valid_3", "verified", NOW - timedelta(minutes=1), "v0.1.0-preview.13"),
        ("val_duplicate", "opg_valid_1", "verified", NOW - timedelta(minutes=1), "v0.1.0-preview.13"),
        ("val_first_party", "opg_first_party", "unreviewed", NOW - timedelta(minutes=1), "v0.1.0-preview.13"),
        ("val_stale", "opg_stale", "verified", NOW - timedelta(hours=2), "v0.1.0-preview.13"),
        ("val_outdated", "opg_outdated", "verified", NOW - timedelta(minutes=1), "v0.1.0-preview.9"),
    ]
    async with await database.new_session() as session:
        await session.execute(sa.insert(probe_groups_t).values(**group))
        for index, (validator_id, operator_group, independence, heartbeat, version) in enumerate(
            validators,
            start=1,
        ):
            account_id = UUID(int=index)
            wallet = "0x" + f"{index:040x}"
            assignment_id = f"asg_{validator_id}"
            nonce = f"grid-nonce-{validator_id}"
            assignment_evidence = "f" * 64 if validator_id == mismatch_validator else evidence_hash
            await session.execute(
                sa.insert(validators_t).values(
                    id=validator_id,
                    account_id=account_id,
                    signing_wallet=wallet,
                    software_version=version,
                    capabilities=["text"],
                    registration_signature="0x" + (f"{index:x}" * 130)[:130],
                    status="active",
                    last_heartbeat=heartbeat,
                    operator_group_id=operator_group,
                    independence_status=independence,
                    qualification_started_at=NOW - timedelta(days=4),
                    heartbeat_sample_count=900,
                    last_heartbeat_sampled_at=heartbeat,
                    independence_reviewed_at=(NOW - timedelta(days=1) if independence == "verified" else None),
                    independence_expires_at=(NOW + timedelta(days=30) if independence == "verified" else None),
                    independence_review_ref=(f"review/{validator_id}" if independence == "verified" else None),
                    created=NOW - timedelta(days=4),
                    updated=NOW - timedelta(minutes=1),
                ),
            )
            await session.execute(
                sa.insert(assignments_t).values(
                    id=assignment_id,
                    probe_group_id=group_id,
                    account_id=account_id,
                    validator_wallet=wallet,
                    validator_id=validator_id,
                    grid_nonce=nonce,
                    target_worker_id="worker-a",
                    target_worker_name="Worker A",
                    model="model-a",
                    modality="text",
                    capability="text.instruction.v1",
                    canary_kind="text.generated",
                    scoring_policy_id="text.generated.v8",
                    challenge={},
                    status="finalized",
                    quorum_status="finalized",
                    quorum_outcome="healthy",
                    probe_status="completed",
                    probe_attempts=1,
                    probe_evidence_hash=evidence_hash,
                    probe_verdict="healthy",
                    created=NOW - timedelta(minutes=55),
                    expires=NOW - timedelta(minutes=40),
                    probed=NOW - timedelta(minutes=30),
                    finalized=NOW - timedelta(minutes=5),
                ),
            )
            await session.execute(
                sa.insert(attestations_t).values(
                    attestation_hash=shadow.commitment({"attestation": validator_id}),
                    account_id=account_id,
                    validator_wallet=wallet,
                    validator_id=validator_id,
                    assignment_id=assignment_id,
                    probe_group_id=group_id,
                    grid_nonce=nonce,
                    evidence_hash=assignment_evidence,
                    authority="authoritative",
                    quorum_status="finalized",
                    worker_id="worker-a",
                    model="model-a",
                    modality="text",
                    capability="text.instruction.v1",
                    canary_kind="text.generated",
                    verdict="healthy",
                    score=1.0,
                    signature="0x" + (f"{index:x}" * 130)[:130],
                    signature_status="verified",
                    payload={"bounded": True},
                    created=NOW - timedelta(minutes=20),
                ),
            )
        await session.commit()


def test_start_gate_requires_three_verified_participating_operators_and_zero_effect():
    ready = shadow.evaluate_start_gate(_gate())
    assert ready["eligible"] is True
    assert ready["failed"] == []

    not_ready = shadow.evaluate_start_gate(_gate(operators=2, participating=2))
    assert not_ready["eligible"] is False
    assert not_ready["failed"] == [
        "verified_independent_operators",
        "participating_independent_operators",
    ]

    economic = {**_gate(), "economic_effect": "rewards"}
    assert "economic_effect_none" in shadow.evaluate_start_gate(economic)["failed"]


def test_policy_rejects_subjective_quality_and_weakened_gates():
    with pytest.raises(ValueError, match="subjective quality"):
        shadow.frozen_policy_config({"allowed_dimensions": ["quality"]})
    with pytest.raises(ValueError, match="below three"):
        shadow.frozen_policy_config({"quorum_min": 2})
    with pytest.raises(ValueError, match="shorter than 168"):
        shadow.frozen_policy_config({"run_hours": 24})
    with pytest.raises(ValueError, match="below 0.80"):
        shadow.frozen_policy_config({"required_sample_coverage": 0.79})
    with pytest.raises(ValueError, match="72 hours"):
        shadow.frozen_policy_config({"minimum_qualification_seconds": 71 * 3600})
    with pytest.raises(ValueError, match="candidate basis"):
        shadow.frozen_policy_config({"candidate_basis": "exact_scheduler_candidates.v1"})


def test_runtime_policy_cannot_drift_from_deployed_baseline_or_sample_interval(db):
    with pytest.raises(ValueError, match="configured cohort baseline"):
        shadow.runtime_policy_config(
            {"validator_baseline_version": "v0.1.0-preview.9"},
        )
    with pytest.raises(ValueError, match="deployment configuration"):
        shadow.runtime_policy_config({"sample_interval_seconds": 600})


def test_actual_healthy_is_same():
    result = _evaluate([_evidence("worker-a", "model-a", "healthy", commitment_char="a")])
    assert result["candidate_basis"] == shadow.CANDIDATE_BASIS
    assert result["decision_class"] == "same"
    assert result["reason_code"] == "actual_objectively_healthy"
    assert result["hypothetical_worker_id"] == "worker-a"
    assert result["mutation_attempted"] is False


def test_failed_actual_and_healthy_alternative_would_change():
    result = _evaluate(
        [
            _evidence("worker-a", "model-a", "failed", commitment_char="a"),
            _evidence("worker-b", "model-b", "healthy", commitment_char="b"),
        ],
    )
    assert result["decision_class"] == "would_change"
    assert result["reason_code"] == "actual_failed_alternative_healthy"
    assert result["hypothetical_worker_id"] == "worker-b"
    assert result["hypothetical_model"] == "model-b"
    assert result["eligible_operator_count"] == 3
    assert result["evidence_commitments"] == ["a" * 64, "b" * 64]


def test_all_objectively_failed_would_exclude():
    result = _evaluate(
        [
            _evidence("worker-a", "model-a", "failed", commitment_char="a"),
            _evidence("worker-b", "model-b", "failed", commitment_char="b"),
        ],
    )
    assert result["decision_class"] == "would_exclude"
    assert result["hypothetical_worker_id"] is None


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        ([], "actual_evidence_insufficient"),
        (
            [_evidence("worker-a", "model-a", "failed", commitment_char="a", operators=2)],
            "actual_evidence_insufficient",
        ),
        (
            [
                _evidence(
                    "worker-a",
                    "model-a",
                    "failed",
                    commitment_char="a",
                    quorum_status="disputed",
                ),
            ],
            "actual_evidence_insufficient",
        ),
        (
            [
                _evidence(
                    "worker-a",
                    "model-a",
                    "failed",
                    commitment_char="a",
                    bindings_valid=False,
                ),
            ],
            "actual_evidence_insufficient",
        ),
        (
            [
                _evidence(
                    "worker-a",
                    "model-a",
                    "failed",
                    commitment_char="a",
                    finalized_at=NOW - timedelta(days=2),
                ),
            ],
            "actual_evidence_insufficient",
        ),
        (
            [_evidence("worker-a", "model-a", "slow", commitment_char="a")],
            "actual_outcome_nonnegative_or_unknown",
        ),
    ],
)
def test_ambiguous_or_ineligible_evidence_never_becomes_negative(evidence, reason):
    result = _evaluate(evidence)
    assert result["decision_class"] == "insufficient_evidence"
    assert result["reason_code"] == reason


def test_unknown_actual_worker_with_multiple_model_replicas_is_insufficient():
    candidates = [
        {"worker_id": "worker-a", "model": "model-a", "baseline_rank": 0},
        {"worker_id": "worker-b", "model": "model-a", "baseline_rank": 1},
    ]
    result = shadow.evaluate_advisory(
        candidates=candidates,
        evidence=[],
        actual_model="model-a",
        actual_worker_id=None,
        modality="text",
        requested_capability="text.instruction.v1",
        observed_at=NOW,
    )
    assert result["decision_class"] == "insufficient_evidence"
    assert result["reason_code"] == "actual_worker_unknown"


def test_job_reference_is_keyed_and_does_not_expose_job_id():
    one = committed_job_ref("job-secret-123", secret="server-secret")
    two = committed_job_ref("job-secret-123", secret="other-secret")
    assert one != two
    assert len(one) == 64
    assert "job-secret-123" not in one


@pytest.mark.asyncio
async def test_authoritative_snapshot_counts_control_groups_once_and_excludes_ineligible_nodes(db):
    await _seed_authoritative_group()
    evidence = await shadow.authoritative_evidence_snapshot(
        candidates=_candidates(),
        modality="text",
        capability="text.instruction.v1",
        observed_at=NOW,
    )
    assert len(evidence) == 1
    assert evidence[0]["distinct_operator_count"] == 3
    assert evidence[0]["bindings_valid"] is True
    assert evidence[0]["outcome"] == "healthy"
    assert evidence[0]["evidence_dimension"] == "protocol_conformance"
    serialized = shadow.canonical_json(evidence)
    for private_value in ("opg_", "val_", "asg_", "prg_", "grid-nonce", "0x0000"):
        assert private_value not in serialized


@pytest.mark.asyncio
async def test_core_evidence_mismatch_is_excluded_before_policy_evaluation(db):
    await _seed_authoritative_group(mismatch_validator="val_valid_3")
    evidence = await shadow.authoritative_evidence_snapshot(
        candidates=_candidates(),
        modality="text",
        capability="text.instruction.v1",
        observed_at=NOW,
    )
    assert evidence[0]["distinct_operator_count"] == 2
    result = _evaluate(evidence)
    assert result["decision_class"] == "insufficient_evidence"
    assert result["reason_code"] == "actual_evidence_insufficient"


@pytest.mark.asyncio
async def test_non_hex_authoritative_hash_is_excluded_before_policy_evaluation(db):
    await _seed_authoritative_group()
    async with await database.new_session() as session:
        await session.execute(
            sa.update(probe_groups_t).where(probe_groups_t.c.id == "prg_shadow_authoritative").values(challenge_hash="z" * 64),
        )
        await session.commit()
    evidence = await shadow.authoritative_evidence_snapshot(
        candidates=_candidates(),
        modality="text",
        capability="text.instruction.v1",
        observed_at=NOW,
    )
    assert evidence == []


@pytest.mark.asyncio
async def test_live_gate_is_derived_from_verified_independent_core_evidence(db):
    await _seed_authoritative_group()
    snapshot = await shadow.live_start_gate_snapshot(
        verification={
            "postgres_migration_verified": True,
            "postgres_concurrency_verified": True,
            "replay_verified": True,
            "no_side_effect_verified": True,
        },
        observed_at=NOW,
    )
    assert snapshot["verified_independent_operators"] == 3
    assert snapshot["participating_independent_operators"] == 3
    assert snapshot["finalized_independent_probe_groups"] == 1
    assert snapshot["evaluation"]["eligible"] is True


def test_live_routing_and_economic_paths_cannot_read_shadow_state():
    protected = [
        ROOT / "grid_api/routers/openai.py",
        ROOT / "grid_api/routers/_passthrough.py",
        ROOT / "grid_api/routers/worker_ws.py",
        ROOT / "grid_api/routers/stats.py",
        ROOT / "grid_api/services/router.py",
        ROOT / "grid_api/services/credits.py",
        ROOT / "grid_api/services/ledger.py",
        ROOT / "grid_api/services/economics.py",
        ROOT / "grid_api/services/enforcement.py",
        ROOT / "grid_api/services/validator_bonds.py",
        ROOT / "grid_api/services/validator_references.py",
        *sorted((ROOT / "grid_api/services/settlement").glob("*.py")),
    ]
    for path in protected:
        source = path.read_text()
        assert "validator_shadow" not in source, path
        assert "grid_validator_shadow_" not in source, path


async def _running_run(monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setattr(shadow, "live_start_gate_snapshot", _fake_live_gate())
        await shadow.create_run(
            run_id=RUN_ID,
            policy_config=None,
            implementation_commit="a" * 40,
            verification_ref="ci://validator-shadow/1",
            verification=_gate(),
            observed_at=NOW,
        )
        return await shadow.start_run(RUN_ID, started_at=NOW)


@pytest.mark.asyncio
async def test_collection_is_dark_by_default(db, monkeypatch):
    monkeypatch.setattr(
        shadow,
        "get_settings",
        lambda: SimpleNamespace(
            validator_shadow_observer_enabled=False,
            validator_cohort_baseline_version="v0.1.0-preview.13",
            validator_shadow_sample_seconds=300,
            validator_shadow_route_hmac_secret=SecretStr("s" * 32),
        ),
    )
    monkeypatch.setattr(shadow, "live_start_gate_snapshot", _fake_live_gate())
    await shadow.create_run(
        run_id=RUN_ID,
        policy_config=None,
        implementation_commit="a" * 40,
        verification_ref="ci://validator-shadow/1",
        verification=_gate(),
        observed_at=NOW,
    )
    with pytest.raises(shadow.ShadowDisabled):
        await shadow.start_run(RUN_ID, started_at=NOW)


@pytest.mark.asyncio
async def test_start_and_finish_times_must_be_current(db, monkeypatch):
    monkeypatch.setattr(shadow, "live_start_gate_snapshot", _fake_live_gate())
    await shadow.create_run(
        run_id=RUN_ID,
        policy_config=None,
        implementation_commit="a" * 40,
        verification_ref="ci://validator-shadow/fresh-time",
        verification=_gate(),
        observed_at=NOW,
    )
    with pytest.raises(ValueError, match="within five minutes"):
        await shadow.start_run(RUN_ID, started_at=NOW - timedelta(minutes=6))
    with pytest.raises(ValueError, match="cannot precede draft creation"):
        await shadow.start_run(RUN_ID, started_at=NOW - timedelta(minutes=1))
    running = await shadow.start_run(RUN_ID, started_at=NOW)
    monkeypatch.setattr(shadow, "_now", lambda: running["scheduled_end"])
    with pytest.raises(ValueError, match="within five minutes"):
        await shadow.finish_run(
            RUN_ID,
            status="completed",
            ended_at=running["scheduled_end"] + timedelta(minutes=6),
        )


@pytest.mark.asyncio
async def test_only_one_shadow_run_may_be_running(db, monkeypatch):
    monkeypatch.setattr(shadow, "live_start_gate_snapshot", _fake_live_gate())
    for run_id in (RUN_ID, "shadow_2026_09_01_protocol_v2"):
        await shadow.create_run(
            run_id=run_id,
            policy_config=None,
            implementation_commit="a" * 40,
            verification_ref=f"ci://validator-shadow/{run_id}",
            verification=_gate(),
            observed_at=NOW,
        )
    await shadow.start_run(RUN_ID, started_at=NOW)
    with pytest.raises(shadow.ShadowConflict, match="already running"):
        await shadow.start_run("shadow_2026_09_01_protocol_v2", started_at=NOW)


@pytest.mark.asyncio
async def test_ineligible_run_cannot_start_even_when_enabled(db, monkeypatch):
    monkeypatch.setattr(
        shadow,
        "live_start_gate_snapshot",
        _fake_live_gate(_gate(operators=2)),
    )
    await shadow.create_run(
        run_id=RUN_ID,
        policy_config=None,
        implementation_commit="a" * 40,
        verification_ref="ci://validator-shadow/1",
        verification=_gate(operators=2),
        observed_at=NOW,
    )
    with pytest.raises(shadow.ShadowStartGateError, match="verified_independent_operators"):
        await shadow.start_run(RUN_ID, started_at=NOW)


@pytest.mark.asyncio
async def test_start_rechecks_live_gate_instead_of_trusting_draft_snapshot(db, monkeypatch):
    snapshots = iter(
        (
            _live_gate_snapshot(_gate()),
            _live_gate_snapshot(_gate(operators=2)),
        ),
    )

    async def changing_gate(**_kwargs):
        return next(snapshots)

    monkeypatch.setattr(shadow, "live_start_gate_snapshot", changing_gate)
    draft = await shadow.create_run(
        run_id=RUN_ID,
        policy_config=None,
        implementation_commit="a" * 40,
        verification_ref="ci://validator-shadow/1",
        verification=_gate(),
        observed_at=NOW,
    )
    assert draft["start_gate"]["evaluation"]["eligible"] is True
    with pytest.raises(shadow.ShadowStartGateError, match="verified_independent_operators"):
        await shadow.start_run(RUN_ID, started_at=NOW)


@pytest.mark.asyncio
async def test_create_and_start_reject_stale_operator_gate_hashes(db, monkeypatch):
    live_gate = _live_gate_snapshot()
    monkeypatch.setattr(shadow, "live_start_gate_snapshot", _fake_live_gate())

    with pytest.raises(shadow.ShadowStartGateError, match="creation gate changed"):
        await shadow.create_run(
            run_id=RUN_ID,
            policy_config=None,
            implementation_commit="a" * 40,
            verification_ref="ci://validator-shadow/stale-preview",
            verification=_gate(),
            observed_at=NOW,
            expected_start_gate_hash="0" * 64,
        )

    await shadow.create_run(
        run_id=RUN_ID,
        policy_config=None,
        implementation_commit="a" * 40,
        verification_ref="ci://validator-shadow/fresh-preview",
        verification=_gate(),
        observed_at=NOW,
        expected_start_gate_hash=shadow.commitment(live_gate),
    )
    with pytest.raises(shadow.ShadowStartGateError, match="start gate changed"):
        await shadow.start_run(
            RUN_ID,
            started_at=NOW,
            expected_start_gate_hash="0" * 64,
        )

    started = await shadow.start_run(
        RUN_ID,
        started_at=NOW,
        expected_start_gate_hash=shadow.commitment(live_gate),
    )
    assert started["status"] == "running"
    assert (await shadow.get_run(RUN_ID))["status"] == "running"


@pytest.mark.asyncio
async def test_create_and_start_derive_real_gate_from_core(db):
    await _seed_authoritative_group()
    draft = await shadow.create_run(
        run_id=RUN_ID,
        policy_config=None,
        implementation_commit="a" * 40,
        verification_ref="ci://validator-shadow/real-core-gate",
        verification=_gate(),
        observed_at=NOW,
    )
    assert draft["start_gate"]["verified_independent_operators"] == 3
    assert draft["start_gate"]["evaluation"]["eligible"] is True
    started = await shadow.start_run(RUN_ID, started_at=NOW)
    assert started["start_gate"]["participating_independent_operators"] == 3
    assert started["start_gate"]["evaluation"]["eligible"] is True


@pytest.mark.asyncio
async def test_public_observation_path_derives_evidence_in_core(db, monkeypatch):
    await _seed_authoritative_group()
    await _running_run(monkeypatch)
    observation = await shadow.record_observation(
        run_id=RUN_ID,
        route_ref="e" * 64,
        job_ref="1" * 64,
        task_class="simple",
        modality="text",
        requested_capability="text.instruction.v1",
        candidates=_candidates(),
        actual_model="model-a",
        actual_worker_id="worker-a",
        observed_at=NOW,
    )
    assert observation["decision_class"] == "same"
    assert observation["eligible_operator_count"] == 3
    assert len(observation["evidence_snapshot"]) == 1


@pytest.mark.asyncio
async def test_public_capacity_sample_derives_counts_in_core(db, monkeypatch):
    await _seed_authoritative_group()
    await _running_run(monkeypatch)
    sample = await shadow.record_capacity_sample(
        run_id=RUN_ID,
        sampled_at=NOW + timedelta(minutes=5),
    )
    assert sample["verified_independent"] == 3
    assert sample["participating_independent"] == 3
    assert sample["finalized_independent_groups"] == 1
    assert sample["quorum_available"] is True


@pytest.mark.asyncio
async def test_observation_outcome_and_sample_are_exactly_idempotent_and_replayable(
    db,
    monkeypatch,
):
    await _running_run(monkeypatch)
    evidence = [
        _evidence("worker-a", "model-a", "failed", commitment_char="a"),
        _evidence("worker-b", "model-b", "healthy", commitment_char="b"),
    ]
    route_ref = "c" * 64
    kwargs = {
        "run_id": RUN_ID,
        "route_ref": route_ref,
        "job_ref": "2" * 64,
        "task_class": "simple",
        "modality": "text",
        "requested_capability": "text.instruction.v1",
        "candidates": _candidates(),
        "evidence": evidence,
        "actual_model": "model-a",
        "actual_worker_id": "worker-a",
        "observed_at": NOW + timedelta(minutes=1),
    }
    first = await shadow._record_observation(**kwargs)
    duplicate = await shadow._record_observation(**kwargs)
    assert duplicate["id"] == first["id"]
    assert duplicate["payload_hash"] == first["payload_hash"]

    replay = await shadow.replay_observation(first["id"])
    assert replay["ok"] is True

    outcome_kwargs = {
        "observation_id": first["id"],
        "actual_worker_id": "worker-a",
        "terminal_status": "succeeded",
        "duration_ms": 1200,
        "finished_at": NOW + timedelta(minutes=2),
    }
    outcome = await shadow.record_outcome(**outcome_kwargs)
    assert (await shadow.record_outcome(**outcome_kwargs))["id"] == outcome["id"]

    sample_kwargs = {
        "run_id": RUN_ID,
        "sampled_at": NOW + timedelta(minutes=5),
        "verified_independent": 3,
        "participating_independent": 3,
        "finalized_independent_groups": 4,
    }
    sample = await shadow._record_capacity_sample(**sample_kwargs)
    assert (await shadow._record_capacity_sample(**sample_kwargs))["id"] == sample["id"]

    error = await shadow.record_error(
        run_id=RUN_ID,
        stage="capture",
        error_code="outbox_gap",
        observed_at=NOW + timedelta(minutes=3),
    )
    assert error["error_code"] == "outbox_gap"

    report = await shadow.run_report(RUN_ID, at=NOW + timedelta(hours=1))
    assert report["schema"] == "aipg.validator.shadow-report.v2"
    assert report["candidate_basis"] == shadow.CANDIDATE_BASIS
    assert report["counterfactual_scope"].startswith("same-model replica preference")
    assert report["decisions"]["would_change"] == 1
    assert report["terminal_outcomes"] == {"succeeded": 1}
    assert report["observer_errors"] == [
        {"stage": "capture", "error_code": "outbox_gap", "count": 1},
    ]
    assert report["routing_effect"] == "none"
    assert report["economic_effect"] == "none"
    assert report["automatic_promotion"] is False


@pytest.mark.asyncio
async def test_idempotency_key_reuse_with_different_payload_fails_closed(db, monkeypatch):
    await _running_run(monkeypatch)
    kwargs = {
        "run_id": RUN_ID,
        "route_ref": "d" * 64,
        "job_ref": "3" * 64,
        "task_class": "simple",
        "modality": "text",
        "requested_capability": "text.instruction.v1",
        "candidates": _candidates(),
        "evidence": [_evidence("worker-a", "model-a", "healthy", commitment_char="a")],
        "actual_model": "model-a",
        "actual_worker_id": "worker-a",
        "observed_at": NOW + timedelta(minutes=1),
    }
    await shadow._record_observation(**kwargs)
    with pytest.raises(shadow.ShadowConflict):
        await shadow._record_observation(**{**kwargs, "task_class": "code"})


@pytest.mark.asyncio
async def test_terminal_outcome_is_bound_to_observed_worker_and_time(db, monkeypatch):
    await _running_run(monkeypatch)
    observation = await shadow._record_observation(
        run_id=RUN_ID,
        route_ref="e" * 64,
        job_ref="4" * 64,
        task_class="simple",
        modality="text",
        requested_capability="text.instruction.v1",
        candidates=_candidates(),
        evidence=[_evidence("worker-a", "model-a", "healthy", commitment_char="a")],
        actual_model="model-a",
        actual_worker_id="worker-a",
        observed_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(shadow.ShadowConflict, match="terminal worker"):
        await shadow.record_outcome(
            observation_id=observation["id"],
            actual_worker_id="worker-b",
            terminal_status="succeeded",
            duration_ms=100,
            finished_at=NOW + timedelta(minutes=2),
        )
    with pytest.raises(shadow.ShadowError, match="cannot precede"):
        await shadow.record_outcome(
            observation_id=observation["id"],
            actual_worker_id="worker-a",
            terminal_status="succeeded",
            duration_ms=100,
            finished_at=NOW,
        )


@pytest.mark.asyncio
async def test_shadow_records_must_fall_inside_frozen_run_window(db, monkeypatch):
    await _running_run(monkeypatch)
    with pytest.raises(shadow.ShadowError, match="frozen shadow run window"):
        await shadow._record_observation(
            run_id=RUN_ID,
            route_ref="f" * 64,
            job_ref="5" * 64,
            task_class="simple",
            modality="text",
            requested_capability="text.instruction.v1",
            candidates=_candidates(),
            evidence=[],
            actual_model="model-a",
            actual_worker_id="worker-a",
            observed_at=NOW - timedelta(seconds=1),
        )
    with pytest.raises(shadow.ShadowError, match="frozen shadow run window"):
        await shadow._record_capacity_sample(
            run_id=RUN_ID,
            sampled_at=NOW + timedelta(hours=168, seconds=1),
            verified_independent=3,
            participating_independent=3,
            finalized_independent_groups=1,
        )
    with pytest.raises(shadow.ShadowError, match="frozen shadow run window"):
        await shadow.record_error(
            run_id=RUN_ID,
            stage="capture",
            error_code="late_capture",
            observed_at=NOW + timedelta(hours=168, seconds=1),
        )


def test_missing_samples_and_negative_quorum_are_both_reported_as_gaps():
    start = NOW
    end = NOW + timedelta(hours=3)
    sparse = [
        {"sampled_at": start, "quorum_available": True},
        {"sampled_at": start + timedelta(hours=2), "quorum_available": True},
    ]
    assert (
        shadow._max_quorum_gap(
            sparse,
            start=start,
            end=end,
            sample_interval_seconds=300,
        )
        == 7200
    )

    unavailable = [
        {"sampled_at": start, "quorum_available": True},
        {"sampled_at": start + timedelta(minutes=5), "quorum_available": False},
        {"sampled_at": start + timedelta(minutes=65), "quorum_available": True},
        {"sampled_at": end, "quorum_available": True},
    ]
    assert (
        shadow._max_quorum_gap(
            unavailable,
            start=start,
            end=end,
            sample_interval_seconds=3600,
        )
        == 6900
    )

    sparse_but_under_one_hour = [
        {"sampled_at": start + timedelta(minutes=offset), "quorum_available": True} for offset in (0, 59, 118, 177)
    ]
    coverage = shadow._capacity_coverage(
        sparse_but_under_one_hour,
        start=start,
        end=end,
        sample_interval_seconds=300,
    )
    assert coverage == {
        "expected_slots": 37,
        "recorded_slots": 4,
        "quorum_slots": 4,
        "coverage": 4 / 37,
    }


@pytest.mark.asyncio
async def test_run_cannot_complete_early_and_completion_never_promotes(db, monkeypatch):
    await _running_run(monkeypatch)
    monkeypatch.setattr(shadow, "_now", lambda: NOW + timedelta(hours=167))
    with pytest.raises(shadow.ShadowStartGateError, match="before 168 hours"):
        await shadow.finish_run(RUN_ID, status="completed", ended_at=NOW + timedelta(hours=167))
    monkeypatch.setattr(shadow, "_now", lambda: NOW + timedelta(hours=168))
    done = await shadow.finish_run(RUN_ID, status="completed", ended_at=NOW + timedelta(hours=168))
    assert done["status"] == "completed"
    report = await shadow.run_report(RUN_ID, at=NOW + timedelta(hours=168))
    assert report["automatic_promotion"] is False
    assert report["review_eligible"] is False
    assert report["gates"]["observations_present"] is False


@pytest.mark.asyncio
async def test_finish_rejects_stale_operator_run_state_hash(db, monkeypatch):
    running = await _running_run(monkeypatch)
    monkeypatch.setattr(shadow, "_now", lambda: NOW + timedelta(hours=168))
    with pytest.raises(shadow.ShadowConflict, match="run state changed"):
        await shadow.finish_run(
            RUN_ID,
            status="completed",
            ended_at=NOW + timedelta(hours=168),
            expected_run_state_hash="0" * 64,
        )
    done = await shadow.finish_run(
        RUN_ID,
        status="completed",
        ended_at=NOW + timedelta(hours=168),
        expected_run_state_hash=shadow.run_state_hash(running),
    )
    assert done["status"] == "completed"


@pytest.mark.asyncio
async def test_report_detects_successful_routes_missing_from_capture(db, monkeypatch):
    await _running_run(monkeypatch)
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(ledger_t).values(
                job_id=UUID("10000000-0000-0000-0000-000000000001"),
                worker_id=UUID("20000000-0000-0000-0000-000000000001"),
                model="model-a",
                job_type="text",
                den=1.0,
                output_units=1,
                created=NOW + timedelta(seconds=1),
            ),
        )
        await session.commit()

    report = await shadow.run_report(RUN_ID, at=NOW + timedelta(seconds=2))
    assert report["production_successful_completions"] == 1
    assert report["captured_successful_routes"] == 0
    assert report["route_capture_coverage"] == 0.0
    assert report["gates"]["route_capture_coverage"] is False


@pytest.mark.asyncio
async def test_report_fails_closed_without_the_run_hmac_secret(db, monkeypatch):
    await _running_run(monkeypatch)
    monkeypatch.setattr(
        shadow,
        "get_settings",
        lambda: SimpleNamespace(validator_shadow_route_hmac_secret=None),
    )
    with pytest.raises(shadow.ShadowError, match="requires the shadow route HMAC secret"):
        await shadow.run_report(RUN_ID, at=NOW + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_report_matches_exact_jobs_instead_of_aggregate_route_count(db, monkeypatch):
    await _running_run(monkeypatch)
    expected_job = UUID("10000000-0000-0000-0000-000000000001")
    wrong_job = UUID("10000000-0000-0000-0000-000000000002")
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(ledger_t).values(
                job_id=expected_job,
                worker_id=UUID("20000000-0000-0000-0000-000000000001"),
                model="model-a",
                job_type="text",
                den=1.0,
                output_units=1,
                created=NOW + timedelta(seconds=1),
            ),
        )
        await session.commit()
    observation = await shadow._record_observation(
        run_id=RUN_ID,
        route_ref="a" * 64,
        job_ref=committed_job_ref(str(wrong_job), secret="s" * 32),
        task_class="simple",
        modality="text",
        requested_capability="text.instruction.v1",
        candidates=_candidates(),
        evidence=[],
        actual_model="model-a",
        actual_worker_id="worker-a",
        observed_at=NOW + timedelta(seconds=1),
    )
    await shadow.record_outcome(
        observation_id=observation["id"],
        actual_worker_id="worker-a",
        terminal_status="succeeded",
        duration_ms=100,
        finished_at=NOW + timedelta(seconds=2),
    )

    report = await shadow.run_report(RUN_ID, at=NOW + timedelta(seconds=3))
    assert report["production_successful_completions"] == 1
    assert report["captured_successful_routes"] == 1
    assert report["captured_successful_jobs"] == 1
    assert report["matched_successful_jobs"] == 0
    assert report["unmatched_captured_successful_jobs"] == 1
    assert report["route_capture_coverage"] == 0.0
    assert report["gates"]["route_capture_coverage"] is False


@pytest.mark.asyncio
async def test_report_deduplicates_retry_attempts_for_one_job(db, monkeypatch):
    await _running_run(monkeypatch)
    job_id = UUID("10000000-0000-0000-0000-000000000001")
    job_ref = committed_job_ref(str(job_id), secret="s" * 32)
    async with await database.new_session() as session:
        await session.execute(
            sa.insert(ledger_t).values(
                job_id=job_id,
                worker_id=UUID("20000000-0000-0000-0000-000000000001"),
                model="model-a",
                job_type="text",
                den=1.0,
                output_units=1,
                created=NOW + timedelta(seconds=1),
            ),
        )
        await session.commit()
    for index, route_char in enumerate(("a", "b"), start=1):
        observation = await shadow._record_observation(
            run_id=RUN_ID,
            route_ref=route_char * 64,
            job_ref=job_ref,
            task_class="simple",
            modality="text",
            requested_capability="text.instruction.v1",
            candidates=_candidates(),
            evidence=[],
            actual_model="model-a",
            actual_worker_id="worker-a",
            observed_at=NOW + timedelta(seconds=index),
        )
        await shadow.record_outcome(
            observation_id=observation["id"],
            actual_worker_id="worker-a",
            terminal_status="succeeded",
            duration_ms=100,
            finished_at=NOW + timedelta(seconds=index + 2),
        )

    report = await shadow.run_report(RUN_ID, at=NOW + timedelta(seconds=5))
    assert report["captured_successful_routes"] == 2
    assert report["captured_successful_jobs"] == 1
    assert report["matched_successful_jobs"] == 1
    assert report["unmatched_captured_successful_jobs"] == 0
    assert report["route_capture_coverage"] == 1.0

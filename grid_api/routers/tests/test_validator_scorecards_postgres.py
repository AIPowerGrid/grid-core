# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real-PG scorecard aggregation over accepted signed synthetic evidence."""

import json
import os
from datetime import timedelta

import pytest
import sqlalchemy as sa

from grid_api import database
from grid_api.routers.tests.test_validator_concurrency import (
    _isolated_logging_salt as _isolated_logging_salt,
)
from grid_api.routers.tests.test_validator_concurrency import pg as pg
from grid_api.routers.tests.test_validator_evidence_postgres import prepare, submit
from grid_api.services import validators as svc
from grid_api.v2.schema import validator_assignments as assignments
from grid_api.v2.schema import validator_attestations as attestations
from grid_api.v2.schema import validator_probe_groups as groups

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("VALIDATORS_TEST_DB_URL", "").startswith("postgresql"),
        reason="requires disposable PostgreSQL",
    ),
]


async def evidence_state():
    async with await database.new_session() as session:
        return [
            (await session.execute(sa.select(table).order_by(table.c.id))).mappings().all() for table in (assignments, attestations, groups)
        ]


async def test_signed_shared_group_counts_and_read_only_snapshot(pg, monkeypatch):
    now = svc._now().replace(microsecond=0)
    monkeypatch.setattr(svc, "_now", lambda: now)
    prepared = await prepare(3)
    assert len({entry[3]["probe_group_id"] for entry in prepared}) == 1
    for entry in prepared:
        assert (await submit(entry))["status"] == "accepted"
    async with await database.new_session() as session:
        await session.execute(sa.update(assignments).values(probed=now - timedelta(hours=3)))
        await session.commit()
    before = await evidence_state()
    report = await svc.scorecards(since_hours=1, authority="authoritative")
    assert await evidence_state() == before
    assert report["generated_at"] == now.isoformat()
    assert report["rate_basis"] == "attestation_votes"
    assert report["window_basis"] == "attestation_received_at"
    assert report["economic_effect"] == "none"
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["sampling"] == {
        "attestation_votes": 3,
        "distinct_assignments": 3,
        "distinct_probe_groups": 1,
        "distinct_registered_validators": 3,
        "votes_without_group": 0,
        "votes_without_registered_validator": 0,
        "independent_sample_count": None,
    }
    assert item["probe_freshness"]["votes_with_probe_time"] == 3
    assert item["probe_freshness"]["age_seconds"] == 10800
    assert item["healthy_rate"] == 1.0
    assert item["uncertainty"]["confidence_interval"] is None
    assert "operator_independence_not_established" in item["uncertainty"]["reasons"]
    assert item["quality_score"] is None
    serialized = json.dumps(report)
    for account, node, _key, payload in prepared:
        for private_value in (str(account), node, payload["grid_nonce"], payload["assignment_id"]):
            assert private_value not in serialized
    assert await svc.scorecards(model="model-not-probed") == {
        **report,
        "items": [],
        "count": 0,
        "window_hours": 168,
        "authority": "all",
        "filters": {"worker_id": None, "model": "model-not-probed"},
    }


@pytest.mark.parametrize(
    "status,offset,age,reason",
    [
        ("completed", -7200, 7200, None),
        ("completed", None, None, "probe_time_missing"),
        ("completed", 600, None, "probe_time_in_future"),
        ("running", -10, None, "probe_time_missing"),
    ],
)
async def test_probe_freshness_uses_completed_core_time(pg, monkeypatch, status, offset, age, reason):
    now = svc._now().replace(microsecond=0)
    monkeypatch.setattr(svc, "_now", lambda: now)
    entry = (await prepare())[0]
    await submit(entry)
    async with await database.new_session() as session:
        await session.execute(
            sa.update(assignments).values(
                probe_status=status,
                probed=None if offset is None else now + timedelta(seconds=offset),
            ),
        )
        await session.commit()
    item = (await svc.scorecards())["items"][0]
    assert item["probe_freshness"]["age_seconds"] == age
    assert item["probe_freshness"]["votes_with_probe_time"] == int(status == "completed" and offset is not None)
    assert item["uncertainty"]["confidence_interval"] is None
    if reason:
        assert reason in item["uncertainty"]["reasons"]


async def test_real_fk_pruning_preserves_vote_but_not_freshness(pg):
    entry = (await prepare())[0]
    await submit(entry)
    async with await database.new_session() as session:
        await session.execute(sa.delete(assignments))
        await session.execute(sa.delete(groups))
        await session.commit()
        row = (await session.execute(sa.select(attestations))).mappings().one()
        assert row["assignment_id"] is None and row["probe_group_id"] is None
    item = (await svc.scorecards())["items"][0]
    assert item["total"] == 1
    assert item["sampling"]["distinct_assignments"] == 0
    assert item["sampling"]["distinct_probe_groups"] == 0
    assert item["sampling"]["votes_without_group"] == 1
    assert item["probe_freshness"]["age_seconds"] is None
    assert "probe_time_missing" in item["uncertainty"]["reasons"]


async def test_received_window_excludes_old_votes(pg, monkeypatch):
    entry = (await prepare())[0]
    await submit(entry)
    now = svc._now().replace(microsecond=0)
    monkeypatch.setattr(svc, "_now", lambda: now)
    async with await database.new_session() as session:
        await session.execute(sa.update(attestations).values(created=now - timedelta(hours=2)))
        await session.commit()
    assert (await svc.scorecards(since_hours=1))["items"] == []
    assert (await svc.scorecards(since_hours=3))["items"][0]["total"] == 1

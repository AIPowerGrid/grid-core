# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime, timezone

import pytest

from grid_api.routers import health, stats


class _Redis:
    async def ping(self):
        return True


def test_build_commit_prefers_valid_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_BUILD_COMMIT", "A" * 40)
    monkeypatch.setattr(health, "_GIT_HEAD", tmp_path / "missing-head")

    assert health.build_commit() == "a" * 40


def test_build_commit_reads_detached_source_checkout(monkeypatch, tmp_path):
    head = tmp_path / "HEAD"
    head.write_text("B" * 40 + "\n", encoding="ascii")
    monkeypatch.delenv("GRID_BUILD_COMMIT", raising=False)
    monkeypatch.setattr(health, "_GIT_HEAD", head)

    assert health.build_commit() == "b" * 40


def test_build_commit_rejects_branch_and_invalid_environment(monkeypatch, tmp_path):
    head = tmp_path / "HEAD"
    head.write_text("ref: refs/heads/main\n", encoding="ascii")
    monkeypatch.setenv("GRID_BUILD_COMMIT", "not-a-release-sha")
    monkeypatch.setattr(health, "_GIT_HEAD", head)

    assert health.build_commit() is None


@pytest.mark.asyncio
async def test_network_status_is_redacted_and_reports_readiness(monkeypatch):
    monkeypatch.setattr(stats, "get_redis", lambda: _Redis())
    monkeypatch.setattr(
        stats,
        "_active_workers",
        lambda: _async_value([
            {
                "worker_id": "private-worker-id",
                "name": "private-worker-name",
                "models": ["model-a"],
                "job_types": ["text"],
            }
        ]),
    )
    monkeypatch.setattr(
        stats,
        "status_models",
        lambda: _async_value([
            {"name": "model-a", "type": "text", "count": 1},
            {"name": "model-b", "type": "image", "count": 3, "capabilities": ["txt2img"]},
        ]),
    )

    validator_health = {
        "window_hours": 24,
        "registered_active": 5,
        "heartbeat_fresh": 5,
        "participating": 3,
        "verified_independent": 0,
        "independence_proven": False,
        "quorum": {"pending": 1, "accepted": 2, "disputed": 0, "finalized": 1},
        "assignments_completed": 3,
        "authoritative_votes": 3,
        "agreement_rate": 1.0,
        "disputed_rate": 0.0,
        "coverage": {"workers": 1, "models": 1},
        "software_versions": [{"version": "0.1.0-preview", "validators": 5}],
        "economic_effect": "none",
    }
    from grid_api.services import validators

    monkeypatch.setattr(
        validators,
        "public_health",
        lambda **_: _async_value(validator_health),
    )

    now = datetime.now(timezone.utc)

    class _Result:
        def mappings(self):
            return self

        def one(self):
            return {"aipg": 12.5, "payouts": 2, "workers": 2, "last_paid": now}

    class _Session:
        async def execute(self, _query):
            return _Result()

    class _SessionContext:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return None

    async def _session():
        return _SessionContext()

    monkeypatch.setattr(stats, "new_session", _session)
    monkeypatch.setattr(stats, "build_commit", lambda: "a" * 40)

    result = await stats.status_network()

    assert result["status"] == "operational"
    assert result["build_commit"] == "a" * 40
    assert result["capacity"]["workers_online"] == 1
    assert result["capacity"]["models_below_target"] == ["model-a"]
    assert result["validators"] == validator_health
    assert result["payouts"]["aipg_paid"] == 12.5
    assert result["charging"]["mode"] in {"off", "allowlist", "on"}
    assert result["incidents"] == []
    assert {item["code"] for item in result["advisories"]} == {
        "limited_model_redundancy",
        "validator_independence_unproven",
    }
    rendered = str(result)
    assert "private-worker-id" not in rendered
    assert "private-worker-name" not in rendered


@pytest.mark.asyncio
async def test_network_status_degrades_without_runtime_dependencies(monkeypatch):
    class _BrokenRedis:
        async def ping(self):
            raise RuntimeError("offline")

    async def _broken(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(stats, "get_redis", lambda: _BrokenRedis())
    monkeypatch.setattr(stats, "_active_workers", _broken)
    monkeypatch.setattr(stats, "status_models", _broken)
    monkeypatch.setattr(stats, "new_session", _broken)
    from grid_api.services import validators

    monkeypatch.setattr(validators, "public_health", _broken)

    result = await stats.status_network()

    assert result["status"] == "degraded"
    assert result["capacity"]["workers_online"] == 0
    assert result["validators"] is None
    assert result["payouts"] is None
    assert {item["code"] for item in result["incidents"]} == {
        "redis_unavailable",
        "worker_registry_unavailable",
        "model_status_unavailable",
        "validator_status_unavailable",
        "payout_status_unavailable",
    }


async def _async_value(value):
    return value

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from types import SimpleNamespace

import pytest

from grid_api.routers import stats
from grid_api.routers.stats import _recipe_model_capacity


def _recipe(name="LTX Director 2.0", required=None, job_type="video"):
    return SimpleNamespace(
        model_name=name,
        required_models=required or ["LTX-2.3"],
        job_type=job_type,
    )


def test_recipe_model_capacity_maps_public_name_to_checkpoint_worker():
    workers = [
        {"models": ["LTX-2.3"], "job_types": ["image", "video"]},
        {"models": ["z-image-turbo"], "job_types": ["image"]},
    ]

    capacity = _recipe_model_capacity(workers, [_recipe()])

    assert capacity == {("LTX Director 2.0", "video"): {0}}


def test_recipe_requirements_must_be_colocated_on_one_worker():
    workers = [
        {"models": ["checkpoint-a"], "job_types": ["video"]},
        {"models": ["checkpoint-b"], "job_types": ["video"]},
    ]
    recipe = _recipe(required=["checkpoint-a", "checkpoint-b"])

    assert _recipe_model_capacity(workers, [recipe]) == {}


def test_recipe_capacity_requires_matching_modality():
    workers = [{"models": ["LTX-2.3"], "job_types": ["image"]}]

    assert _recipe_model_capacity(workers, [_recipe(job_type="video")]) == {}


@pytest.mark.asyncio
async def test_status_models_advertises_executable_recipe_name(monkeypatch):
    workers = [
        {
            "models": ["LTX-2.3"],
            "job_types": ["image", "video"],
            "max_context_length": 2048,
        }
    ]

    class _SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def _active_workers():
        return workers

    async def _new_session():
        return _SessionContext()

    async def _perf_by_model(session, since):
        return {}

    monkeypatch.setattr(stats, "_active_workers", _active_workers)
    monkeypatch.setattr(stats, "new_session", _new_session)
    monkeypatch.setattr(stats, "_perf_by_model", _perf_by_model)
    monkeypatch.setattr("grid_api.services.recipes.list_recipes", lambda: [_recipe()])

    result = await stats.status_models()

    director = next(item for item in result if item["name"] == "LTX Director 2.0")
    assert director == {
        "name": "LTX Director 2.0",
        "count": 1,
        "type": "video",
        "max_context_length": None,
        "samples": 0,
        "tokens_per_s": None,
        "avg_ttft_s": None,
        "avg_latency_s": None,
        "recipe_backed": True,
    }

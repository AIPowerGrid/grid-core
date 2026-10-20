# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Actual pinned worker adaptation; not native GPU/storage/billing proof."""

import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from grid_api.services import media, recipes


@pytest.mark.parametrize("has_source", [False, True])
def test_moss_graph_reaches_actual_reviewed_worker_unchanged(monkeypatch, has_source):
    source = os.environ.get("MEDIA_VIDEO_WORKER_SOURCE")
    if not source:
        if os.environ.get("CI"):
            pytest.fail("MOSS handoff requires the CI-pinned media-worker checkout")
        pytest.skip("MEDIA_VIDEO_WORKER_SOURCE required for actual worker adaptation")
    worker = Path(source).resolve()
    python = worker / ".venv/bin/python"
    assert python.is_file()
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worker, text=True).strip() == \
        "6b80591350ade4da9f7e052aa2d6dec2810ce560"
    for name in ("_BY_ROOT", "_BY_NAME", "_BY_ID", "_BY_MODEL", "_LOCAL_RECIPES", "_ONCHAIN_RECIPES"):
        monkeypatch.setattr(recipes, name, {})
    recipes.load_local_recipes(str(Path(__file__).resolve().parents[3] / "recipes"))
    model = "MOSS Image SFT BF16"
    inputs = {"prompt": "synthetic scene", "seed": 739, "width": 1344, "height": 768}
    if has_source:
        inputs["denoise"] = 0.7
    resolved = recipes.resolve_for_model(model, inputs, has_source=has_source)
    payload = {"n": 1}
    if has_source:
        payload["source_image_url"] = "https://source.invalid/synthetic.png"
    payload, route_models = media.bind_dispatch_recipe(model, payload, resolved)
    assert route_models == [model]
    code = """
import asyncio,contextlib,json,sys
from bridge import workflow
payload=json.load(sys.stdin)
calls=[]
async def download(url,filename):
    calls.append([url,filename])
    return filename
workflow.download_image=download
async def run():
    with contextlib.redirect_stdout(sys.stderr):
        graph=await workflow.build_recipe_workflow({'id':'synthetic-moss'},payload)
    print(json.dumps({'graph':graph,'downloads':calls}))
asyncio.run(run())
"""
    result = subprocess.run([str(python), "-c", code], cwd=worker, input=json.dumps(payload),
                            capture_output=True, text=True, check=True, timeout=20)
    adapted = json.loads(result.stdout)
    expected = copy.deepcopy(resolved["spec"])
    expected["9"]["inputs"]["filename_prefix"] = "grid_synthetic-moss"
    if has_source:
        expected["10"]["inputs"]["image"] = "src_synthetic-moss.png"
    assert adapted["graph"] == expected
    assert adapted["downloads"] == (
        [["https://source.invalid/synthetic.png", "src_synthetic-moss.png"]] if has_source else []
    )

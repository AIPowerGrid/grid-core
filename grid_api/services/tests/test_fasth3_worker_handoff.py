# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in real Core/worker contract check with synthetic media and mocked HTTP."""

import base64
import io
import json
import os
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from grid_api.services import storage
from grid_api.services.fasth3_plans import compile_candidate


@pytest.mark.parametrize(
    "appearance,opening,mode",
    [
        (False, False, "generated"),
        (True, False, "generated"),
        (False, True, "generated"),
        (True, True, "generated"),
        (True, False, "reference"),
        (False, True, "reference"),
        (True, True, "reference"),
        (True, False, "preserve"),
        (False, True, "preserve"),
        (True, True, "preserve"),
    ],
)
@pytest.mark.asyncio
async def test_actual_core_source_plan_reaches_candidate_worker_without_dispatch(tmp_path, monkeypatch, appearance, opening, mode):
    source = os.environ.get("FASTH3_WORKER_SOURCE")
    if not source:
        pytest.skip("reviewed FASTH3_WORKER_SOURCE is required for FastH3 handoff")
    worker = Path(source).resolve()
    # Keep the venv entry path: resolving its symlink selects bare Python.
    python = Path(os.environ.get("FASTH3_WORKER_PYTHON", str(worker / ".venv/bin/python"))).absolute()
    if not python.is_file() or not (worker / "bridge/fasth3_sources.py").is_file():
        pytest.fail("FastH3 handoff requires its reviewed worker checkout and test Python")

    def picture(size):
        stream = io.BytesIO()
        Image.new("RGB", size, "red").save(stream, "PNG")
        return base64.b64encode(stream.getvalue()).decode()

    bundle = {}
    if appearance:
        bundle["appearance_image"] = picture((64, 96))
    if opening:
        bundle["opening_image"] = picture((768, 1344))
    if mode != "generated":
        sound = io.BytesIO()
        with wave.open(sound, "wb") as writer:
            writer.setparams((2, 2, 48_000, 0, "NONE", "not compressed"))
            writer.writeframes(b"\0" * (282_000 if mode == "preserve" else 96_000) * 4)
        bundle["soundtrack" if mode == "preserve" else "conditioning_audio"] = base64.b64encode(sound.getvalue()).decode()
    plan = compile_candidate(
        {
            "prompt": "A sunny synthetic park.",
            "seed": 0,
            "frames": 141,
            "width": 768,
            "height": 1344,
            "appearance_image": appearance,
            "opening_image": opening,
            "audio_mode": mode,
        },
        bundle,
    )
    content = {}
    endpoint = "https://" + "a" * 32 + ".r2.cloudflarestorage.com"
    bucket = "synthetic-private-sources"
    monkeypatch.setenv("R2_TRANSIENT_ACCOUNT", endpoint)
    monkeypatch.setenv("R2_TRANSIENT_BUCKET", "synthetic-public-outputs")
    monkeypatch.setattr(storage, "private_source_bucket", lambda: bucket)

    def upload(raw, extension):
        url = f"{endpoint}/{bucket}/source/{len(content):032x}.{extension}?signature=synthetic"
        content[url] = base64.b64encode(raw).decode()
        return url

    monkeypatch.setattr(storage, "upload_private_source", upload)
    payload = await plan.stage()
    script = """
import asyncio, base64, copy, json, sys
import httpx, respx
sys.path.insert(0, sys.argv[1])
from bridge import fasth3_sources, source_media
source_media.Settings.COMFYUI_URL = "http://127.0.0.1:8188"
request = json.load(sys.stdin)
body = request["payload"]
original = copy.deepcopy(body)
uploaded = []
def read(request):
    assert "authorization" not in request.headers
    return httpx.Response(200, content=base64.b64decode(content[str(request.url)]))
def upload(request):
    assert "authorization" not in request.headers
    index = len(uploaded)
    source = body["source_media"][index]
    extension = "png" if source["content_type"] == "image/png" else "wav"
    name = "collision-" + str(index) + "." + extension
    uploaded.append(name)
    return httpx.Response(200, json={"name": name, "type": "input", "subfolder": ""})
content = request["content"]
async def run():
    with respx.mock(assert_all_called=True) as routes:
        for url in content:
            routes.get(url).mock(side_effect=read)
        if content:
            routes.post("http://127.0.0.1:8188/upload/image").mock(side_effect=upload)
        identity = "a0666ffe-da9b-4e92-93c1-abf9301e0558"
        graph = await fasth3_sources.build_candidate_workflow(identity, body)
        expected = copy.deepcopy(body["recipe_spec"])
        for index, binding in enumerate(body["recipe_source_bindings"]):
            name, _, field = binding["path"].split(".")
            expected[name]["inputs"][field] = uploaded[index]
        expected["save"]["inputs"]["filename_prefix"] = "grid_" + identity
        assert graph == expected
        assert body == original
        assert len(uploaded) == len(body["source_media"])
asyncio.run(run())
"""
    result = subprocess.run(
        [str(python), "-c", script, str(worker)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHON_DOTENV_DISABLED": "1"},
        input=json.dumps({"payload": payload, "content": content}),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "synthetic FastH3 Core/worker handoff failed"

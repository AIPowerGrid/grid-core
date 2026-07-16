# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Governed ACE-Step V1 audio request contract."""

from __future__ import annotations

import hashlib
import json

DEFAULT_AUDIO_MODEL = "ace-step-v1.5-turbo"
ACE_STEP_RUNTIME_ADAPTER = "ace-step-1.5-api"
ACE_STEP_RUNTIME_DIGEST = "7571cec88e676620b178a6aa03d8326da8381d65a7001d592a5238d8a1b50743"
ACE_STEP_CAPABILITY_TIERS = frozenset(
    {"audio.ace-step.base", "audio.ace-step.standard"},
)
# Ordered deadlines prevent a slow runtime from outliving Core's worker wait or
# the client request. Redis claim heartbeats keep legitimate work reclaim-safe.
AUDIO_RUNTIME_TIMEOUT = 1800
AUDIO_WORKER_TIMEOUT = 1860
AUDIO_TIMEOUT = 1920
# The worker receives upload slots before generation starts. Keep those slots
# valid through the worker deadline plus a bounded upload/clock-skew margin.
AUDIO_UPLOAD_URL_TTL = AUDIO_WORKER_TIMEOUT + 300
MIN_WAV_BYTES = 44
MAX_AUDIO_BYTES = 256 * 1024 * 1024
MIN_AUDIO_SECONDS = 10.0
MAX_AUDIO_SECONDS = 300.0
MIN_INFERENCE_STEPS = 1
MAX_INFERENCE_STEPS = 20

ACE_STEP_RECIPE_SPEC = {
    "adapter": "ace-step-api-audio-v1",
    "fixed": {
        "audio_format": "wav",
        "batch_size": 1,
        "model": "acestep-v15-turbo",
        "sample_mode": False,
        "thinking": False,
        "use_cot_caption": False,
        "use_cot_language": False,
        "use_format": False,
        "use_random_seed": False,
    },
    "limits": {
        "audio_duration": [10, 300],
        "inference_steps": [1, 20],
        "lyrics_chars": 20000,
        "prompt_chars": 2000,
    },
    "variables": ["prompt", "lyrics", "audio_duration", "inference_steps", "seed"],
}
ACE_STEP_RECIPE_ROOT = "21e6572ec04f315bea7089080797f55c7836da0707417b39f505f5ac5b54235f"


def recipe_root(spec=ACE_STEP_RECIPE_SPEC) -> str:
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


if recipe_root() != ACE_STEP_RECIPE_ROOT:  # pragma: no cover - import-time invariant
    raise RuntimeError("ACE-Step recipe root does not match its governed specification")

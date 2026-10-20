# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private H3 candidate planning/staging, outside public or financial admission."""

import hashlib
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from . import fasth3_sources, media_source_retention, video_plans
from .fasth3_contract import MODEL
from .media_jobs import MAX_PAYLOAD_BYTES

SCHEMA = "aipg.fasth3-plan.v1"
SOURCE_SCHEMA = "aipg.fasth3-sources.v1"
FEATURE = "recipe-fasth3-sources-v1"


@dataclass(frozen=True)
class CandidateH3Plan(video_plans.FrozenVideoPlan):
    """Reuse frozen source transport, never synthesize a governed recipe root."""

    def check_inputs(self) -> None:
        super().check_inputs()
        payload = self.payload
        if (
            self.snapshot.get("schema") != SCHEMA
            or self.model != MODEL
            or payload.get("qualification_only") is not True
            or "recipe_root" in payload
            or payload.get("recipe_source_schema") != SOURCE_SCHEMA
            or payload.get("required_media_features") != [FEATURE]
        ):
            raise HTTPException(422, detail="Invalid FastH3 qualification plan")

    def _validate_staged_sources(self, payload: dict[str, Any]) -> None:
        # FrozenVideoPlan.stage independently compares every non-URL source
        # commitment with the fully decoded Core inputs after this check.
        expected = self.snapshot["payload"]["recipe_source_bindings"]
        sources = payload.get("source_media", [])
        if (
            payload.get("recipe_source_bindings") != expected
            or not isinstance(sources, list)
            or any(not isinstance(source, dict) for source in sources)
            or [source.get("role") for source in sources] != [binding["role"] for binding in expected]
        ):
            raise HTTPException(422, detail="FastH3 source roles changed during staging")
        try:
            media_source_retention.capture_urls([source["url"] for source in sources])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(503, detail="Private FastH3 source staging failed") from None


def compile_candidate(inputs: dict[str, Any], inline_sources: dict[str, str]) -> CandidateH3Plan:
    """Freeze validated sources and exact graph without quota/storage/money I/O.

    The plan is not authenticated, account-owned, priced or runtime-qualified.
    Ingress must provide all those authorities before any eventual admission.
    Stage uses existing private uploads; no source URL may enter compilation.
    """
    candidate, sources = fasth3_sources.prepare_candidate(inputs, inline_sources)
    execution = candidate["execution"]
    seed = execution["seed"]
    validated = video_plans.VideoInputs(
        model=MODEL,
        payload_json=video_plans._json({**execution, "n": 1}),
        seeds=(seed,),
        images=(),
        sources=sources,
        timed_images=False,
        private_images=True,
    )
    uncompiled = validated.planning_payload()
    payload = {
        **uncompiled,
        "model": MODEL,
        "execution": execution,
        "recipe_engine": "comfyui",
        "recipe_spec": candidate["spec"],
        "recipe_source_schema": SOURCE_SCHEMA,
        "recipe_source_bindings": candidate["source_bindings"],
        "source_media": uncompiled.get("source_media", []),
        "required_media_features": [FEATURE],
        "qualification_only": True,
    }
    if len(video_plans._json(payload)) > MAX_PAYLOAD_BYTES:
        raise HTTPException(413, detail="FastH3 dispatch exceeds its candidate limit")
    snapshot = video_plans._json(
        {
            "schema": SCHEMA,
            "model": MODEL,
            "payload": payload,
            "route_models": [MODEL],
            "execution": execution,
            "inputs": validated.commitments,
            "uncompiled": uncompiled,
        },
    )
    result = CandidateH3Plan(MODEL, snapshot, hashlib.sha256(snapshot.encode("ascii")).hexdigest(), validated)
    result.check_inputs()
    return result

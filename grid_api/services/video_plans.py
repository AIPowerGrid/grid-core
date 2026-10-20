# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private video planning before storage or economic side effects.

This is an in-memory compiled execution snapshot, not a payment quote, durable
authorization or public route. Source bytes never enter the compiled snapshot.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import HTTPException

from . import media, storage, video_sources
from .media_jobs import MAX_PAYLOAD_BYTES

MAX_PLAN_BYTES = 2 * 1024 * 1024
_SOURCE_ORIGIN = "https://video-plan.invalid"


def _json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(encoded) > MAX_PLAN_BYTES:
        raise HTTPException(413, detail="Compiled video plan exceeds its limit")
    return encoded


@dataclass(frozen=True)
class VideoInputs:
    """Validated inputs; payload views are copies and raw bytes are private."""

    model: str
    payload_json: str = field(repr=False)
    seeds: tuple[int, ...]
    images: tuple[tuple[bytes, str, int, int], ...] = field(repr=False)
    sources: tuple[video_sources.Source, ...] = field(repr=False)
    timed_images: bool
    private_images: bool

    @property
    def payload(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.payload_json))

    @property
    def commitments(self) -> dict[str, Any]:
        return {
            "images": [
                {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "ext": ext, "width": width, "height": height}
                for raw, ext, width, height in self.images
            ],
            "sources": [source.commitment for source in self.sources],
        }

    def planning_payload(self) -> dict[str, Any]:
        """Slots are non-routable placeholders, never dispatchable URLs."""
        payload = self.payload
        if self.images:
            urls = [f"{_SOURCE_ORIGIN}/image/{index}" for index in range(len(self.images))]
            payload["source_image_url"] = urls[0]
            if self.timed_images:
                payload["source_image_urls"] = urls
        if self.sources:
            payload["source_media"] = [{**source.commitment, "url": f"{_SOURCE_ORIGIN}/{source.role}"} for source in self.sources]
        return payload


@dataclass(frozen=True)
class FrozenVideoPlan:
    """Immutable seed/graph/units/source commitment, without payment authority."""

    model: str
    snapshot_json: str = field(repr=False)
    sha256: str
    inputs: VideoInputs = field(repr=False)

    @property
    def snapshot(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.snapshot_json))

    @property
    def payload(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.snapshot["payload"])

    @property
    def route_models(self) -> list[str]:
        return cast(list[str], self.snapshot["route_models"])

    def check_inputs(self) -> None:
        """Reject changed inputs before quota or even the first upload."""
        snapshot = self.snapshot
        if (
            hashlib.sha256(self.snapshot_json.encode("ascii")).hexdigest() != self.sha256
            or snapshot["model"] != self.model
            or self.inputs.model != self.model
            or self.inputs.private_images is not True
            or _json(list(self.inputs.seeds)) != _json([snapshot["payload"]["seed"]])
            or _json(snapshot["inputs"]) != _json(self.inputs.commitments)
            or _json(snapshot["uncompiled"]) != _json(self.inputs.planning_payload())
        ):
            raise HTTPException(422, detail="Video inputs changed after planning")

    def _validate_staged_sources(self, payload: dict[str, Any]) -> None:
        if payload.get("source_media"):
            video_sources.validate_commitments(payload["source_media"], payload["recipe_source_bindings"], self.snapshot["execution"])

    async def stage(self) -> dict[str, Any]:
        """Bind private uploads to the frozen graph, without re-resolving it."""
        self.check_inputs()
        staged = await stage_inputs(self.inputs)
        payload = self.payload
        # Only storage locations may change; prompt, graph, seed and units are
        # already fixed. The worker binds slots using these separate URL fields.
        for key in ("source_image_url", "source_image_urls", "source_media"):
            if key in staged:
                payload[key] = staged[key]
        urls = list(payload.get("source_image_urls") or [])
        if payload.get("source_image_url"):
            urls.append(payload["source_image_url"])
        urls.extend(source["url"] for source in payload.get("source_media", []))
        for value in urls:
            try:
                url = urlsplit(value)
                if (
                    not isinstance(value, str)
                    or not 0 < len(value) <= 4096
                    or url.scheme != "https"
                    or not url.hostname
                    or url.hostname == "video-plan.invalid"
                    or url.username
                    or url.password
                    or url.fragment
                    or url.port not in {None, 443}
                ):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(503, detail="Private video source staging failed") from None
        self._validate_staged_sources(payload)
        observed = [{key: value for key, value in source.items() if key != "url"} for source in payload.get("source_media", [])]
        if _json(observed) != _json(self.snapshot["inputs"]["sources"]):
            raise HTTPException(422, detail="Video sources changed during staging")
        return payload


def compile_video(inputs: VideoInputs) -> FrozenVideoPlan:
    """Pure governed compilation: no quota, upload, worker call or monetary I/O."""
    uncompiled = inputs.planning_payload()
    seed = uncompiled.get("seed")
    if (
        type(seed) is not int
        or not 0 <= seed <= media.MAX_SEED
        or inputs.seeds != (seed,)
        or uncompiled.get("n") != 1
        or not inputs.private_images
        or len(inputs.images) > 16
        or len(inputs.images) > 1
        and not inputs.timed_images
    ):
        raise HTTPException(422, detail="Video planning requires validated single-output inputs and a fixed seed")
    payload, recipe = media.compile_dispatch(inputs.model, "video", uncompiled)
    if recipe is None or not recipe.get("execution"):
        raise HTTPException(422, detail="Video planning requires a governed typed recipe")
    payload, route_models = media.bind_dispatch_recipe(inputs.model, payload, recipe)
    if len(_json(payload)) > MAX_PAYLOAD_BYTES:
        raise HTTPException(413, detail="Compiled video dispatch exceeds its limit")
    snapshot = _json(
        {
            "schema": "aipg.video-plan.v1",
            "model": inputs.model,
            "payload": payload,
            "route_models": route_models,
            "execution": recipe["execution"],
            "inputs": inputs.commitments,
            "uncompiled": uncompiled,
        },
    )
    return FrozenVideoPlan(inputs.model, snapshot, hashlib.sha256(snapshot.encode("ascii")).hexdigest(), inputs)


async def stage_inputs(inputs: VideoInputs) -> dict[str, Any]:
    """Upload previously validated sources; quota and finance belong to ingress."""
    payload = inputs.payload
    sources = [video_sources.Source(source.role, source.data, source.ext, dict(source.metadata)) for source in inputs.sources]
    if inputs.private_images and (inputs.images or inputs.sources):
        storage.private_source_bucket()
    if inputs.images:
        if inputs.timed_images:
            urls = await media.upload_video_keyframes(list(inputs.images))
            payload["source_image_urls"] = urls
            payload["source_image_url"] = urls[0]
        else:
            raw, ext, _width, _height = inputs.images[0]
            uploader = storage.upload_private_source if inputs.private_images else storage.upload_source
            payload["source_image_url"] = await asyncio.to_thread(uploader, raw, ext)
    if sources:
        payload["source_media"] = await video_sources.upload_sources(sources)
    return payload

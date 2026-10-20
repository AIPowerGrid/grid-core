# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline bounded FastH3 source preparation, with no upload or admission effect."""

import io
import struct
import wave

from fastapi import HTTPException
from PIL import Image

from .fasth3_contract import compile_candidate
from .video_sources import MAX_AUDIO_BYTES, MAX_IMAGE_BYTES, Source, decode_inline

IMAGE_ROLES = {"appearance_image", "opening_image"}
AUDIO_ROLES = {"conditioning_audio", "soundtrack"}
MAX_ENCODED_BUNDLE = 4 * (2 * MAX_IMAGE_BYTES + MAX_AUDIO_BYTES) // 3 + 256


def _image(raw: bytes, role: str, width: int, height: int) -> Source:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_IMAGE_BYTES:
        raise ValueError("invalid image size")
    with Image.open(io.BytesIO(raw), formats=["PNG"]) as image:
        w, h = image.size
        if (
            not 32 <= w <= 2048
            or not 32 <= h <= 2048
            or w * h > 2048**2
            or image.mode not in {"RGB", "RGBA"}
            or (role == "opening_image" and (w, h) != (width, height))
        ):
            raise ValueError("invalid image geometry")
        image.verify()
    with Image.open(io.BytesIO(raw), formats=["PNG"]) as image:
        if image.n_frames != 1 or image.getexif().get(274, 1) != 1:
            raise ValueError("animated or rotated source image")
        image.load()
        if image.mode == "RGBA" and image.getchannel("A").getextrema() != (255, 255):
            raise ValueError("transparent sources require explicit prior flattening")
    return Source(role, raw, "png", {"width": w, "height": h})


def _audio(raw: bytes, role: str, frames: int) -> Source:
    # Canonical PCM WAV only: no metadata/extended/concatenated RIFF chunks.
    if (
        type(raw) is not bytes
        or not 44 <= len(raw) <= MAX_AUDIO_BYTES
        or raw[:4] != b"RIFF"
        or raw[8:12] != b"WAVE"
        or raw[12:16] != b"fmt "
        or raw[36:40] != b"data"
        or struct.unpack_from("<I", raw, 4)[0] != len(raw) - 8
        or struct.unpack_from("<I", raw, 16)[0] != 16
        or struct.unpack_from("<I", raw, 40)[0] != len(raw) - 44
    ):
        raise ValueError("invalid canonical WAV")
    with wave.open(io.BytesIO(raw)) as reader:
        samples = reader.getnframes()
        if (
            (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getcomptype()) != (2, 2, 48_000, "NONE")
            or len(raw) != 44 + samples * 4
            or struct.unpack_from("<I", raw, 28)[0] != 192_000
            or struct.unpack_from("<H", raw, 32)[0] != 4
            or len(reader.readframes(samples + 1)) != samples * 4
        ):
            raise ValueError("invalid PCM audio")
    if role == "soundtrack":
        if samples != frames * 2000:
            raise ValueError("soundtrack must cover the exact target duration")
    elif not 2 * 48_000 <= samples <= 15 * 48_000:
        raise ValueError("audio reference must last between two and fifteen seconds")
    units = {"sample_rate": 48_000, "channels": 2, "samples": samples}
    if role == "soundtrack":
        units.update(frames=frames, fps=24)
    return Source(role, raw, "wav", units)


def prepare_candidate(values: dict, inline_sources: dict) -> tuple[dict, tuple[Source, ...]]:
    """Validate every named source before handing any bytes to a future uploader.

    Appearance is not frame zero; reference audio is not a frozen soundtrack.
    Inputs are already-normalized inline PNG/PCM WAV, never URLs or filenames.
    No user/account ownership, private storage or worker permission is granted.
    """
    plan = compile_candidate(values)
    expected = [binding["role"] for binding in plan["source_bindings"]]
    try:
        if (
            type(inline_sources) is not dict
            or set(inline_sources) != set(expected)
            or not all(type(value) is str for value in inline_sources.values())
            or sum(len(value) for value in inline_sources.values()) > MAX_ENCODED_BUNDLE
        ):
            raise ValueError("source roles or bounds disagree")
        sources = []
        execution = plan["execution"]
        for role in expected:
            image = role in IMAGE_ROLES
            if not image and role not in AUDIO_ROLES:
                raise ValueError("unknown source role")
            raw = decode_inline(inline_sources[role], "reference_image" if image else "reference_audio")
            sources.append(_image(raw, role, execution["width"], execution["height"]) if image else _audio(raw, role, execution["frames"]))
    except (ValueError, TypeError, OSError, SyntaxError, EOFError, wave.Error, struct.error, Image.DecompressionBombError, HTTPException):
        raise ValueError("Invalid FastH3 qualification sources") from None
    return plan, tuple(sources)

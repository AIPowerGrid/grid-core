# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real synthetic PNG/WAV parsing, not native model or public upload proof."""

import base64
import hashlib
import io
import struct
import wave

import pytest
from PIL import Image

from grid_api.services import fasth3_sources as sources
from grid_api.services import storage


def inline(raw):
    return base64.b64encode(raw).decode("ascii")


def png(size=(64, 96), **options):
    stream = io.BytesIO()
    Image.new("RGB", size, "red").save(stream, "PNG", **options)
    return stream.getvalue()


def wav(samples=248_000, *, channels=2, rate=48_000, width=2):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        writer.writeframes(b"\0" * samples * channels * width)
    return stream.getvalue()


@pytest.fixture(autouse=True)
def forbid_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("offline source validation must not upload or select storage")

    for name in ("upload_source", "upload_private_source", "private_source_bucket"):
        monkeypatch.setattr(storage, name, forbidden)


def prepare(values=None, bundle=None):
    return sources.prepare_candidate({"prompt": "A sunny park.", **(values or {})}, bundle or {})


def test_generated_plan_needs_no_sources_and_never_uploads():
    plan, assets = prepare()
    assert plan["qualification_only"] is True
    assert assets == ()
    assert plan["execution"]["output_audio_sample_rate"] == 32_000


def test_appearance_opening_and_soundtrack_are_separate_hash_bound_roles():
    picture, scene, sound = png(), png((1344, 768)), wav()
    plan, assets = prepare(
        {"appearance_image": True, "opening_image": True, "audio_mode": "preserve"},
        {"appearance_image": inline(picture), "opening_image": inline(scene), "soundtrack": inline(sound)},
    )
    assert [asset.role for asset in assets] == ["appearance_image", "opening_image", "soundtrack"]
    assert [asset.data for asset in assets] == [picture, scene, sound]
    for asset in assets:
        assert asset.commitment["sha256"] == hashlib.sha256(asset.data).hexdigest()
        assert asset.commitment["bytes"] == len(asset.data)
    assert assets[0].metadata == {"width": 64, "height": 96}
    assert assets[1].metadata == {"width": 1344, "height": 768}
    assert assets[2].metadata == {"sample_rate": 48_000, "channels": 2, "samples": 248_000, "frames": 124, "fps": 24}
    assert plan["execution"]["output_audio_sample_rate"] == 48_000
    assert plan["execution"]["output_audio_channels"] == 2


@pytest.mark.parametrize("samples", [96_000, 144_000, 720_000])
def test_voice_reference_duration_is_independent_of_target_video(samples):
    _, assets = prepare(
        {"appearance_image": True, "audio_mode": "reference"},
        {"appearance_image": inline(png()), "conditioning_audio": inline(wav(samples))},
    )
    assert assets[1].metadata["samples"] == samples
    assert "frames" not in assets[1].metadata


@pytest.mark.parametrize("frames", [124, 141, 243, 362])
def test_preserved_audio_uses_h3_exact_frames_not_ltx_grid(frames):
    _, assets = prepare(
        {"appearance_image": True, "audio_mode": "preserve", "frames": frames},
        {"appearance_image": inline(png()), "soundtrack": inline(wav(frames * 2000))},
    )
    assert assets[1].metadata["samples"] == frames * 2000


@pytest.mark.parametrize(
    "bundle",
    [
        {"appearance_image": "https://example.invalid/image.png"},
        {"appearance_image": "local.png"},
        {"appearance_image": "%%%"},
        {"appearance_image": "data:audio/wav;base64,YWJj"},
        {"appearance_image": ""},
        {"appearance_image": b"abc"},
        {"opening_image": "YWJj"},
        {"appearance_image": "YWJj", "soundtrack": "YWJj"},
        {},
    ],
)
def test_missing_extra_remote_wrong_role_or_malformed_source_rejects(bundle):
    with pytest.raises(ValueError, match="Invalid FastH3 qualification sources"):
        prepare({"appearance_image": True}, bundle)


@pytest.mark.parametrize("bad", [b"", b"not a PNG", png()[:-8], png((16, 96)), png((2049, 32))])
def test_image_bounds_and_complete_decode(bad):
    with pytest.raises(ValueError):
        prepare({"appearance_image": True}, {"appearance_image": inline(bad)})


def test_opening_size_cannot_silently_crop_an_appearance_portrait():
    with pytest.raises(ValueError):
        prepare({"opening_image": True}, {"opening_image": inline(png())})


@pytest.mark.parametrize("alpha", [0, 128, 255])
def test_transparency_is_not_silently_dropped_by_the_model_loader(alpha):
    stream = io.BytesIO()
    Image.new("RGBA", (64, 96), (255, 0, 0, alpha)).save(stream, "PNG")
    if alpha == 255:
        _, assets = prepare({"appearance_image": True}, {"appearance_image": inline(stream.getvalue())})
        assert len(assets) == 1
    else:
        with pytest.raises(ValueError):
            prepare({"appearance_image": True}, {"appearance_image": inline(stream.getvalue())})


@pytest.mark.parametrize("case", ["short", "long", "mono", "rate", "width", "truncated", "trailing", "invalid"])
def test_target_audio_must_be_complete_canonical_exact_duration_pcm(case):
    factories = {
        "short": lambda: wav(247_999),
        "long": lambda: wav(248_001),
        "mono": lambda: wav(channels=1),
        "rate": lambda: wav(rate=32_000),
        "width": lambda: wav(width=1),
        "truncated": lambda: wav()[:-1],
        "trailing": lambda: wav() + b"extra",
        "invalid": lambda: b"not audio",
    }
    bad = factories[case]()
    with pytest.raises(ValueError):
        prepare({"appearance_image": True, "audio_mode": "preserve"}, {"appearance_image": inline(png()), "soundtrack": inline(bad)})


@pytest.mark.parametrize("samples", [95_999, 720_001])
def test_reference_audio_bounds_do_not_silently_trim(samples):
    with pytest.raises(ValueError):
        prepare(
            {"appearance_image": True, "audio_mode": "reference"},
            {"appearance_image": inline(png()), "conditioning_audio": inline(wav(samples))},
        )


def test_wav_transport_fields_cannot_lie_about_byte_rate():
    bad = bytearray(wav())
    struct.pack_into("<I", bad, 28, 1)
    with pytest.raises(ValueError):
        prepare({"appearance_image": True, "audio_mode": "preserve"}, {"appearance_image": inline(png()), "soundtrack": inline(bytes(bad))})


def test_encoded_bundle_bound_precedes_decoder_admission(monkeypatch):
    monkeypatch.setattr(sources, "MAX_ENCODED_BUNDLE", 64)

    def forbidden(*_args, **_kwargs):
        pytest.fail("oversized input must reject before decoding")

    monkeypatch.setattr(sources, "decode_inline", forbidden)
    with pytest.raises(ValueError):
        prepare({"appearance_image": True}, {"appearance_image": "a" * 65})


def test_animated_png_is_not_a_single_reference_frame():
    stream = io.BytesIO()
    Image.new("RGB", (64, 96), "red").save(stream, "PNG", save_all=True, append_images=[Image.new("RGB", (64, 96), "blue")], duration=100)
    with pytest.raises(ValueError):
        prepare({"appearance_image": True}, {"appearance_image": inline(stream.getvalue())})


def test_rotated_metadata_is_not_silently_transformed():
    exif = Image.Exif()
    exif[274] = 6
    with pytest.raises(ValueError):
        prepare({"appearance_image": True}, {"appearance_image": inline(png(exif=exif))})


@pytest.mark.parametrize("role", ["appearance_image", "soundtrack"])
def test_data_uri_is_accepted_only_with_the_exact_role_mime(role):
    picture = "data:image/png;base64," + inline(png())
    sound = "data:audio/wav;base64," + inline(wav())
    values = {"appearance_image": True, "audio_mode": "preserve"}
    bundle = {"appearance_image": picture, "soundtrack": sound}
    _, assets = prepare(values, bundle)
    assert len(assets) == 2
    bundle[role] = bundle[role].replace("image/png", "image/jpeg").replace("audio/wav", "audio/mpeg")
    with pytest.raises(ValueError):
        prepare(values, bundle)

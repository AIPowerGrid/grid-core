# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exact public price-book contracts."""

import pytest

from grid_api.services import den, pricing


def test_unknown_text_uses_published_default_without_reward_promotion():
    model = "new-unverified-9999b"
    assert pricing.get_price(model) is None
    assert pricing.is_priced_for(model, "text")
    assert pricing.quote_text(model, 1_000_000, 1_000_000) == 375_000
    assert pricing.quote_text(model, 1, 0) == 1
    assert pricing.public_text_price(model) == pricing.public_catalog()["price_book"]["default_text"]
    assert model not in den.MODEL_REGISTRY
    assert den.estimate_model_multiplier(model) == den.DEFAULT_MULTIPLIER
    assert model not in pricing.PRICING
    for modality in ("image", "video", "audio", "3d"):
        assert not pricing.is_priced_for(model, modality)
    assert pricing.quote_image(model) == 0


@pytest.mark.parametrize("model", ["", "  ", "auto", "auto:fast", "Krea 2 Turbo", "LTX Director 2.0"])
def test_default_does_not_make_unresolved_or_media_models_text_priced(model):
    assert not pricing.is_priced_for(model, "text")
    assert pricing.quote_text(model, 100, 100) == 0
    assert pricing.public_text_price(model) is None


def test_explicit_rate_including_zero_wins_over_default(monkeypatch):
    assert pricing.quote_text("smollm-135m", 1_000_000, 1_000_000) == 15_000
    assert pricing.public_text_price(" DEEPSEEK-V4-FLASH-NVFP4 ")["source"] == "model"
    monkeypatch.setitem(pricing.PRICING, "disabled-text", pricing.ModelPrice(0, 0))
    assert not pricing.is_priced_for("disabled-text", "text")


def test_qwen_next_has_explicit_text_only_launch_price():
    model = "qwen38-flash-next-125b-nvfp4"
    assert pricing.is_priced_for(model, "text")
    assert pricing.quote_text(model, 1_000_000, 0) == 75_000
    assert pricing.quote_text(model, 0, 1_000_000) == 300_000
    assert pricing.quote_text(model.upper(), 1, 0) == 1
    assert pricing.quote_text(f" {model} ", 0, 1) == 1
    for modality in ("image", "video", "audio", "3d"):
        assert not pricing.is_priced_for(model, modality)
    assert pricing.get_price(model + "-unknown") is None


def test_turbo_image_launch_prices_are_locked():
    assert pricing.quote_image("z-image-turbo") == 3_000
    assert pricing.quote_image("Krea 2 Turbo") == 5_000


def test_turbo_image_prices_scale_per_output():
    assert pricing.quote_image("Z-IMAGE-TURBO", 4) == 12_000
    assert pricing.quote_image("krea 2 turbo", 4) == 20_000


def test_moss_has_explicit_image_only_price_without_implicit_aliases():
    model = "MOSS Image SFT BF16"
    assert pricing.is_priced_for(model, "image")
    assert pricing.quote_image(model) == 10_000
    assert pricing.quote_image(model.upper(), 4) == 40_000
    for modality in ("text", "video", "audio", "3d"):
        assert not pricing.is_priced_for(model, modality)
    for unknown in ("MOSS Image", "MOSS Image Base BF16", "MOSS Image SFT INT8"):
        assert pricing.get_price(unknown) is None
        assert not pricing.is_priced_for(unknown, "image")
    catalog = pricing.public_catalog()
    row = next(item for item in catalog["price_book"]["models"] if item["model"] == model.lower())
    assert row["rates"]["per_image_usd"] == 0.01
    assert all(item["model"] != model.lower() for item in catalog["comparison_evidence"]["items"])


@pytest.mark.parametrize("model,costs", [
    ("LTX-2.5 Distilled BF16", (67_500, 280_834, 1_200_000)),
    ("LTX-2.5 Distilled NVFP4", (33_750, 140_417, 600_000)),
])
def test_distilled_video_tariff_uses_effective_output_duration(model, costs):
    assert pricing.is_priced_for(model, "video")
    assert pricing.quote_video(model, 81 / 24) == costs[0]
    assert pricing.quote_video(model, 337 / 24) == costs[1]
    assert pricing.quote_video(model, 60) == costs[2]
    for modality in ("text", "image", "audio", "3d"):
        assert not pricing.is_priced_for(model, modality)
    for unapproved in ("LTX-2.5 Dev BF16", "LTX-2.5 Dev BF16 Ingredients", "LTX-2.5"):
        assert pricing.get_price(unapproved) is None
        assert not pricing.is_priced_for(unapproved, "video")


@pytest.mark.parametrize("model,rate", [
    ("ltx-2.5 distilled bf16", 0.02),
    ("ltx-2.5 distilled nvfp4", 0.01),
])
def test_distilled_video_catalog_does_not_claim_availability_or_comparison(model, rate):
    catalog = pricing.public_catalog()
    row = next(item for item in catalog["price_book"]["models"]
               if item["model"] == model)
    assert row["rates"]["per_video_second_usd"] == rate
    assert all(row["rates"][key] is None for key in row["rates"]
               if key != "per_video_second_usd")
    assert all(item["model"] != row["model"]
               for item in catalog["comparison_evidence"]["items"])

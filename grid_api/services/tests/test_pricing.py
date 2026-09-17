# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exact public price-book contracts."""

from grid_api.services import pricing
from grid_api.services import den
import pytest


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

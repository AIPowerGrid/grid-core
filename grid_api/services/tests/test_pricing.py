# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exact public price-book contracts."""

from grid_api.services import pricing


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

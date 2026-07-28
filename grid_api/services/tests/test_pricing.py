# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exact public price-book contracts."""

from grid_api.services import pricing


def test_turbo_image_launch_prices_are_locked():
    assert pricing.quote_image("z-image-turbo") == 3_000
    assert pricing.quote_image("Krea 2 Turbo") == 5_000


def test_turbo_image_prices_scale_per_output():
    assert pricing.quote_image("Z-IMAGE-TURBO", 4) == 12_000
    assert pricing.quote_image("krea 2 turbo", 4) == 20_000

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from scripts.grant_canary_credit import amount_to_micro


def test_canary_amount_is_exact_and_bounded():
    assert amount_to_micro("0.000001") == 1
    assert amount_to_micro("10") == 10_000_000
    with pytest.raises(ValueError):
        amount_to_micro("0")
    with pytest.raises(ValueError):
        amount_to_micro("10.000001")
    with pytest.raises(ValueError):
        amount_to_micro("1.0000001")

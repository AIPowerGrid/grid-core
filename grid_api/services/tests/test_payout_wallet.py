# SPDX-License-Identifier: AGPL-3.0-or-later
"""Payout-wallet address validation — the only guard on a no-proof payout address.

We don't prove control (mining-style), but we DO reject malformed addresses so a
typo can't silently send earnings into a black hole."""

from grid_api.services.accounts import is_valid_eth_address


def test_accepts_well_formed_addresses():
    assert is_valid_eth_address("0x" + "a" * 40)
    assert is_valid_eth_address("0x52908400098527886E0F7030069857D2E4169EE7")  # checksum-cased
    assert is_valid_eth_address("  0x" + "0" * 40 + "  ")  # trims whitespace


def test_rejects_malformed_addresses():
    assert not is_valid_eth_address("")
    assert not is_valid_eth_address("0x123")                 # too short
    assert not is_valid_eth_address("0x" + "a" * 41)         # too long
    assert not is_valid_eth_address("a" * 40)                # missing 0x
    assert not is_valid_eth_address("0x" + "g" * 40)         # non-hex
    assert not is_valid_eth_address("not-an-address")

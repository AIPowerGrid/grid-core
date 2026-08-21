# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from grid_api import safe_logging


def test_opaque_id_is_stable_bounded_and_does_not_expose_input():
    value = "0x1234567890abcdef1234567890abcdef12345678"

    first = safe_logging.opaque_id(value)

    assert first == safe_logging.opaque_id(value)
    assert len(first) == 18
    assert value not in first
    assert first != safe_logging.opaque_id(value + "1")


def test_opaque_id_handles_empty_values():
    assert safe_logging.opaque_id(None) == "-"
    assert safe_logging.opaque_id("") == "-"


def test_error_type_never_returns_exception_message():
    exc = RuntimeError("secret-value")

    assert safe_logging.error_type(exc) == "RuntimeError"
    assert "secret-value" not in safe_logging.error_type(exc)

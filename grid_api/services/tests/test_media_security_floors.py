# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Installed security floors and bounded parser/trust-store regression checks."""

import io
import ssl
from importlib.metadata import version

import certifi
import pytest
from packaging.version import Version
from PIL import Image


@pytest.mark.parametrize("package,floor", [("pillow", "13.0.0"), ("certifi", "2026.10.19")])
def test_installed_media_transport_security_floor(package, floor):
    assert Version(version(package)) >= Version(floor)


def test_rejected_gif_frame_never_poisoned_the_surviving_canvas():
    # GHSA-65qm-jvmh-6pqg: inspect state only; never load the rejected huge frame.
    assert Version(version("pillow")) >= Version("13.0.0")
    raw = bytes.fromhex(
        "47494638396101000100800000000000ffffff2c00000000010001000002024401002c00000000409c409c002c00000000010001000002024401003b",
    )
    with Image.open(io.BytesIO(raw), formats=["GIF"]) as image:
        assert image.size == (1, 1)
        with pytest.raises(Image.DecompressionBombError):
            image.seek(1)
        assert image.size == (1, 1)


def test_updated_ca_bundle_remains_a_usable_tls_trust_store():
    context = ssl.create_default_context(cafile=certifi.where())
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.cert_store_stats()["x509_ca"] > 0

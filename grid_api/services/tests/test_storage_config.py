# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from grid_api.services import storage


def test_media_bucket_requires_explicit_configuration(monkeypatch):
    monkeypatch.delenv("R2_TRANSIENT_BUCKET", raising=False)

    with pytest.raises(RuntimeError, match="R2_TRANSIENT_BUCKET not configured"):
        storage.media_bucket()


def test_media_bucket_returns_configured_name(monkeypatch):
    monkeypatch.setenv("R2_TRANSIENT_BUCKET", "configured-media-bucket")

    assert storage.media_bucket() == "configured-media-bucket"

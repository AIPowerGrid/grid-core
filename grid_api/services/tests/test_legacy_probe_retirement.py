# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import importlib
from unittest.mock import AsyncMock

import pytest

from grid_api.services import job_queue, probe


@pytest.mark.asyncio
async def test_legacy_probe_flag_cannot_reopen_unbilled_dispatch(monkeypatch):
    monkeypatch.setenv("GRID_PROBE_ENABLED", "1")
    monkeypatch.setenv("GRID_PROBE_INTERVAL", "1")
    retired = importlib.reload(probe)
    submit = AsyncMock(side_effect=AssertionError("Legacy probe must not queue ordinary work"))
    monkeypatch.setattr(job_queue, "submit_job", submit)
    with pytest.raises(RuntimeError, match="retired"):
        await retired._run_job("model", "prompt")
    with pytest.raises(RuntimeError, match="retired"):
        await retired.run_once()
    await retired.probe_loop()
    submit.assert_not_awaited()

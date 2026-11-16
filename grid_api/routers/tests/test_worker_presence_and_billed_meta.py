# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Billed quantity on the chat reply and `last_seen` for a connected worker.

Statement shape only; no live database, worker or settlement.
"""

from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from grid_api.routers import worker_ws


def test_billed_completion_is_added_without_touching_provenance():
    meta = {"worker": "rig", "gen_time": 1.2, "ttft": 0.1, "tokens_per_s": 40.0}

    billed = worker_ws._with_billed_completion(meta, 387)

    assert billed == {**meta, "billed_completion_tokens": 387}
    assert "billed_completion_tokens" not in meta
    assert worker_ws._with_billed_completion(None, 0) == {"billed_completion_tokens": 0}


class _Session:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def execute(self, statement):
        self.log.append(("execute", statement))

    async def commit(self):
        self.log.append(("commit", None))


@pytest.mark.asyncio
async def test_touch_updates_only_that_workers_last_seen(monkeypatch):
    log = []

    async def new_session():
        return _Session(log)

    monkeypatch.setattr(worker_ws, "new_session", new_session)
    worker_id = "5c0d6f0e-9a53-4d0c-8a38-0b1f8a2f6a11"
    before = datetime.utcnow()

    await worker_ws._touch_last_seen(worker_id)

    assert [step for step, _ in log] == ["execute", "commit"]
    compiled = log[0][1].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("UPDATE grid_workers SET last_seen=") and "WHERE grid_workers.id = " in sql
    assert set(compiled.params) == {"last_seen", "id_1"}
    assert compiled.params["id_1"] == UUID(worker_id)
    assert before <= compiled.params["last_seen"] <= before + timedelta(seconds=5)


def test_last_seen_is_refreshed_about_once_a_minute():
    # The keepalive ticks every 20 seconds.
    assert 20 * worker_ws.LAST_SEEN_REFRESH_TICKS == 60

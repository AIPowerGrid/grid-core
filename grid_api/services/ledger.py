# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Append-only ledger writes (grid_ledger) — the v2 source of truth.

One event per completed job: who did the work, what it earned (den), and the
content hashes that make the work attestable. The settlement bot Merkle-izes
each epoch's events and anchors the root on-chain; nothing here is ever
updated or deleted.

Hash semantics (v1 of receipts):
  prompt_hash  — sha256 of the canonicalized request payload, computed
                 server-side (we saw the request).
  result_hash  — text: sha256 of the full output, computed server-side.
                 media: worker-reported sha256 of the uploaded bytes (the
                 server never touches media bytes — uploads go direct to R2).
                 Verifiable after the fact by fetching the object.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

import sqlalchemy as sa

from ..database import new_session
from ..v2.schema import ledger as ledger_table

logger = logging.getLogger("grid_api.ledger")


def canonical_hash(obj) -> str:
    """sha256 over a canonical JSON encoding (stable key order)."""
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode()).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def record_completion(
    *,
    job_id: str,
    worker_id: str,
    wallet: str,
    model: str,
    job_type: str,
    den: float,
    output_units: int,
    prompt_hash: str | None,
    result_hash: str | None,
) -> None:
    """Append one completion event. Failure is logged, never raised — a
    ledger hiccup must not fail the job the client already got."""
    try:
        async with await new_session() as session:
            await session.execute(
                sa.insert(ledger_table).values(
                    job_id=job_id,
                    worker_id=worker_id,
                    wallet=wallet or None,
                    model=model,
                    job_type=job_type,
                    den=den,
                    output_units=output_units,
                    prompt_hash=prompt_hash,
                    result_hash=result_hash,
                    created=datetime.now(timezone.utc),
                )
            )
            await session.commit()
    except Exception as e:
        logger.error(f"Ledger write failed for job {job_id}: {e}", exc_info=True)

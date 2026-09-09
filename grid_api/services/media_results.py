# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private recovery of billed media outputs; never dispatches or retries work."""

import json
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from ..database import new_session
from ..v2.schema import reservations
from .identities import account_family_ids

MAX_RESULT_BYTES = 64 * 1024


class MediaOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: HttpUrl
    key: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int | None = None


class MediaResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    media: list[MediaOutput] = Field(min_length=1, max_length=4)
    model: str = Field(min_length=1, max_length=255)
    worker: str = Field(max_length=255)
    gen_time: float = Field(ge=0)
    recipe_root: str | None = Field(default=None, max_length=255)


def validate_result(value: dict) -> dict:
    # Bound before parsing/serializing nested fields and refuse NaN in durable JSON.
    if len(json.dumps(value, allow_nan=False).encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("media result exceeds recovery bound")
    return MediaResult.model_validate(value).model_dump(mode="json")


class AmbiguousResult(ValueError):
    pass


async def recover(account_id, *, job_id: UUID | None = None,
                  client_ref: str | None = None) -> dict | None:
    """Read one owner's result, including proved merged-account aliases.

    A client ref is correlation, NOT an idempotency key or a bearer capability.
    Multiple matches are ambiguous; never choose whichever happened to finish.
    """
    if not account_id:
        return None
    if (job_id is None) == (client_ref is None):
        raise ValueError("exactly one media result selector is required")
    async with await new_session() as session:
        family = await account_family_ids(account_id, session=session)
        selector = (reservations.c.job_id == str(job_id) if job_id is not None
                    else reservations.c.media_client_ref == client_ref)
        rows = (await session.execute(sa.select(
            reservations.c.job_id, reservations.c.status, reservations.c.actual_micro,
            reservations.c.media_result,
        ).where(reservations.c.account_id.in_(family), selector).limit(2))).mappings().all()
    if not rows:
        return None
    if len(rows) != 1:
        raise AmbiguousResult("client reference identifies multiple jobs")
    row = rows[0]
    result = row["media_result"]
    if result is not None:
        if row["status"] != "settled":
            raise ValueError("uncommitted media result")
        result = validate_result(result)
    state = ("completed" if result is not None else
             "pending" if row["status"] == "held" else "closed_without_result")
    return {"job_id": row["job_id"], "state": state,
            "actual_micro": row["actual_micro"], "result": result}

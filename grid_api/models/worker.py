# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pydantic models for the WebSocket worker protocol."""

from typing import Literal, Optional

from pydantic import BaseModel


# ── Server → Worker messages ──

class WorkerReady(BaseModel):
    type: Literal["ready"] = "ready"
    worker_id: str


class WorkerJob(BaseModel):
    type: Literal["job"] = "job"
    id: str  # job/procgen ID
    model: str
    payload: dict  # prompt, max_length, temperature, etc.


class WorkerAck(BaseModel):
    type: Literal["ack"] = "ack"
    id: str
    den: float  # den (kudos) awarded


class WorkerPing(BaseModel):
    type: Literal["ping"] = "ping"


class WorkerNoJob(BaseModel):
    type: Literal["no_job"] = "no_job"


class WorkerError(BaseModel):
    type: Literal["error"] = "error"
    message: str


# ── Worker → Server messages ──

class TokenMessage(BaseModel):
    type: Literal["token"]
    id: str  # job ID
    text: str  # the token text


class DoneMessage(BaseModel):
    type: Literal["done"]
    id: str  # job ID
    full_text: str
    seed: Optional[int] = None


class PongMessage(BaseModel):
    type: Literal["pong"]

# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AIPG P2P Protocol message definitions.

These dataclasses define the wire format for P2P messages.
All messages are JSON-encoded for simplicity (could migrate to protobuf later).

Direct streaming protocol:
- Workers open a direct libp2p stream to the requester for results
- More efficient than gossipsub for high-frequency token streaming
"""

import hashlib

# Protocol ID for direct result streaming
RESULT_STREAM_PROTOCOL = "/aipg/1/result-stream"
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class JobRequest:
    """A job broadcast to the network."""

    id: str
    model: str
    payload: dict[str, Any]
    max_cost: int  # Max AIPG (in wei) willing to pay
    user_pubkey: str  # Hex-encoded public key
    signature: str  # Hex-encoded signature
    requester_peer_id: str = ""  # Peer ID to stream results to (direct connection)
    timestamp: float = field(default_factory=time.time)
    ttl: int = 60  # Seconds until expiry

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, data: str) -> "JobRequest":
        return cls(**json.loads(data))

    @classmethod
    def from_dict(cls, data: dict) -> "JobRequest":
        return cls(**data)

    def is_expired(self) -> bool:
        return time.time() > self.timestamp + self.ttl

    def seed(self) -> bytes:
        """Get the random seed for claim resolution (first 32 bytes of signature)."""
        return bytes.fromhex(self.signature[:64])


@dataclass
class JobClaim:
    """A claim broadcast when a worker starts processing a job."""

    job_id: str
    worker_id: str  # libp2p peer ID
    worker_pubkey: str  # Hex-encoded public key (for payment)
    price: int  # Actual price (must be <= max_cost)
    signature: str  # Worker signs: job_id + price
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, data: str) -> "JobClaim":
        return cls(**json.loads(data))

    @classmethod
    def from_dict(cls, data: dict) -> "JobClaim":
        return cls(**data)


@dataclass
class StreamToken:
    """A single token in a streaming response."""

    text: str
    index: int


@dataclass
class FinalResult:
    """Final result after generation completes."""

    full_text: str
    token_count: int
    receipt_signature: str  # Worker signs: job_id + hash(full_text)


@dataclass
class ErrorResult:
    """Error during generation."""

    message: str
    code: int = 0


@dataclass
class JobResult:
    """Result message (token, done, or error)."""

    job_id: str
    worker_id: str
    type: str  # "token", "done", "error"
    token: StreamToken | None = None
    done: FinalResult | None = None
    error: ErrorResult | None = None

    def to_json(self) -> str:
        data = {
            "job_id": self.job_id,
            "worker_id": self.worker_id,
            "type": self.type,
        }
        if self.token:
            data["token"] = {"text": self.token.text, "index": self.token.index}
        if self.done:
            data["done"] = {
                "full_text": self.done.full_text,
                "token_count": self.done.token_count,
                "receipt_signature": self.done.receipt_signature,
            }
        if self.error:
            data["error"] = {"message": self.error.message, "code": self.error.code}
        return json.dumps(data)

    @classmethod
    def from_json(cls, data: str) -> "JobResult":
        d = json.loads(data)
        token = None
        done = None
        error = None

        if d.get("token"):
            token = StreamToken(**d["token"])
        if d.get("done"):
            done = FinalResult(**d["done"])
        if d.get("error"):
            error = ErrorResult(**d["error"])

        return cls(
            job_id=d["job_id"],
            worker_id=d["worker_id"],
            type=d["type"],
            token=token,
            done=done,
            error=error,
        )

    @classmethod
    def token_msg(cls, job_id: str, worker_id: str, text: str, index: int) -> "JobResult":
        return cls(
            job_id=job_id,
            worker_id=worker_id,
            type="token",
            token=StreamToken(text=text, index=index),
        )

    @classmethod
    def done_msg(
        cls, job_id: str, worker_id: str, full_text: str, token_count: int, signature: str
    ) -> "JobResult":
        return cls(
            job_id=job_id,
            worker_id=worker_id,
            type="done",
            done=FinalResult(
                full_text=full_text, token_count=token_count, receipt_signature=signature
            ),
        )

    @classmethod
    def error_msg(cls, job_id: str, worker_id: str, message: str, code: int = 0) -> "JobResult":
        return cls(
            job_id=job_id,
            worker_id=worker_id,
            type="error",
            error=ErrorResult(message=message, code=code),
        )


def compute_claim_score(job_id: str, seed: bytes, worker_id: str) -> bytes:
    """Compute deterministic score for claim resolution.

    Lower score wins. This allows all nodes to independently compute
    the same winner without coordination.
    """
    data = job_id.encode() + seed + worker_id.encode()
    return hashlib.sha256(data).digest()


def should_claim(job: JobRequest, my_worker_id: str, known_workers: list[str]) -> bool:
    """Determine if this worker should claim the job.

    Uses deterministic hash-based selection. All workers compute the same
    result, so only one will attempt to claim.

    Args:
        job: The job request
        my_worker_id: This worker's peer ID
        known_workers: List of known worker peer IDs (including self)

    Returns:
        True if this worker should claim the job
    """
    if not known_workers:
        return True

    seed = job.seed()
    my_score = compute_claim_score(job.id, seed, my_worker_id)

    for worker_id in known_workers:
        if worker_id == my_worker_id:
            continue
        their_score = compute_claim_score(job.id, seed, worker_id)
        if their_score < my_score:
            return False  # Someone else should win

    return True

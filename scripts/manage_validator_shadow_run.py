#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Preview or apply a validator shadow-run lifecycle transition."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.config import get_settings
from grid_api.database import close_database, init_database
from grid_api.services import validator_shadow as shadow
from grid_api.services.route_events import CAPTURE_TIMEOUT_SECONDS, STREAM_KEY
from grid_api.services.validator_shadow_collector import CONSUMER_GROUP, LEADER_KEY

VERIFICATION_KEYS = (
    "postgres_migration_verified",
    "postgres_concurrency_verified",
    "replay_verified",
    "no_side_effect_verified",
)
RUN_ID_RE = re.compile(r"^shadow_[a-z0-9_]{8,89}$")
VERIFICATION_REF_RE = re.compile(
    r"^https://github\.com/AIPowerGrid/grid-core/actions/runs/[0-9]+(?:/job/[0-9]+)?$",
)
CAPTURE_DRAIN_GRACE_SECONDS = max(300.0, CAPTURE_TIMEOUT_SECONDS)


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--at must include an explicit timezone")
    return parsed.astimezone(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _verification(path: str) -> dict[str, bool]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or set(value) != set(VERIFICATION_KEYS):
        raise ValueError("verification JSON must contain exactly the four documented boolean keys")
    if any(type(value[key]) is not bool for key in VERIFICATION_KEYS):
        raise ValueError("every verification value must be a JSON boolean")
    return {key: value[key] for key in VERIFICATION_KEYS}


def _verification_from_gate(gate: dict[str, Any]) -> dict[str, bool]:
    return {key: bool(gate.get(key)) for key in VERIFICATION_KEYS}


async def _prepare(args, at: datetime) -> dict[str, Any]:
    if not RUN_ID_RE.fullmatch(args.run_id):
        raise ValueError("run id must be a bounded lowercase shadow_* identifier")
    if not VERIFICATION_REF_RE.fullmatch(args.verification_ref):
        raise ValueError("verification ref must be an immutable AIPowerGrid/grid-core Actions run or job URL")
    shadow._require_implementation_commit(args.implementation_commit)
    verification = _verification(args.verification_json)
    config = shadow.runtime_policy_config()
    gate = await shadow.live_start_gate_snapshot(
        verification=verification,
        observed_at=at,
        policy_config=config,
    )
    gate_hash = shadow.commitment(gate)
    result: dict[str, Any] = {
        "schema": "aipg.validator.shadow-run-control.v1",
        "action": "prepare",
        "apply": bool(args.apply),
        "run_id": args.run_id,
        "observed_at": at.isoformat(),
        "implementation_commit": args.implementation_commit,
        "verification_ref": args.verification_ref,
        "policy_version": config["policy_version"],
        "policy_config_hash": shadow.commitment(config),
        "start_gate": gate,
        "start_gate_hash": gate_hash,
        "eligible_to_start": bool(gate["evaluation"]["eligible"]),
    }
    if not args.apply:
        return result
    if args.expect_gate_hash != gate_hash:
        raise shadow.ShadowStartGateError("shadow creation gate changed; preview again")
    row = await shadow.create_run(
        run_id=args.run_id,
        policy_config=config,
        implementation_commit=args.implementation_commit,
        verification_ref=args.verification_ref,
        verification=verification,
        observed_at=at,
        expected_start_gate_hash=gate_hash,
    )
    return {**result, "status": row["status"], "run_state_hash": shadow.run_state_hash(row)}


async def _start(args, at: datetime) -> dict[str, Any]:
    row = await shadow.get_run(args.run_id)
    shadow._require_implementation_commit(row)
    transport = await _transport()
    gate = await shadow.live_start_gate_snapshot(
        verification=_verification_from_gate(row["start_gate"]),
        observed_at=at,
        policy_config=row["policy_config"],
    )
    gate_hash = shadow.commitment(gate)
    transport_ready = bool(
        transport["consumer_group_present"]
        and transport["leader_lease_ttl_seconds"] > 0,
    )
    blocking_reasons = list(gate["evaluation"]["failed"])
    if not transport["consumer_group_present"]:
        blocking_reasons.append("collector_group_missing")
    if transport["leader_lease_ttl_seconds"] <= 0:
        blocking_reasons.append("collector_leader_missing")
    result: dict[str, Any] = {
        "schema": "aipg.validator.shadow-run-control.v1",
        "action": "start",
        "apply": bool(args.apply),
        "run_id": args.run_id,
        "observed_at": at.isoformat(),
        "current_status": row["status"],
        "current_run_state_hash": shadow.run_state_hash(row),
        "start_gate": gate,
        "start_gate_hash": gate_hash,
        "transport": transport,
        "blocking_reasons": blocking_reasons,
        "eligible_to_apply": row["status"] == "draft" and bool(gate["evaluation"]["eligible"]) and transport_ready,
    }
    if not args.apply:
        return result
    if args.expect_gate_hash != gate_hash:
        raise shadow.ShadowStartGateError("shadow start gate changed; preview again")
    if not result["eligible_to_apply"]:
        raise shadow.ShadowStartGateError(
            "shadow start preflight failed: " + ", ".join(blocking_reasons or ["run_not_draft"]),
        )
    started = await shadow.start_run(
        args.run_id,
        started_at=at,
        expected_start_gate_hash=gate_hash,
    )
    return {
        **result,
        "status": started["status"],
        "started": started["started"],
        "scheduled_end": started["scheduled_end"],
        "run_state_hash": shadow.run_state_hash(started),
    }


async def _finish(args, at: datetime) -> dict[str, Any]:
    row = await shadow.get_run(args.run_id)
    shadow._require_implementation_commit(row)
    state_hash = shadow.run_state_hash(row)
    scheduled_end = _aware(row.get("scheduled_end"))
    completed_too_early = args.status == "completed" and (scheduled_end is None or at < scheduled_end)
    capture_grace_missing = args.status == "completed" and (
        scheduled_end is None or at < scheduled_end + timedelta(seconds=CAPTURE_DRAIN_GRACE_SECONDS)
    )
    transport = await _transport() if args.status == "completed" else None
    outbox_not_drained = bool(transport is not None and not transport["drained"])
    blocking_reasons = []
    if completed_too_early:
        blocking_reasons.append("run_has_not_reached_168_hours")
    elif capture_grace_missing:
        blocking_reasons.append("observer_capture_grace_not_elapsed")
    if outbox_not_drained:
        blocking_reasons.append("observer_outbox_not_drained")
    result: dict[str, Any] = {
        "schema": "aipg.validator.shadow-run-control.v1",
        "action": "finish",
        "apply": bool(args.apply),
        "run_id": args.run_id,
        "observed_at": at.isoformat(),
        "requested_status": args.status,
        "current_status": row["status"],
        "current_run_state_hash": state_hash,
        "transport": transport,
        "eligible_to_apply": row["status"] == "running" and not blocking_reasons,
        "blocking_reasons": blocking_reasons,
    }
    if not args.apply:
        return result
    if args.expect_state_hash != state_hash:
        raise shadow.ShadowConflict("shadow run state changed; preview again")
    if not result["eligible_to_apply"]:
        raise shadow.ShadowConflict(
            "shadow finish preflight failed: " + ", ".join(blocking_reasons or ["run_not_running"]),
        )
    finished = await shadow.finish_run(
        args.run_id,
        status=args.status,
        ended_at=at,
        expected_run_state_hash=state_hash,
    )
    return {**result, "status": finished["status"], "ended": finished["ended"], "run_state_hash": shadow.run_state_hash(finished)}


async def _transport() -> dict[str, Any]:
    import redis.asyncio as aioredis
    import redis.exceptions

    redis = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        stream_length = int(await redis.xlen(STREAM_KEY))
        try:
            groups = await redis.xinfo_groups(STREAM_KEY)
        except redis.exceptions.ResponseError as exc:
            if "no such key" not in str(exc).lower():
                raise
            groups = []
        group = next((row for row in groups if row.get("name") == CONSUMER_GROUP), None)
        pending = int(group.get("pending") or 0) if group else 0
        lag = int(group.get("lag") or 0) if group and group.get("lag") is not None else stream_length
        leader_ttl = int(await redis.ttl(LEADER_KEY))
        return {
            "schema": "aipg.validator.shadow-transport-status.v1",
            "stream_present": bool(groups or stream_length),
            "consumer_group_present": group is not None,
            "stream_length": stream_length,
            "pending": pending,
            "lag": lag,
            "leader_lease_ttl_seconds": max(-1, leader_ttl),
            "drained": group is not None and stream_length == 0 and pending == 0 and lag == 0,
        }
    finally:
        await redis.aclose()


async def _run(args) -> dict[str, Any]:
    at = _parse_time(getattr(args, "at", None))
    if args.command == "transport":
        return await _transport()
    await init_database()
    try:
        if args.command == "prepare":
            return await _prepare(args, at)
        if args.command == "start":
            return await _start(args, at)
        if args.command == "finish":
            return await _finish(args, at)
        if args.command == "report":
            return await shadow.run_report(args.run_id, at=at)
        return await shadow.live_start_gate_snapshot(
            verification=_verification(args.verification_json),
            observed_at=at,
        )
    finally:
        await close_database()


def _add_at(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--at", help="fixed ISO-8601 UTC time; required when applying")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate = subparsers.add_parser("gate", help="read the current aggregate start gate")
    gate.add_argument("--verification-json", required=True)
    _add_at(gate)

    prepare = subparsers.add_parser("prepare", help="preview or create one inert draft run")
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--implementation-commit", required=True)
    prepare.add_argument("--verification-ref", required=True)
    prepare.add_argument("--verification-json", required=True)
    prepare.add_argument("--apply", action="store_true")
    prepare.add_argument("--expect-gate-hash")
    _add_at(prepare)

    start = subparsers.add_parser("start", help="preview or start an eligible seven-day run")
    start.add_argument("--run-id", required=True)
    start.add_argument("--apply", action="store_true")
    start.add_argument("--expect-gate-hash")
    _add_at(start)

    finish = subparsers.add_parser("finish", help="preview or close a running run")
    finish.add_argument("--run-id", required=True)
    finish.add_argument("--status", choices=("completed", "failed", "cancelled"), required=True)
    finish.add_argument("--apply", action="store_true")
    finish.add_argument("--expect-state-hash")
    _add_at(finish)

    report = subparsers.add_parser("report", help="print the aggregate, non-promoting report")
    report.add_argument("--run-id", required=True)
    _add_at(report)

    subparsers.add_parser("transport", help="read private collector backlog health")

    args = parser.parse_args()
    if args.command in {"prepare", "start", "finish"} and args.apply:
        if not args.at:
            parser.error("--apply requires the exact --at value from a fresh preview")
        expected = args.expect_state_hash if args.command == "finish" else args.expect_gate_hash
        if not expected:
            parser.error("--apply requires the exact expected hash from a fresh preview")
    try:
        print(json.dumps(asyncio.run(_run(args)), indent=2, sort_keys=True, default=shadow._json_default))
    except (TypeError, ValueError, shadow.ShadowError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

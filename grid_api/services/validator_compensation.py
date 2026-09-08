# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Explicitly approved pilot allocations. No payout queue, wallet or RPC access.

Mutations require PostgreSQL and a matching preview digest. The whole final
allocation and its globally unique work claims commit in one transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from ..database import new_session
from ..v2.schema import validator_assignments as assignments
from ..v2.schema import validator_attestations as attestations
from ..v2.schema import validator_compensation_allocations as allocations
from ..v2.schema import validator_compensation_campaigns as campaigns
from ..v2.schema import validator_compensation_work as work
from ..v2.schema import validators as nodes
from . import validators as evidence
from .settlement.assets import spec as asset_spec
from .validator_compensation_preview import PreviewError, preview_allocation
from .validator_operators import cohort_version_status

SCHEMA = "aipg.validator.compensation.v1"
RECEIPT_GRACE_SECONDS = 3600
LOCK_KEY = 683826451923
MAX_SOURCE_ROWS = 10_000
POLICY = "text.generated.v8"


class CompensationError(ValueError):
    """A stale, unapproved or unverifiable allocation must not commit."""


def _now():
    return datetime.now(UTC)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _time(value):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (TypeError, ValueError):
        raise CompensationError("timestamp requires an explicit timezone") from None


async def _lock(session):
    if session.bind.dialect.name != "postgresql":
        raise CompensationError("durable compensation requires PostgreSQL")
    await session.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})


async def _transaction(session, apply):
    if session.bind.dialect.name != "postgresql":
        raise CompensationError("durable compensation requires PostgreSQL")
    if apply:
        await _lock(session)
    else:
        await session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


def _terms(request):
    required = {
        "campaign_id",
        "starts_at",
        "ends_at",
        "budget_atomic",
        "operator_cap_atomic",
        "daily_unit_cap",
        "software_version",
        "approval_ref",
    }
    if not isinstance(request, dict) or set(request) != required:
        raise CompensationError("invalid pilot contract fields")
    result = dict(request)
    if not isinstance(result["campaign_id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", result["campaign_id"]):
        raise CompensationError("invalid campaign identifier")
    if not isinstance(result["approval_ref"], str) or not re.fullmatch(r"approval:[A-Za-z0-9_-]{3,96}", result["approval_ref"]):
        raise CompensationError("explicit opaque owner approval reference required")
    if not isinstance(result["software_version"], str) or not re.fullmatch(
        r"v[0-9]+\.[0-9]+\.[0-9]+(?:-preview\.[0-9]+)?",
        result["software_version"],
    ):
        raise CompensationError("exact released validator version required")
    if not cohort_version_status(result["software_version"])[1]:
        raise CompensationError("pilot version is not admitted by Core")
    start, end = _time(result["starts_at"]), _time(result["ends_at"])
    if end - start != timedelta(days=7):
        raise CompensationError("pilot duration must be exactly seven days")
    result.update(starts_at=start.isoformat(), ends_at=end.isoformat())
    # Reuse the established integer/cap policy without accepting its unverified
    # operator/work input as authority for the durable ledger.
    try:
        preview_allocation({"terms": _allocation_terms(result), "operators": [], "contributions": []}, as_of=start.isoformat())
    except PreviewError as exc:
        raise CompensationError(str(exc)) from None
    return result


def _allocation_terms(contract):
    return {
        key: contract[key]
        for key in (
            "campaign_id",
            "starts_at",
            "ends_at",
            "budget_atomic",
            "operator_cap_atomic",
            "daily_unit_cap",
        )
    }


def _member(row):
    if row["independence_status"] != "verified" or row["status"] != "active" or not row["independence_review_ref"]:
        raise CompensationError("every pilot member requires a current independent-control review")
    if not row["operator_group_id"] or not re.fullmatch(r"opg_[A-Za-z0-9_-]{8,88}", row["operator_group_id"]):
        raise CompensationError("reviewed operator group required")
    return {
        "validator_id": row["id"],
        "account_id": str(row["account_id"]),
        "signing_wallet": row["signing_wallet"],
        "software_version": row["software_version"],
        "operator_group_id": row["operator_group_id"],
        "reviewed_at": _time(row["independence_reviewed_at"]).isoformat(),
        "expires_at": _time(row["independence_expires_at"]).isoformat(),
        "review_ref": row["independence_review_ref"],
    }


async def _members(session, ids, *, apply):
    if not isinstance(ids, list) or not 3 <= len(ids) <= 10:
        raise CompensationError("pilot requires three to ten distinct reviewed members")
    if any(not isinstance(value, str) or not re.fullmatch(r"val_[a-f0-9]{32}", value) for value in ids):
        raise CompensationError("invalid validator identifier")
    if len(set(ids)) != len(ids):
        raise CompensationError("pilot requires distinct reviewed members")
    query = sa.select(nodes).where(nodes.c.id.in_(ids)).order_by(nodes.c.id)
    if apply:
        query = query.with_for_update()
    rows = (await session.execute(query)).mappings().all()
    if len(rows) != len(ids):
        raise CompensationError("pilot member not found")
    members = [_member(row) for row in rows]
    if len({row["operator_group_id"] for row in members}) != len(members):
        raise CompensationError("one pilot seat per independently controlled operator")
    return rows, members


async def create_campaign(request, validator_ids, *, apply=False, expected_digest=None):
    terms = _terms(request)
    asset = asset_spec("AIPG")
    if (
        not asset
        or asset["kind"] != "erc20"
        or asset["decimals"] != 18
        or not re.fullmatch(r"0x[a-fA-F0-9]{40}", asset["address"] or "")
        or int(asset["address"], 16) == 0
    ):
        raise CompensationError("configured AIPG asset is invalid")
    async with await new_session() as session:
        await _transaction(session, apply)
        rows, members = await _members(session, validator_ids, apply=apply)
        contract = {
            **terms,
            "schema": SCHEMA,
            "asset": "AIPG",
            "chain_id": 8453,
            "token_address": asset["address"].lower(),
            "decimals": asset["decimals"],
            "scoring_policy": POLICY,
            "receipt_grace_seconds": RECEIPT_GRACE_SECONDS,
            "members": members,
        }
        digest = _hash(contract)
        if apply and expected_digest != digest:
            raise CompensationError("pilot contract changed or approval digest missing")
        existing = (await session.execute(sa.select(campaigns).where(campaigns.c.id == terms["campaign_id"]))).mappings().first()
        if existing:
            if existing["contract_hash"] != digest:
                raise CompensationError("campaign terms are immutable")
            return {"campaign_id": existing["id"], "digest": digest, "dry_run": not apply, "sendable": False}
        now, start, end = _now(), _time(terms["starts_at"]), _time(terms["ends_at"])
        if not now < start <= now + timedelta(days=1):
            raise CompensationError("freeze the pilot before work starts, at most one day ahead")
        for row, member in zip(rows, members):
            if member["software_version"] != terms["software_version"]:
                raise CompensationError("pilot members must use its exact frozen release")
            if not now - timedelta(seconds=evidence.VALIDATOR_HEARTBEAT_FRESH_SECONDS) <= _time(row["last_heartbeat"]) <= now:
                raise CompensationError("fresh member heartbeat required to freeze pilot")
            if not _time(member["reviewed_at"]) <= now or _time(member["expires_at"]) <= end + timedelta(seconds=RECEIPT_GRACE_SECONDS):
                raise CompensationError("member review must cover the complete pilot and receipt grace")
        if apply:
            if _now() >= start:
                raise CompensationError("pilot start passed before approval committed")
            await session.execute(
                sa.insert(campaigns).values(
                    id=terms["campaign_id"],
                    contract=contract,
                    contract_hash=digest,
                    budget_atomic=Decimal(terms["budget_atomic"]),
                    created=now,
                ),
            )
            await session.commit()
        return {"campaign_id": terms["campaign_id"], "digest": digest, "dry_run": not apply, "sendable": False, "contract": contract}


def _verify_work(attestation, assignment, member, contract):
    if not assignment or assignment["probe_status"] != "completed":
        raise CompensationError("completed frozen-policy assignment is missing")
    if assignment["scoring_policy_id"] != contract["scoring_policy"] or assignment["modality"] != "text":
        return None, "outside_pilot_policy"
    try:
        normalized = evidence._normalize(attestation["payload"], attestation["signature"])
    except evidence.AttestationError:
        raise CompensationError("attestation signature or envelope is invalid") from None
    for key in (
        "authority",
        "signature_status",
        "attestation_hash",
        "validator_wallet",
        "assignment_id",
        "probe_group_id",
        "grid_nonce",
        "evidence_hash",
        "verdict",
    ):
        if normalized[key] != attestation[key]:
            raise CompensationError("stored evidence does not match its signed envelope")
    if normalized["authority"] != "authoritative" or normalized["signature_status"] != "verified":
        raise CompensationError("unsigned or preview evidence is not payable work")
    for row in (attestation, assignment):
        if (
            str(row["account_id"]) != member["account_id"]
            or row["validator_id"] != member["validator_id"]
            or row["validator_wallet"] != member["signing_wallet"]
        ):
            raise CompensationError("evidence ownership changed")
    checks = {
        "assignment_id": assignment["id"],
        "probe_group_id": assignment["probe_group_id"],
        "grid_nonce": assignment["grid_nonce"],
        "evidence_hash": assignment["probe_evidence_hash"],
        "worker_id": assignment["target_worker_id"],
        "model": assignment["model"],
        "modality": assignment["modality"],
        "capability": assignment["capability"],
        "canary_kind": assignment["canary_kind"],
    }
    if not assignment["probe_evidence_hash"] or not assignment["probe_group_id"]:
        raise CompensationError("completed assignment binding is incomplete")
    if any(normalized[key] != value or attestation[key] != value for key, value in checks.items()):
        raise CompensationError("signed report does not match its assigned task")
    start, end = _time(contract["starts_at"]), _time(contract["ends_at"])
    probed, received = _time(assignment["probed"]), _time(attestation["created"])
    if not start <= _time(assignment["created"]) <= probed < end or not probed <= received < end + timedelta(
        seconds=contract["receipt_grace_seconds"],
    ):
        return None, "outside_work_window"
    if received > _time(assignment["expires"]) + timedelta(seconds=evidence.ATTESTATION_GRACE_SECONDS):
        raise CompensationError("report was received outside its assignment grace")
    if normalized["verdict"] != assignment["probe_verdict"]:
        return None, "incorrect_task_score"
    verification = {
        "assignment_created": _time(assignment["created"]).isoformat(),
        "assignment_expires": _time(assignment["expires"]).isoformat(),
        "scoring_policy": assignment["scoring_policy_id"],
        "probed": probed.isoformat(),
        "received": received.isoformat(),
        "objective_verdict": assignment["probe_verdict"],
    }
    proof = {**checks, "attestation_hash": normalized["attestation_hash"], "verification": verification}
    return {
        "attestation_id": attestation["id"],
        "assignment_id": assignment["id"],
        "operator_group_id": member["operator_group_id"],
        "probe_group_id": assignment["probe_group_id"],
        "evidence_commitment": _hash(proof),
        "verification": verification,
        "completed_at": probed.isoformat(),
    }, None


async def _committed_result(session, campaign):
    result = campaign["result"]
    body = {key: value for key, value in result.items() if key not in {"digest", "status", "dry_run"}}
    if _hash(body) != result["digest"] or result["contract_hash"] != campaign["contract_hash"]:
        raise CompensationError("finalized allocation commitment mismatch")
    rows = (
        (
            await session.execute(
                sa.select(allocations)
                .where(
                    allocations.c.campaign_id == campaign["id"],
                )
                .order_by(allocations.c.operator_group_id),
            )
        )
        .mappings()
        .all()
    )
    actual = [
        {
            "operator_group_id": row["operator_group_id"],
            "reviewed_units": row["reviewed_units"],
            "amount_atomic": str(int(row["amount_atomic"])),
        }
        for row in rows
    ]
    if actual != result["allocations"] or sum(int(row["amount_atomic"]) for row in rows) != int(campaign["allocated_atomic"]):
        raise CompensationError("allocation rows do not reconcile with finalized campaign")
    members = {member["operator_group_id"]: member for member in campaign["contract"]["members"]}
    for row, allocation in zip(rows, actual):
        if str(row["account_id"]) != members[row["operator_group_id"]]["account_id"] or row["allocation_hash"] != _hash(
            {
                "campaign_id": campaign["id"],
                **allocation,
                "digest": result["digest"],
            },
        ):
            raise CompensationError("allocation beneficiary or amount commitment changed")
    counts = dict(
        (
            await session.execute(
                sa.select(work.c.operator_group_id, sa.func.count())
                .where(
                    work.c.campaign_id == campaign["id"],
                )
                .group_by(work.c.operator_group_id),
            )
        ).all(),
    )
    if counts != {row["operator_group_id"]: row["reviewed_units"] for row in rows}:
        raise CompensationError("work claims do not reconcile with finalized allocations")
    return result


async def finalize_campaign(campaign_id, *, apply=False, expected_digest=None):
    async with await new_session() as session:
        await _transaction(session, apply)
        campaign = (await session.execute(sa.select(campaigns).where(campaigns.c.id == campaign_id))).mappings().first()
        if not campaign:
            raise CompensationError("pilot contract not found")
        contract = campaign["contract"]
        if _hash(contract) != campaign["contract_hash"]:
            raise CompensationError("pilot contract commitment mismatch")
        if campaign["status"] == "finalized":
            if apply and expected_digest != campaign["result"]["digest"]:
                raise CompensationError("finalized allocation digest mismatch")
            return await _committed_result(session, campaign)
        now, end = _now(), _time(contract["ends_at"])
        if now < end + timedelta(seconds=contract["receipt_grace_seconds"]):
            raise CompensationError("pilot and late-receipt window have not ended")
        _, members = await _members(session, [m["validator_id"] for m in contract["members"]], apply=apply)
        if members != contract["members"] or any(_time(m["expires_at"]) <= now for m in members):
            raise CompensationError("member identity or review changed; review the frozen pilot")
        by_id = {m["validator_id"]: m for m in members}
        query = (
            sa.select(attestations)
            .where(
                attestations.c.validator_id.in_(by_id),
                attestations.c.authority == "authoritative",
                attestations.c.created >= _time(contract["starts_at"]),
                attestations.c.created < end + timedelta(seconds=contract["receipt_grace_seconds"]),
            )
            .order_by(attestations.c.created, attestations.c.id)
            .limit(MAX_SOURCE_ROWS + 1)
        )
        reports = (await session.execute(query)).mappings().all()
        if len(reports) > MAX_SOURCE_ROWS:
            raise CompensationError("source snapshot exceeds bounded pilot size")
        assignment_ids = [r["assignment_id"] for r in reports if r["assignment_id"]]
        assigned = {
            r["id"]: r for r in (await session.execute(sa.select(assignments).where(assignments.c.id.in_(assignment_ids)))).mappings()
        }
        claimed = set(
            (
                await session.execute(sa.select(work.c.attestation_id).where(work.c.attestation_id.in_([r["id"] for r in reports])))
            ).scalars(),
        )
        groups = set(
            (
                await session.execute(
                    sa.select(work.c.operator_group_id, work.c.probe_group_id).where(
                        work.c.probe_group_id.in_([r["probe_group_id"] for r in reports if r["probe_group_id"]]),
                    ),
                )
            ).all(),
        )
        included, exclusions, daily, source = [], Counter(), Counter(), []
        for report in reports:
            member = by_id[report["validator_id"]]
            item, reason = _verify_work(report, assigned.get(report["assignment_id"]), member, contract)
            source.append([report["id"], report["attestation_hash"], item, reason])
            if reason:
                exclusions[reason] += 1
                continue
            key = (item["operator_group_id"], item["probe_group_id"])
            if report["id"] in claimed or key in groups:
                exclusions["previously_allocated_or_duplicate_group"] += 1
                continue
            day = (item["operator_group_id"], _time(item["completed_at"]).date().isoformat())
            if daily[day] >= contract["daily_unit_cap"]:
                exclusions["daily_cap"] += 1
                continue
            groups.add(key)
            daily[day] += 1
            included.append(item)
        inputs = {
            "terms": _allocation_terms(contract),
            "operators": [
                {
                    "operator_group_id": m["operator_group_id"],
                    "first_party": False,
                    "review_status": "verified",
                    "reviewed_at": m["reviewed_at"],
                    "expires_at": m["expires_at"],
                    "review_digest": _hash(m),
                }
                for m in members
            ],
            "contributions": [
                {
                    "assignment_id": w["assignment_id"],
                    "operator_group_id": w["operator_group_id"],
                    "probe_group_id": w["probe_group_id"],
                    "completed_at": w["completed_at"],
                    "evidence_digest": w["evidence_commitment"],
                }
                for w in included
            ],
        }
        computed = preview_allocation(inputs, as_of=now.isoformat())
        if computed["reviewed_units"] != len(included) or computed["excluded"]:
            raise CompensationError("allocation policy disagrees with verified work selection")
        result = {
            key: computed[key] for key in ("reviewed_units", "eligible_operators", "allocated_atomic", "unallocated_atomic", "allocations")
        }
        result.update(
            schema=SCHEMA,
            campaign_id=campaign_id,
            sendable=False,
            excluded=dict(sorted(exclusions.items())),
            contract_hash=campaign["contract_hash"],
            source_commitment=_hash(source),
        )
        digest = _hash(result)
        result.update(digest=digest, status="finalized" if apply else "preview", dry_run=not apply)
        if apply:
            if expected_digest != digest:
                raise CompensationError("allocation preview changed or approval digest missing")
            member_groups = {m["operator_group_id"]: m for m in members}
            for allocation in result["allocations"]:
                await session.execute(
                    sa.insert(allocations).values(
                        campaign_id=campaign_id,
                        operator_group_id=allocation["operator_group_id"],
                        account_id=uuid.UUID(member_groups[allocation["operator_group_id"]]["account_id"]),
                        reviewed_units=allocation["reviewed_units"],
                        amount_atomic=Decimal(allocation["amount_atomic"]),
                        allocation_hash=_hash({"campaign_id": campaign_id, **allocation, "digest": digest}),
                        created=now,
                    ),
                )
            for item in included:
                await session.execute(
                    sa.insert(work).values(
                        campaign_id=campaign_id,
                        created=now,
                        **{key: value for key, value in item.items() if key != "completed_at"},
                    ),
                )
            await session.execute(
                sa.update(campaigns)
                .where(campaigns.c.id == campaign_id, campaigns.c.status == "open")
                .values(
                    status="finalized",
                    finalized_at=now,
                    allocated_atomic=Decimal(result["allocated_atomic"]),
                    result=result,
                ),
            )
            await session.commit()
        return result

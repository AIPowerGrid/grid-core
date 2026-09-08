# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline allocation simulation, never an entitlement or a payment manifest.

Inputs are private reviewer assertions, not independently verified signatures.
No database, wallet, network, or settlement dependency belongs in this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

SCHEMA = "aipg.validator.compensation-preview.v1"
ATOMIC_PER_AIPG = 10**18


class PreviewError(ValueError):
    """Invalid or conflicting reviewer input; messages contain no private values."""


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise PreviewError("unexpected input fields")


def _text(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise PreviewError("invalid identifier or digest")
    return value


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise PreviewError("invalid timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except ValueError:
        raise PreviewError("timestamp must have an explicit timezone") from None


def _amount(value, maximum):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,23}", value):
        raise PreviewError("amount must be a positive decimal base-unit string")
    amount = int(value)
    if amount > maximum:
        raise PreviewError("amount exceeds draft pilot limit")
    return amount


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def preview_allocation(payload, *, as_of: str):
    """Deduplicate reviewed inputs and simulate capped, integer-only allocation.

    The snapshot is untrusted until independently checked against Core and reviewed.
    The digest binds this simulation, not an approved budget, recipient or payment.
    """
    _fields(payload, "terms operators contributions")
    terms = payload["terms"]
    _fields(terms, "campaign_id starts_at ends_at budget_atomic operator_cap_atomic daily_unit_cap")
    campaign = _text(terms["campaign_id"], r"[A-Za-z0-9_-]{3,96}")
    start, end, now = _time(terms["starts_at"]), _time(terms["ends_at"]), _time(as_of)
    if not timedelta(0) < end - start <= timedelta(days=7):
        raise PreviewError("campaign window must be positive and at most seven days")
    budget = _amount(terms["budget_atomic"], 10_000 * ATOMIC_PER_AIPG)
    cap = _amount(terms["operator_cap_atomic"], 2_000 * ATOMIC_PER_AIPG)
    daily_cap = terms["daily_unit_cap"]
    if type(daily_cap) is not int or not 1 <= daily_cap <= 100:
        raise PreviewError("daily unit cap must be an integer from one to one hundred")
    if not isinstance(payload["operators"], list) or len(payload["operators"]) > 100:
        raise PreviewError("operator list is invalid or oversized")
    operators = {}
    for item in payload["operators"]:
        _fields(item, "operator_group_id first_party review_status reviewed_at expires_at review_digest")
        group = _text(item["operator_group_id"], r"opg_[A-Za-z0-9_-]{8,88}")
        if group in operators or type(item["first_party"]) is not bool:
            raise PreviewError("duplicate operator or invalid first-party classification")
        if item["review_status"] not in ("verified", "unreviewed", "rejected"):
            raise PreviewError("invalid review status")
        reviewed, expires = _time(item["reviewed_at"]), _time(item["expires_at"])
        if reviewed >= expires:
            raise PreviewError("invalid review interval")
        _text(item["review_digest"], r"[a-f0-9]{64}")
        operators[group] = {**item, "reviewed_at": reviewed.isoformat(), "expires_at": expires.isoformat()}

    rows = payload["contributions"]
    if not isinstance(rows, list) or len(rows) > 10_000:
        raise PreviewError("contribution list is invalid or oversized")
    unique = {}
    for item in rows:
        _fields(item, "assignment_id operator_group_id probe_group_id completed_at evidence_digest")
        assignment = _text(item["assignment_id"], r"[A-Za-z0-9_-]{1,96}")
        _text(item["probe_group_id"], r"[A-Za-z0-9_-]{1,96}")
        group = _text(item["operator_group_id"], r"opg_[A-Za-z0-9_-]{8,88}")
        _text(item["evidence_digest"], r"[a-f0-9]{64}")
        if group not in operators:
            raise PreviewError("contribution has no matching operator review")
        normalized = {**item, "completed_at": _time(item["completed_at"]).isoformat()}
        if assignment in unique and unique[assignment] != normalized:
            raise PreviewError("conflicting replay of an assignment")
        unique[assignment] = normalized

    counts, daily, groups = Counter(), Counter(), set()
    excluded = Counter()
    for item in sorted(unique.values(), key=lambda row: (row["completed_at"], row["assignment_id"])):
        group = item["operator_group_id"]
        operator = operators[group]
        completed = _time(item["completed_at"])
        if not start <= completed < end or completed > now:
            excluded["outside_observation_window"] += 1
            continue
        if operator["first_party"] or operator["review_status"] != "verified":
            excluded["operator_not_eligible"] += 1
            continue
        if not _time(operator["reviewed_at"]) <= completed <= now < _time(operator["expires_at"]):
            excluded["review_not_current_for_work_and_preview"] += 1
            continue
        key = (group, item["probe_group_id"])
        if key in groups:
            excluded["duplicate_operator_probe_group"] += 1
            continue
        groups.add(key)
        day = (group, completed.date().isoformat())
        if daily[day] >= daily_cap:
            excluded["daily_cap"] += 1
            continue
        daily[day] += 1
        counts[group] += 1

    total_units = sum(counts.values())
    allocations = [
        {"operator_group_id": group, "reviewed_units": units, "amount_atomic": str(min(cap, budget * units // total_units))}
        for group, units in sorted(counts.items())
    ]
    allocated = sum(int(row["amount_atomic"]) for row in allocations)
    snapshot = {
        "schema": SCHEMA,
        "terms": {**terms, "campaign_id": campaign, "starts_at": start.isoformat(), "ends_at": end.isoformat()},
        "as_of": now.isoformat(),
        "operators": [operators[group] for group in sorted(operators)],
        "contributions": [unique[key] for key in sorted(unique)],
    }
    return {
        "schema": SCHEMA,
        "dry_run": True,
        "sendable": False,
        "input_authority": "unverified_reviewer_snapshot",
        "simulation_digest": hashlib.sha256(_canonical(snapshot).encode()).hexdigest(),
        "campaign_id": campaign,
        "window_complete": now >= end,
        "reviewed_units": total_units,
        "eligible_operators": len(counts),
        "allocated_atomic": str(allocated),
        "unallocated_atomic": str(budget - allocated),
        "excluded": dict(sorted(excluded.items())),
        "allocations": allocations,
    }

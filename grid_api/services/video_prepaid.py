# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private proved-payment -> external hold/outbox, with unsent refund liabilities.

No HTTP sender, facilitator, runtime admission flag or refund signer exists here.
The existing reservation/execution fence remains the only financial terminal.
"""

import copy
from datetime import UTC, datetime
from urllib.parse import urlsplit

import sqlalchemy as sa

from ..database import new_session
from ..v2.schema import media_jobs as jobs
from ..v2.schema import reservations, video_payment_attempts, video_prepaid_payments, video_quotes
from . import identities, media_jobs, payment_receipt_claims, validator_audit_budgets
from . import video_payment_attempts as attempts
from . import video_quote_staging as staging
from . import video_quotes as quotes

SOURCE = "video_prepaid"


class VideoPrepaidError(ValueError):
    """Opaque funded-admission error, without private inputs or payment values."""


def _now():
    return datetime.now(UTC)


def _check_payload(quote, payload, manifest):
    expected = quote["snapshot"]["plan"]["payload"]
    actual = copy.deepcopy(payload)
    authority = staging.authority(manifest)

    def location(value, item):
        if type(value) is not str or not 0 < len(value) <= 8192 or authority is None:
            raise VideoPrepaidError("frozen video source location changed")
        parsed = urlsplit(value)
        if (f"{parsed.scheme}://{parsed.netloc}" != authority["endpoint"]
                or parsed.path != f"/{authority['bucket']}/{item['key']}" or not parsed.query or parsed.fragment):
            raise VideoPrepaidError("frozen video source location changed")

    images = manifest["images"]
    if images:
        location(actual.get("source_image_url"), images[0])
        if "source_image_urls" in expected:
            values = actual.get("source_image_urls")
            if type(values) is not list or len(values) != len(images):
                raise VideoPrepaidError("frozen video source slots changed")
            for value, item in zip(values, images, strict=True):
                location(value, item)
    for key in ("source_image_url", "source_image_urls"):
        if key in expected:
            if key not in actual or (key.endswith("urls") and len(actual[key]) != len(expected[key])):
                raise VideoPrepaidError("frozen video source slots changed")
            actual[key] = expected[key]
    if "source_media" in expected:
        if len(actual.get("source_media", [])) != len(expected["source_media"]):
            raise VideoPrepaidError("frozen video source slots changed")
        for index, (item, original) in enumerate(zip(actual["source_media"], expected["source_media"], strict=True)):
            location(item.get("url"), manifest["sources"][index])
            item["url"] = original["url"]
    if actual != expected:
        raise VideoPrepaidError("frozen video execution changed")


async def _proof_owner(session, quote, attempt):
    if attempt is None or attempt["status"] != "proven":
        raise VideoPrepaidError("original video payment is not proven")
    attempts._validate(attempt, quote)
    proof = attempt["proof"]
    claim = await payment_receipt_claims.require_owned_in_session(
        session, chain_id=proof["chain_id"], asset=proof["asset"], transaction_hash=proof["transaction_hash"],
        owner_kind="video_x402", owner_ref=str(quote["id"]),
    )
    if claim["purpose"] != {"schema": "aipg.video-receipt.v1", "quote_commitment": quote["commitment"],
                            "authorization_identity": attempt["authorization_identity"], "amount_micro": quote["amount_micro"]}:
        raise VideoPrepaidError("original video receipt purpose changed")
    return claim


async def _validate_payment(session, payment, quote, attempt):
    claim = await _proof_owner(session, quote, attempt)
    if (payment["job_id"] != str(quote["job_id"]) or payment["receipt_claim_key"] != claim["claim_key"]
            or payment["quote_commitment"] != quote["commitment"] or payment["proof_hash"] != attempt["proof_hash"]
            or payment["amount_micro"] != quote["amount_micro"]
            or quotes._aware(payment["created"]) < quotes._aware(attempt["proven_at"])):
        raise VideoPrepaidError("stored prepaid video binding changed")
    if payment["admitted_at"] is not None:
        if (quotes._aware(payment["admitted_at"]) != quotes._aware(payment["created"])
                or not quotes._aware(quote["created"]) <= quotes._aware(payment["admitted_at"]) < quotes._aware(quote["expires"])):
            raise VideoPrepaidError("stored prepaid video admission time changed")
        _stored_quote, stage = await staging.require_ready_in_session(session, quote["account_id"], quote["id"])
        if payment["manifest_hash"] != stage["manifest_hash"]:
            raise VideoPrepaidError("prepaid video input ownership changed")
    if payment["refund_due_at"] is not None and quotes._aware(payment["refund_due_at"]) < quotes._aware(payment["created"]):
        raise VideoPrepaidError("stored prepaid video refund time changed")
    return payment


async def lookup(account_id, quote_id):
    """Owner-family recovery only; no admission, expiry gate or presigning."""
    try:
        async with await new_session() as session:
            quote, attempt = await attempts._owned(session, account_id, quote_id)
            return await _payment(session, quote, attempt)
    except Exception:
        raise VideoPrepaidError("prepaid video recovery unavailable") from None


async def _payment(session, quote, attempt):
    row = (await session.execute(sa.select(video_prepaid_payments).where(
        video_prepaid_payments.c.quote_id == quote["id"]))).mappings().one_or_none()
    if row is None:
        return None
    payment = await _validate_payment(session, dict(row), quote, attempt)
    if payment["admitted_at"] is not None:
        await require_hold_in_session(session, payment["job_id"])
    for key in ("created", "admitted_at", "refund_due_at"):
        if payment[key] is not None:
            payment[key] = quotes._aware(payment[key])
    return payment


async def require_hold_in_session(session, job_id, *, require_job=True):
    """Caller owns the financial job lock; no chain, storage or pocket movement."""
    payment = (await session.execute(sa.select(video_prepaid_payments).where(
        video_prepaid_payments.c.job_id == str(job_id)))).mappings().one_or_none()
    if payment is None or payment["admitted_at"] is None:
        raise VideoPrepaidError("funded video admission is unavailable")
    quote = (await session.execute(sa.select(video_quotes).where(
        video_quotes.c.id == payment["quote_id"]))).mappings().one()
    attempt = (await session.execute(sa.select(video_payment_attempts).where(
        video_payment_attempts.c.quote_id == payment["quote_id"]))).mappings().one()
    await _validate_payment(session, dict(payment), dict(quote), dict(attempt))
    hold = (await session.execute(sa.select(reservations).where(reservations.c.job_id == str(job_id)))).mappings().one_or_none()
    expected_actual = 0 if payment["refund_due_at"] is not None else payment["amount_micro"]
    if (hold is None or hold["billing_source"] != SOURCE or hold["account_id"] != quote["account_id"]
            or hold["model"] != quote["snapshot"]["plan"]["model"] or hold["reserved_micro"] != payment["amount_micro"]
            or hold["external_payer"] != attempt["payer"] or hold["free_micro"] != 0 or hold["promo_micro"] != 0
            or hold["actual_micro"] not in {None, 0, payment["amount_micro"]}
            or hold["status"] not in {"held", "settled"}
            or (hold["status"] == "held" and (payment["refund_due_at"] is not None or hold["actual_micro"] is not None))
            or (hold["status"] == "settled" and (hold["settled"] is None
                or hold["actual_micro"] != expected_actual))):
        raise VideoPrepaidError("external video hold changed")
    if require_job:
        job = (await session.execute(sa.select(jobs).where(jobs.c.job_id == str(job_id)))).mappings().one_or_none()
        if job is None:
            raise VideoPrepaidError("funded video outbox is unavailable")
        await _check_job(session, dict(job), dict(quote))
    return dict(payment)


async def _check_job(session, job, quote):
    _quote, stage = await staging.require_ready_in_session(session, quote["account_id"], quote["id"])
    payload = copy.deepcopy(job["payload"])
    marker = payload.pop(media_jobs.MARKER, None)
    if type(marker) is not int or marker != 1:
        raise VideoPrepaidError("funded video journal marker changed")
    _check_payload(quote, payload, stage["manifest"])
    plan, preferred = quote["snapshot"]["plan"], quote["snapshot"].get("preferred_worker", "")
    if (job["account_id"] != quote["account_id"] or job["model"] != plan["model"]
            or job["request_hash"] != quote["request_hash"] or job["max_cost_micro"] != quote["amount_micro"]
            or job["route_models"] != plan["route_models"] or job["preferred_worker"] != preferred
            or job["idempotency_hash"] != quotes._hash({"schema": "aipg.prepaid-video-key.v1", "quote_id": str(quote["id"])})
            or job["source_objects"] != staging.authority(stage["manifest"])
            or job["payload_hash"] != media_jobs._payload_hash(job["payload"], job["route_models"], job["model"], preferred)):
        raise VideoPrepaidError("funded video outbox changed")


async def require_prepared_in_session(session, job, source_objects):
    payment = await require_hold_in_session(session, job.job_id, require_job=False)
    quote = (await session.execute(sa.select(video_quotes).where(
        video_quotes.c.id == payment["quote_id"]))).mappings().one()
    await _check_job(session, {**vars(job), "payload": job.payload, "route_models": job.route_models,
                              "source_objects": source_objects}, dict(quote))


async def fail_in_session(session, job_id):
    """Called by the winning reservation release; not a credit or chain refund."""
    payment = await require_hold_in_session(session, job_id)
    if payment["refund_due_at"] is not None:
        return
    await session.execute(video_prepaid_payments.update().where(
        video_prepaid_payments.c.quote_id == payment["quote_id"], video_prepaid_payments.c.refund_due_at.is_(None),
    ).values(refund_due_at=_now(), refund_reason="execution_failed"))


def _closed_reason(quote, allow_execution, admission_until):
    if _now() >= quotes._aware(quote["expires"]):
        return "quote_expired"
    if not allow_execution or _now() >= admission_until:
        return "admission_closed"
    return None


async def admit_proven(account_id, quote_id, *, allow_execution: bool, admission_until: datetime,
                       concurrency_limit: int = 4):
    """One atomic funded hold/job or refund-due record for an owned proved receipt.

    The future adapter must enforce scoped auth, model permission and runtime
    pilot gates. This private call is not payment collection or public admission.
    Presigning stays outside SQL locks. Replay never refreshes a job's inputs,
    plan, seed, price or identity, even after admission rollback and expiry.
    """
    try:
        if (type(allow_execution) is not bool or not isinstance(admission_until, datetime)
                or admission_until.tzinfo is None or type(concurrency_limit) is not int or not 1 <= concurrency_limit <= 16):
            raise VideoPrepaidError("explicit bounded video admission policy required")
        async with await new_session() as session:
            quote, attempt = await attempts._owned(session, account_id, quote_id)
            await _proof_owner(session, quote, attempt)
            existing = await _payment(session, quote, attempt)
            if existing is not None:
                return existing
        payload = source_record = None
        if _closed_reason(quote, allow_execution, admission_until) is None:
            payload, source_record = await staging.dispatch_payload(account_id, quote_id)
        async with await new_session() as session:
            quote, attempt = await attempts._owned(session, account_id, quote_id, lock=True)
            claim = await _proof_owner(session, quote, attempt)
            existing = await _payment(session, quote, attempt)
            if existing is not None:
                return existing
            await validator_audit_budgets.lock_job_in_session(session, str(quote["job_id"]))
            reason = _closed_reason(quote, allow_execution, admission_until)
            stage = None
            if reason is None:
                family = await identities.account_family_ids(account_id, session=session, max_members=128)
                held = await session.scalar(sa.select(sa.func.count()).select_from(jobs.join(
                    reservations, reservations.c.job_id == jobs.c.job_id,
                )).where(jobs.c.account_id.in_(family), reservations.c.status == "held"))
                if held >= concurrency_limit:
                    reason = "capacity_exceeded"
                else:
                    _original, stage = await staging.require_ready_in_session(session, account_id, quote_id)
                    if source_record != staging.authority(stage["manifest"]):
                        raise VideoPrepaidError("staged source authority changed")
            now = _now()
            values = {"quote_id": quote["id"], "job_id": str(quote["job_id"]), "receipt_claim_key": claim["claim_key"],
                "quote_commitment": quote["commitment"], "proof_hash": attempt["proof_hash"], "amount_micro": quote["amount_micro"],
                "manifest_hash": stage["manifest_hash"] if stage else None, "admitted_at": now if reason is None else None,
                "refund_due_at": now if reason else None, "refund_reason": reason, "created": now}
            await session.execute(video_prepaid_payments.insert().values(**values))
            if reason is None:
                from . import credits
                plan = quote["snapshot"]["plan"]
                _check_payload(quote, payload, stage["manifest"])
                preferred = quote["snapshot"].get("preferred_worker", "")
                frozen = {**payload, media_jobs.MARKER: 1}
                job = media_jobs.PreparedMediaJob(
                    job_id=values["job_id"], account_id=quote["account_id"],
                    idempotency_hash=quotes._hash({"schema": "aipg.prepaid-video-key.v1", "quote_id": str(quote["id"])}),
                    request_hash=quote["request_hash"], payload_hash=media_jobs._payload_hash(
                        frozen, plan["route_models"], plan["model"], preferred),
                    model=plan["model"], payload_json=media_jobs._json(frozen), route_json=media_jobs._json(plan["route_models"]),
                    preferred_worker=preferred, max_cost_micro=quote["amount_micro"], seconds=float(payload["seconds"]),
                )
                await credits._insert_reservation_in_session(session, job.job_id, job.account_id, job.model,
                    quote["amount_micro"], 0, billing_source=SOURCE, external_payer=attempt["payer"])
                await media_jobs.insert_in_session(session, job, prepaid=True, source_objects=source_record)
                if _closed_reason(quote, allow_execution, admission_until) is not None:
                    await session.rollback()
                    return await admit_proven(account_id, quote_id, allow_execution=False, admission_until=admission_until,
                                              concurrency_limit=concurrency_limit)
            await session.commit()
            return values
    except VideoPrepaidError:
        raise
    except Exception:
        raise VideoPrepaidError("prepaid video admission unavailable") from None


def funded_condition(hold):
    """Read-only payout backing; a source label or old proven attempt is not money."""
    from ..v2.schema import payment_receipt_claims as claims
    p, a, q, c = video_prepaid_payments.c, video_payment_attempts.c, video_quotes.c, claims.c
    raw = sa.func.replace(sa.cast(q.id, sa.String()), "-", "")
    ref = (sa.func.substr(raw, 1, 8).concat("-").concat(sa.func.substr(raw, 9, 4)).concat("-")
           .concat(sa.func.substr(raw, 13, 4)).concat("-").concat(sa.func.substr(raw, 17, 4))
           .concat("-").concat(sa.func.substr(raw, 21, 12)))
    return sa.exists(sa.select(sa.literal(1)).select_from(
        video_prepaid_payments.join(video_payment_attempts, a.quote_id == p.quote_id)
        .join(video_quotes, q.id == p.quote_id).join(claims, c.claim_key == p.receipt_claim_key),
    ).where(
        p.job_id == hold.job_id, p.admitted_at.is_not(None), p.refund_due_at.is_(None), hold.media_result.is_not(None),
        p.amount_micro == hold.actual_micro, p.amount_micro == hold.reserved_micro,
        hold.free_micro == 0, hold.promo_micro == 0, hold.account_id == q.account_id,
        hold.external_payer == a.payer, a.status == "proven", a.proof.is_not(None),
        a.proof_hash == p.proof_hash, p.quote_commitment == q.commitment, a.quote_commitment == q.commitment,
        p.amount_micro == q.amount_micro, c.owner_kind == "video_x402", c.owner_ref == ref,
        c.transaction_hash == a.transaction_hash,
        c.chain_id == q.snapshot["payment"]["eip712_domain"]["chainId"].as_integer(),
        c.asset == q.snapshot["payment"]["asset"].as_string(),
        c.purpose["quote_commitment"].as_string() == q.commitment,
        c.purpose["authorization_identity"].as_string() == a.authorization_identity,
        c.purpose["amount_micro"].as_integer() == p.amount_micro,
        c.purpose["schema"].as_string() == "aipg.video-receipt.v1",
    ).correlate(reservations))

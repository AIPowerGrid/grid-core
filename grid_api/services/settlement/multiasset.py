# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Multi-asset Base sender — pays each account's per-asset legs (pass-through).

Consumes `revenue.compute_multiasset_payouts` output and moves each leg
(USDC / ETH / AIPG) on Base to the account's payout wallet. NO conversion —
this is the executor for the share-of-basket model (PAYOUT_EXECUTOR.md).

Inherits every money invariant from the proven AIPG rail (payouts.py):
* OFAC screen before ANY funds move (sanctions.screen, fail-closed).
* Each leg is BOUND to a treasury nonce and recorded 'pending' BEFORE
  broadcast (crash-safe). A consumed nonce we can't PROVE paid becomes
  'manual_review' — never auto-'sent', never re-sent. Idempotent per
  (period_id, account_id, asset) via grid_payout_legs.
* Proof, not trust:
    - ERC-20 legs: receipt must emit Transfer(_, address, expected_units) from
      THAT token contract. status==1 alone is not proof.
    - native ETH legs: no Transfer log exists — proof is the transaction itself
      (tx.to == address AND tx.value == expected_units) with a status-1 receipt.
* Replacement (same nonce) escalates the priority fee per attempt.
* ONE treasury nonce space: fresh nonces go above the chain's pending view AND
  the max nonce ever bound in grid_payouts OR grid_payout_legs, so this rail
  can never collide with the legacy AIPG rail.

DARK by default: without SETTLEMENT_TREASURY_PK + BASE_RPC_URL nothing can
send; without revenue pots (charging off) there is nothing to send.
"""

import datetime as _dt
import logging
import os
import uuid as _uuid

import sqlalchemy as sa

from ...database import new_session
from ...v2.schema import payout_legs as legs_t
from ...v2.schema import payouts as payouts_t
from . import assets, sanctions
from .aggregate import aggregate_den_by_account
from .revenue import compute_multiasset_payouts, revenue_pots, worker_pots

logger = logging.getLogger("grid_api.multiasset")

BASE_RPC_URL = os.getenv("BASE_RPC_URL", "")
TREASURY_PK = os.getenv("SETTLEMENT_TREASURY_PK", "")  # never logged

_ERC20_ABI = [
    {"name": "transfer", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


def _as_uuid(v):
    return v if isinstance(v, _uuid.UUID) else _uuid.UUID(str(v))


# ── persistence (idempotent per period+account+asset) ────────────────────────

async def _leg_row(period_id, account_id, asset) -> dict | None:
    async with await new_session() as s:
        r = (await s.execute(
            sa.select(legs_t.c.status, legs_t.c.nonce, legs_t.c.external_id).where(
                legs_t.c.period_id == period_id,
                legs_t.c.account_id == _as_uuid(account_id),
                legs_t.c.asset == asset,
            ))).first()
        return {"status": r[0], "nonce": r[1], "external_id": r[2]} if r else None


async def _write_leg(period_id, account_id, asset, *, address, amount, status,
                     external_id=None, nonce=None, paid=False, set_ext=True):
    """Upsert one leg. `set_ext=False` keeps the stored external_id (used when
    marking sent via the nonce check, where the winning hash may differ)."""
    async with await new_session() as s:
        existing = (await s.execute(
            sa.select(legs_t.c.id).where(
                legs_t.c.period_id == period_id,
                legs_t.c.account_id == _as_uuid(account_id),
                legs_t.c.asset == asset,
            ))).first()
        vals = dict(address=address, rail="base", amount=amount, status=status)
        if set_ext:
            vals["external_id"] = external_id
        if nonce is not None:
            vals["nonce"] = nonce
        if paid:
            vals["paid"] = _now()
        if existing:
            await s.execute(sa.update(legs_t).where(legs_t.c.id == existing[0]).values(**vals))
        else:
            await s.execute(sa.insert(legs_t).values(
                period_id=period_id, account_id=_as_uuid(account_id), asset=asset,
                created=_now(), **vals))
        await s.commit()


async def _max_bound_nonce() -> int:
    """Highest treasury nonce ever bound by EITHER rail (legacy AIPG payouts or
    multi-asset legs). Fresh nonces go above this — one shared nonce space."""
    async with await new_session() as s:
        a = (await s.execute(sa.select(sa.func.max(payouts_t.c.nonce)))).scalar()
        b = (await s.execute(sa.select(sa.func.max(legs_t.c.nonce)))).scalar()
    return max(a or -1, b or -1)


# ── on-chain plumbing ─────────────────────────────────────────────────────────

def _w3():
    if not (BASE_RPC_URL and TREASURY_PK):
        raise RuntimeError("SETTLEMENT_TREASURY_PK + BASE_RPC_URL required to send")
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    return Web3, w3, w3.eth.account.from_key(TREASURY_PK)


def _fees(w3, attempt):
    priority = w3.to_wei(0.05 * (2 ** min(attempt, 4)), "gwei")
    try:
        base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    except Exception:
        base_fee = w3.to_wei(0.1, "gwei")
    return {"maxFeePerGas": base_fee * 5 + priority, "maxPriorityFeePerGas": priority}


def _signed_leg(Web3, w3, acct, asset, to_addr, units, nonce, attempt):
    """Sign one leg: ERC-20 transfer() or a native value send."""
    spec = assets.spec(asset)
    to = Web3.to_checksum_address(to_addr)
    if spec["kind"] == "native":
        tx = {"from": acct.address, "to": to, "value": units, "nonce": nonce,
              "gas": 21000, "chainId": w3.eth.chain_id, **_fees(w3, attempt)}
        return acct.sign_transaction(tx)
    token = w3.eth.contract(address=Web3.to_checksum_address(spec["address"]), abi=_ERC20_ABI)
    tx = token.functions.transfer(to, units).build_transaction({
        "from": acct.address, "nonce": nonce, "gas": 120000, **_fees(w3, attempt)})
    return acct.sign_transaction(tx)


def _hx(x) -> str:
    s = x.hex() if hasattr(x, "hex") else str(x)
    return s.lower()[2:] if s.lower().startswith("0x") else s.lower()


def _receipt_proves_erc20(w3, rec, token_address, expected_to, expected_units) -> bool:
    """status==1 AND the receipt emitted Transfer(_, expected_to, expected_units)
    from THIS token contract. (Same proof as the AIPG rail, token-parameterized.)"""
    if not rec or rec.get("status") != 1:
        return False
    topic = _hx(w3.keccak(text="Transfer(address,address,uint256)"))
    to_pad = ("0" * 24) + expected_to.lower().replace("0x", "")
    for lg in rec.get("logs", []):
        try:
            if lg["address"].lower() != token_address.lower():
                continue
            topics = [_hx(t) for t in lg["topics"]]
            if len(topics) >= 3 and topics[0] == topic and topics[2] == to_pad:
                if int(_hx(lg["data"]) or "0", 16) == expected_units:
                    return True
        except Exception:
            continue
    return False


def _tx_proves_native(w3, tx_hash, rec, expected_to, expected_units) -> bool:
    """Native ETH proof: a status-1 receipt AND the transaction itself paid
    expected_units to expected_to (there is no Transfer log for native value)."""
    if not rec or rec.get("status") != 1:
        return False
    try:
        tx = w3.eth.get_transaction(tx_hash)
        return (str(tx.get("to") or "").lower() == expected_to.lower()
                and int(tx.get("value") or 0) == expected_units)
    except Exception:
        return False


def _verify_leg(w3, asset, tx_hash, expected_to, expected_units) -> bool:
    """Fetch tx_hash's receipt and prove THIS leg paid. Per-hash lookup only."""
    if not tx_hash:
        return False
    h = tx_hash if str(tx_hash).startswith("0x") else "0x" + str(tx_hash)
    try:
        rec = w3.eth.get_transaction_receipt(h)
    except Exception:
        return False
    spec = assets.spec(asset)
    if spec["kind"] == "native":
        return _tx_proves_native(w3, h, rec, expected_to, expected_units)
    return _receipt_proves_erc20(w3, rec, spec["address"], expected_to, expected_units)


# ── settle one leg (the payouts.py state machine, per asset) ─────────────────

async def _settle_leg(ctx, *, period_id, account_id, asset, address, amount,
                      stored_nonce, stored_tx=None, attempt=0) -> str:
    Web3, w3, acct = ctx
    units = assets.to_base_units(asset, amount)
    if units <= 0:
        return "skipped"
    mined = w3.eth.get_transaction_count(acct.address)

    # (1) Bound nonce consumed → require proof before settling.
    if stored_nonce is not None and mined > stored_nonce:
        if _verify_leg(w3, asset, stored_tx, address, units):
            await _write_leg(period_id, account_id, asset, address=address, amount=amount,
                            status="sent", nonce=stored_nonce, paid=True, set_ext=False)
            return "sent"
        await _write_leg(period_id, account_id, asset, address=address, amount=amount,
                        status="manual_review", nonce=stored_nonce, set_ext=False)
        logger.error("leg %s/%s/%s: nonce %s consumed but UNPROVEN (tx=%s) — manual_review",
                     period_id, account_id, asset, stored_nonce, stored_tx)
        return "manual_review"

    # (2) Nonce: reuse the bound one, else fresh above BOTH rails' history.
    if stored_nonce is not None:
        nonce = stored_nonce
    else:
        nonce = max(w3.eth.get_transaction_count(acct.address, "pending"),
                    (await _max_bound_nonce()) + 1)

    signed = _signed_leg(Web3, w3, acct, asset, address, units, nonce, attempt)
    h = signed.hash

    # (3) Bind BEFORE broadcast (crash-safe).
    await _write_leg(period_id, account_id, asset, address=address, amount=amount,
                    status="pending", external_id=h.hex(), nonce=nonce)

    # (4) Broadcast; in-flight dupes are not errors.
    try:
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as e:
        msg = str(e).lower()
        if not any(k in msg for k in ("already known", "nonce too low",
                                      "replacement transaction underpriced")):
            await _write_leg(period_id, account_id, asset, address=address, amount=amount,
                            status="failed", external_id=str(e)[:120], nonce=nonce)
            return "failed"

    # (5) Confirm + PROVE; timeout stays 'pending' for the next run's nonce check.
    try:
        rec = w3.eth.wait_for_transaction_receipt(h, timeout=90, poll_latency=2)
        if rec.get("status") != 1:
            await _write_leg(period_id, account_id, asset, address=address, amount=amount,
                            status="failed", external_id=h.hex(), nonce=nonce)
            return "failed"
        spec = assets.spec(asset)
        proven = (_tx_proves_native(w3, h, rec, address, units) if spec["kind"] == "native"
                  else _receipt_proves_erc20(w3, rec, spec["address"], address, units))
        if proven:
            await _write_leg(period_id, account_id, asset, address=address, amount=amount,
                            status="sent", external_id=h.hex(), nonce=nonce, paid=True)
            return "sent"
        await _write_leg(period_id, account_id, asset, address=address, amount=amount,
                        status="manual_review", external_id=h.hex(), nonce=nonce)
        logger.error("leg %s/%s/%s: status-1 receipt but leg UNPROVEN (tx=%s) — manual_review",
                     period_id, account_id, asset, h.hex())
        return "manual_review"
    except Exception:
        return "pending"


# ── period runner ─────────────────────────────────────────────────────────────

async def send_period_multiasset(start, end, period_id: str) -> dict:
    """Distribute the period's revenue pots pro-rata by den, per asset, on Base.
    OFAC-screened, idempotent per leg, safe to re-run. Accounts without a wallet
    accrue; unsupported assets in the pot are skipped loudly (never guessed)."""
    rows = await aggregate_den_by_account(start, end)
    pots = worker_pots(await revenue_pots(period_id))
    unsupported = [a for a in pots if not assets.supported(a)]
    for a in unsupported:
        logger.error("period %s: pot asset %s UNSUPPORTED on the Base rail — held", period_id, a)
    pots = {a: v for a, v in pots.items() if assets.supported(a)}
    pay = compute_multiasset_payouts(rows, pots)

    counts: dict[str, int] = {}
    def bump(k):
        counts[k] = counts.get(k, 0) + 1

    ctx = None
    if any(p["payable"] for p in pay) and BASE_RPC_URL and TREASURY_PK:
        ctx = _w3()

    for p in pay:
        # One OFAC screen per account — gates every leg to that address.
        if p["payable"]:
            screen = await sanctions.screen(p["payout_address"])
            blocked = sanctions.payable_status(screen)
        else:
            blocked = None
        for asset, amount in p["amounts"].items():
            existing = await _leg_row(period_id, p["account_id"], asset)
            if existing and existing["status"] in ("sent", "manual_review",
                                                   "blocked_sanctions"):
                bump("skipped")
                continue
            if not p["payable"]:
                await _write_leg(period_id, p["account_id"], asset, address=None,
                                amount=amount, status="accrued")
                bump("accrued")
                continue
            if blocked:
                await _write_leg(period_id, p["account_id"], asset,
                                address=p["payout_address"], amount=amount, status=blocked)
                bump(blocked)
                continue
            if ctx is None:
                bump("unsendable")  # wallet present but no treasury configured
                continue
            try:
                st = await _settle_leg(ctx, period_id=period_id, account_id=p["account_id"],
                                       asset=asset, address=p["payout_address"], amount=amount,
                                       stored_nonce=(existing or {}).get("nonce"),
                                       stored_tx=(existing or {}).get("external_id"))
                bump(st)
            except Exception as e:
                logger.error("leg error %s/%s/%s: %s", period_id, p["account_id"], asset, e)
                bump("failed")
    return {"period_id": period_id, "pots": pots, "unsupported": unsupported, **counts}

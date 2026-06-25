# Worker payouts — operations runbook

> **What is LIVE today:** the **custodial payout CLI** (`python -m
> grid_api.services.settlement.payouts`), run hourly by a systemd timer. It pays
> each worker's AIPG from a treasury **hot wallet** on Base, pro-rata to den.
> The trustless on-chain Merkle/claim design (RewardPool / PaymentRouter) is
> **NOT live** — it's the future direction, kept at the bottom of this doc. Do
> not follow the on-chain steps thinking they're the running system.

---

## 1. What's running

```
worker completes jobs ─► den (work units) recorded in grid_ledger
        │ (hourly, top of the UTC hour)
        ▼
aipg-payout.timer ─► aipg-payout.service ─► scripts/payout_hourly.sh
        │
        ├─ pays the just-completed clock hour:
        │   payouts --since <H> --until <H+1> --period-id hour-YYYY-MM-DDTHH
        │            --budget $PAYOUT_HOURLY_BUDGET --send
        └─ then self-heals any stragglers:  payouts --retry-failed
        ▼
treasury HOT WALLET sends AIPG (ERC-20) → each account's payout_wallet on Base
        ▼
recorded in grid_payouts (one row per (period_id, account_id))
```

- **Custodial:** the grid holds funds (the hot wallet) and pays on workers'
  behalf. Bootstrap rail until the trustless on-chain claim ships.
- **Attribution is by ACCOUNT** (a worker authenticates with its account key →
  `workers.account_id`). An account with no `payout_wallet` **accrues** (owed)
  and is paid the moment it sets one (`--pay-accrued`). Nothing strands.
- **den is the single source of truth** (`grid_ledger`) — shared with the future
  on-chain rail, so moving on-chain swaps the *mechanism*, not the accounting.

## 2. Config (prod)

In `/etc/aipg/grid.env` (chmod 600, root; never in git/logs/argv):

| var | meaning |
|-----|---------|
| `SETTLEMENT_TREASURY_PK` | the **hot wallet** private key — payout sender. Funded with AIPG (runway) + a little Base ETH (gas). |
| `BASE_RPC_URL` | Base RPC for reads + sends. **Use a dedicated provider** (Coinbase CDP / Alchemy) — the public `mainnet.base.org` is load-balanced and drops tx submissions. |
| `PAYOUT_HOURLY_BUDGET` | AIPG distributed per hour (default `208.33` = 5000/day). |

Hot wallet (current): `0x20A82fD11e4A5fC8d4b5A44083C05e4b28dB53B9`.
AIPG token (Base): `0xa1c0deCaFE3E9Bf06A5F29B7015CD373a9854608`.

## 3. Status lifecycle (grid_payouts.status)

```
accrued        owed, account has no payout_wallet yet (no tx)
   │ (wallet set + --pay-accrued, or next hour)
pending        tx broadcast, BOUND to a treasury nonce (grid_payouts.nonce)
   │
   ├─► sent           receipt status==1 AND the matching AIPG Transfer is proven on-chain
   ├─► failed         broadcast/revert error — retryable (reconcile re-sends at the bound nonce)
   └─► manual_review  the bound nonce was consumed but the expected Transfer can't be proven
                      (revert / replacement we didn't record / unrelated tx). NEVER auto-paid,
                      NEVER auto-resent — a human resolves it (see §6).
```

## 4. Safety properties (why re-runs / outages don't double-pay)

- **Nonce-bound:** each payout is bound to one treasury nonce; it's settled iff
  that nonce has mined **and** the AIPG Transfer is proven. One nonce ⇒ one
  payout ⇒ pays at most once.
- **Transfer proof:** `sent` requires `Transfer(_, payout_wallet, amount)` in the
  tx receipt — `status==1` alone is not enough (both confirm paths enforce this).
- **No new-nonce resends:** retries REPLACE at the bound nonce (escalating tip);
  a receipt timeout stays `pending` (unknown), resolved next run by the nonce check.
- **Collision-proof allocation:** fresh nonce = `max(chain pending, max assigned
  in DB + 1)` + a **partial UNIQUE index** on `nonce` + a **pg advisory lock**
  around any writing run (serializes concurrent runners).

## 5. Commands (dry-run by default; `--send` executes)

```bash
cd /home/aipg/system-core && set -a; . /etc/aipg/grid.env; set +a

# Preview a window (NO money):
.venv/bin/python -m grid_api.services.settlement.payouts --days 1 --budget 5000

# Send a specific hour (idempotent — re-running a settled period skips):
.venv/bin/python -m grid_api.services.settlement.payouts \
  --since 2026-06-25T13:00:00+00:00 --until 2026-06-25T14:00:00+00:00 \
  --period-id hour-2026-06-25T13 --budget 208.33 --send

# Pay accounts that just connected a wallet (their accrued balance):
.venv/bin/python -m grid_api.services.settlement.payouts --pay-accrued

# Self-heal: reconcile pending + retry failed (nonce-bound, idempotent):
.venv/bin/python -m grid_api.services.settlement.payouts --retry-failed
```

## 6. Operations

- **Kill switch (stop all automated payouts):** `systemctl stop aipg-payout.timer`
  (re-enable: `systemctl start aipg-payout.timer`). Nothing settles while stopped;
  owed den just accrues and pays when re-enabled.
- **Status:** `systemctl status aipg-payout.timer` / `journalctl -u aipg-payout.service`.
- **Resolve `manual_review`:** list them
  (`SELECT period_id, account_id, address, aipg_amount, nonce, tx_hash FROM
  grid_payouts WHERE status='manual_review'`), check the bound nonce / address on
  BaseScan. If the worker WAS paid → set `status='sent'` with the real tx_hash.
  If NOT paid and you want to re-pay → clear the row's nonce+tx_hash, set
  `status='failed'`, and run `--retry-failed` (it allocates a fresh nonce).

## 7. Canary procedure (before re-enabling after any change)

A supervised tiny live cycle proves the path end-to-end at ~zero cost (hot→hot
self-transfer of 1 AIPG through the real `_settle_one`):

1. With the timer **stopped**, run one settle of a synthetic period to the hot
   wallet itself; confirm it returns `sent` (which requires the Transfer proof).
2. Re-run the same period with the bound nonce: must return `sent` with **no
   second transfer** (nonce unchanged) — idempotency proven.
3. `--retry-failed` → no-op. Delete the canary row.
4. Acceptance: no duplicate nonce, no unexpected `manual_review`, no long-lived
   `pending`, on-chain Transfer to the expected wallet/amount, rerun does not
   rebroadcast. Only then `systemctl start aipg-payout.timer`.

---

## FUTURE (NOT LIVE): trustless on-chain claim

The endgame replaces custodial transfers with **worker-claimed** rewards: the grid
publishes a per-epoch Merkle root (worker→amount) to an on-chain RewardDistributor
on Base, funded from emissions; each worker calls `claim(proof, amount)` and pulls
AIPG directly — the grid can't withhold, and payouts are verifiable on-chain. Same
`grid_ledger` den feeds it.

**Status: not deployed.** Blockers: reward facets not live on the Base diamond;
`settlement/bot.py` is a scaffold (unfinished integrations); economics (den→AIPG
rate, model multipliers, emission schedule) not locked; no claim UI. The contract
addresses/runbook that previously lived here describe that *planned* system, not
the running one — see `bot.py` and the contracts repo when that work is picked up.
Until then, the custodial CLI above is the rail.

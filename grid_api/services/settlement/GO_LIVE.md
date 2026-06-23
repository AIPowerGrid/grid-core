# Settlement go-live runbook

Turns the den ledger into real on-chain AIPG payouts. The contracts are already
deployed and verified on Base; this is **configuration + bot ops**, not a deploy.

Money moves at steps 3–4 and step 8. Everything before that is read-only or a
dry-run. Do them in order; verify each before the next.

```
Grid diamond : 0x79F39f2a0eA476f53994812e6a8f3C8CFe08c609
AIPG token   : 0xa1c0deCaFE3E9Bf06A5F29B7015CD373a9854608
Admin (HW)   : 0xA218db26ed545f3476e6c3E827b595cf2E182533   (107M AIPG, holds ADMIN_ROLE)
RewardPool   : 0x973a82955A3baC4d7d735330090FcE3FDB8E5082
DenReporter  : 0xf06dEBc2556CeAc3caE09f934AC9aE9529760fd5
PaymentRouter: 0x3fF26503539F3e85E136fDA20042Cf2B4E3Ac65A
```

Current state (verified read-only): facets cut ✓, not paused ✓, `poolBalance=0`,
`periodAllocation=0`, `periodLengthSeconds=86400`. So steps 3 (fund), 3b (allocate),
and 5 (grant reporter) are the only on-chain changes needed.

---

## 0. Sanity: is there anything to pay?

A first payout needs ledger rows **with a wallet attached**. On prod:

```sql
-- den earned in the last closed UTC day, by wallet (NULL/'' wallets are unpayable)
SELECT wallet, count(*) jobs, sum(den) den
FROM grid_ledger
WHERE created >= date_trunc('day', now() - interval '1 day')
  AND created <  date_trunc('day', now())
GROUP BY wallet ORDER BY den DESC;
```

If every row has a NULL/empty wallet, fix worker→wallet attribution first — the
bot can't pay an address it doesn't have. The dry-run (step 6) also reports this
as "X den across N jobs has NO wallet".

## 1. Provision the reporter hot wallet (gas-only, never holds funds)

On prod, generate a fresh key — do NOT reuse the admin or any funded wallet:

```bash
cast wallet new          # prints an address + private key
```

- Put the private key in the bot's chmod-600 env as `SETTLEMENT_REPORTER_PK`
  (never on argv, never in git/logs).
- Note the **address** — it gets `REPORTER_ROLE` in step 5.
- Fund it with a tiny amount of Base ETH for gas (~0.001 ETH covers many periods;
  Base is cheap and the bot caps at `MAX_GWEI`).

A compromised reporter is bounded to **one period's allocation** (it can only post
a root for an unreported period; payouts always pull the fixed allocation split by
den). Keep the allocation small until validator co-signing lands.

## 2. Decide the starter economics (small — this is a proof)

Recommended first numbers (ramp later; `setPeriodAllocation` allows 10×/call):

| Knob | Starter | Why |
|------|---------|-----|
| Pool seed (`DEPOSIT_AIPG`) | `5000` | ~weeks of runway at the starter rate; refillable anytime |
| Per-day allocation (`ALLOCATION_AIPG`) | `100` | tiny ($≈0.12/day); proves the pipe before real emissions |
| Period length | `86400` (default) | daily; leave as-is |

With one worker, that worker takes the whole allocation regardless of size — so a
small number is a safe first live test. Ramp via `setPeriodAllocation` once it works.

## 3–5. Fund + allocate + grant reporter (admin hardware wallet)

One script does all three (`aipg-smart-contracts/scripts/deployment/configure-rewards.sh`):

```bash
cd aipg-smart-contracts
DEPOSIT_AIPG=5000 ALLOCATION_AIPG=100 \
REPORTER_BOT=0x<reporter-address-from-step-1> \
HWFLAG=--ledger \
./scripts/deployment/configure-rewards.sh
```

It runs: `approve` → `depositRewards` → `setPeriodAllocation` → `grantRole(REPORTER_ROLE, bot)`,
then prints the new pool balance + allocation. Verify:

```bash
GRID=0x79F39f2a0eA476f53994812e6a8f3C8CFe08c609
RPC=https://mainnet.base.org
cast call $GRID 'poolBalance()(uint256)'      --rpc-url $RPC   # expect 5000e18
cast call $GRID 'periodAllocation()(uint256)' --rpc-url $RPC   # expect  100e18
cast call $GRID 'hasRole(bytes32,address)(bool)' \
  $(cast keccak "REPORTER_ROLE") 0x<reporter-address> --rpc-url $RPC   # expect true
```

## 6. DRY-RUN the settlement (no transactions)

Configure the bot env on prod (DRY_RUN stays ON by default):

```bash
export BASE_RPC_URL=https://mainnet.base.org
export GRID_DIAMOND_ADDRESS=0x79F39f2a0eA476f53994812e6a8f3C8CFe08c609
export SETTLEMENT_REPORTER_PK=<from step 1>   # not needed for dry-run, fine to set
export SETTLEMENT_DRY_RUN=1                    # explicit
python -m grid_api.services.settlement.bot --once     # last closed period
```

Read the log carefully. It prints the **Merkle root**, `[DRY_RUN] reportPeriod(...)`,
and `[DRY_RUN] claimBatch(... den_sum=...)`. Confirm:
- entries match the SQL from step 0 (right wallets, sane den),
- no large "NO wallet" stranded den,
- root is non-zero, totalDen > 0.

## 7. Go/no-go

Proceed only if the dry-run looks correct and `poolBalance >= the period's payout`
(payout ≈ allocation; pool must cover it).

## 8. Flip live — settle ONE period

```bash
export SETTLEMENT_DRY_RUN=0
python -m grid_api.services.settlement.bot --once --period <same id as the dry-run>
```

This submits `reportPeriod` then `claimBatch`. Watch for the two tx hashes in the
log, then verify on-chain a real payout landed:

```bash
cast call $GRID 'isClaimed(uint256,address)(bool)' <periodId> 0x<worker-wallet> --rpc-url $RPC  # true
cast call $GRID 'totalPaidOut()(uint256)' --rpc-url $RPC                                         # > 0
cast call 0xa1c0deCaFE3E9Bf06A5F29B7015CD373a9854608 \
  'balanceOf(address)(uint256)' 0x<worker-wallet> --rpc-url $RPC                                 # increased
```

## 9. Hand off to the daily loop

Once a manual period settles cleanly, run the service (systemd) with
`SETTLEMENT_DRY_RUN=0`; it sleeps to each period boundary and settles
autonomously. `SETTLEMENT_STATE_FILE` tracks the last settled period so a
restart never double-reports or skips. The `--once` runs don't touch that state,
so your manual go-live period and the loop won't collide (the loop catches up
from its own saved cursor).

## Rollback / safety

- **Pause everything:** `cast send $GRID 'pause()' --ledger` (PAUSER_ROLE/ADMIN) —
  blocks deposits, reports, and claims.
- **Halt emissions only:** `setPeriodAllocation(0, "halt")` — unbounded down to 0.
- **Stop the bot:** stop the systemd unit; nothing settles while it's down, and it
  catches up safely when restarted.
- A reported period is immutable (no double-report); a bad root is bounded to that
  one period's allocation.
```

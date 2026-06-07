# Settlement bot

Replaces `grid-rewards-sentry`. Runs once per period (default daily) to push
worker den snapshots on-chain and trigger batch payouts.

## What's here

- `ipfs.py` — IPFS pinning helper, lifted from `grid-rewards-sentry/main.py:web3_base`
  branch with deterministic serialization added. **Ready to use.**
- `merkle.py` — Merkle tree builder matching the on-chain verify convention
  (pairwise `keccak256(min(a,b) || max(a,b))`, leaf = `keccak256(address || uint256 den)`).
  **Ready to use.**
- `bot.py` — Settlement scheduler. **Stub with TODOs.** Multisig integration
  and DB schema details need filling in before this can run.

## What's not here yet

- Safe multisig signing path for `reportPeriod()` — the bot needs to submit
  a proposal to the team Safe and wait for threshold signatures rather than
  signing directly.
- DB schema and queries for "den earned per worker per period" — currently
  the den.py service writes per-job, but there's no roll-up table.
- State tracking (which periods are reported / fully claimed) so the bot
  resumes correctly after restart.

## Architecture

```
                       ┌────────────────────────────────────┐
                       │   system-core DB (den events)      │
                       └────────────────┬───────────────────┘
                                        │ aggregate by period
                                        ▼
                            ┌───────────────────────┐
                            │   settlement bot      │
                            │                       │
                            │   1. snapshot         │
                            │   2. pin → IPFS       │
                            │   3. build Merkle     │
                            │   4. multisig sign    │
                            └───────────┬───────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
   ┌──────────────────┐     ┌──────────────────────┐    ┌────────────────────┐
   │  Pinata / IPFS   │     │   DenReporter        │    │   PaymentRouter    │
   │  (audit JSON)    │     │  .reportPeriod(...)  │    │  .claimBatch(...)  │
   └──────────────────┘     │                      │    │  × N batches       │
                            │  on Grid Diamond     │    │                    │
                            │  (Base mainnet)      │    │  on Grid Diamond   │
                            └──────────────────────┘    └────────────────────┘
                                                                  │
                                                                  ▼
                                                       ┌────────────────────┐
                                                       │   Workers receive  │
                                                       │   AIPG. No gas     │
                                                       │   for workers.     │
                                                       └────────────────────┘
```

## Operational notes

- **Hot wallet vs multisig.** The bot signs `claimBatch()` from a hot wallet
  (it never moves funds — it just pays gas). The `DenReporter.reportPeriod()`
  call comes from the team multisig so a compromised bot can't post fake
  den snapshots.
- **Gas cap.** Set `MAX_GWEI` in env (Base default ~0.06). Bot refuses to
  submit above this; sleeps and retries.
- **Resumability.** Bot tracks last fully-settled period in DB. On crash/restart,
  it picks up from there without re-reporting completed periods.
- **No bot, no settlement.** Reports are gated by `REPORTER_ROLE`. If the bot
  is down for a day, no payouts that day — but den data is still in the DB
  and can be settled retroactively when the bot comes back online.

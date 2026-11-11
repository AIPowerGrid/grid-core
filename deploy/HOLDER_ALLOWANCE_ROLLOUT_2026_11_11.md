# Holder Daily Allowance: Production Rollout

## Artifact

- Approved scope: the owner's go-ahead on 2026-11-11 to build the holder
  allowance and put it live.
- Release: `6564c2950216e9c65a79e42f94956b0cd5744c15` (PR #253), at
  `/home/aipg/releases/grid-core-6564c295`. Previous: `grid-core-0f0bad60`.
- Cutover: 2026-11-11, about 17:16 UTC. No migration (`0047` stays head), no
  dependency, unit or Nginx change.

## Configuration

Added to `/etc/aipg/grid.env`; the previous file is
`grid.env.before-holder-allowance-20261111T171557Z`.

```dotenv
GRID_FREE_HOLDER_TIERS=10000:100000,100000:500000,1000000:2000000
GRID_FREE_HOLDER_DAILY_BUDGET_MICRO=25000000
```

| AIPG in the sign-in wallet | Free per UTC day |
| ---: | ---: |
| 10,000 | $0.10 |
| 100,000 | $0.50 |
| 1,000,000 | $2.00 |

The network-wide ceiling is $25 a day. The amounts were proposed by the
agent at the $0.0010 AIPG deposit price and the owner approved going live
without changing them. `GRID_FREE_HOLDER_BONUS_MICRO=0` remains in the file and
is unused while the tier list is set.

## Verification

- PR checks passed after two runs stalled on a hosted-runner DNS failure for
  the Ubuntu mirror, before any test ran. The full `grid_api` suite also
  passed locally (2485 passed, 703 skipped).
- After cutover: `/health` reported the release SHA and 8 connected workers;
  public `/health` 200, `/v1/models` 200, unauthenticated validator
  assignments 401, `/v2/status/heartbeat` 410; no errors in the first minute
  of the journal.
- One real account: the owner's signed-in account read
  `GET /v1/account/credits` through the public hostname and received
  `holder_bonus_usd: 2.0`, `daily_cap_usd: 2.1` and the three tiers. That read
  made the day's first claim.

## Not proven live

- Spending holder credit on a job, a wallet-only account without Google, a
  wallet in the lower tiers, the budget ceiling being reached, and the next
  UTC-midnight rollover.

## Rollback

Remove the two variables (or restore the saved environment file) and restart
`aipg-gridapi`; the code path then grants nothing. Claims already made today
stay in Redis until UTC midnight but are not honored once the tiers are
unset. Repointing `/home/aipg/current` to `grid-core-0f0bad60` is also safe:
the schema is unchanged.

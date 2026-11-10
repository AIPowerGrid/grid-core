# Release a6cd1166: dependency update, payout alerting, AIPG deposits, effort compatibility

## Posture

Production selected immutable Core `a6cd11664080a1fc2f10d71351dda8c9d58db5fd`
at `2026-11-10T05:38:56Z`, replacing `0d2ee0b7`. Alembic stayed at `0046`; no
migration was pending. The owner approved the deployment and the AIPG deposit
parameters below.

Contents since `0d2ee0b7`:

- #243 - Pillow 13.0.0, pyasn1 0.6.5 and certifi 2026.10.23 past published
  advisories; test runners pinned to Ubuntu 24.04.
- #246 - payout failure and expiry alerts, the operator AIPG price file, and
  portable reasoning effort on the three text routes.

## Procedure evidence

- Release built beside the live tree from a detached clean checkout; hash-locked
  install; `pip check` clean; installed Pillow reported 13.0.0.
- Backup `grid-postgres-20261110T053621Z.dump` (312 MB) verified, then the
  restore proof passed against the candidate: `No new upgrade operations
  detected`, Alembic `0046`.
- The new settings and price file were loaded under the candidate release in a
  one-off process before any restart: settings parsed, the application
  imported, AIPG reported configured, USDC still configured.
- Unit assets installed; `systemd-analyze verify` and `nginx -t` passed. Only
  `aipg-payout.service` changed (alert hooks) and `aipg-ops-alert@.service` is
  new. The previous payout unit is retained as
  `aipg-payout.service.pre-20261110T053855Z`. The existing payout drop-in
  `40-demand-pilot-start.conf` was not touched.
- `aipg-gridapi` restarted once. `/health` answered within 13 seconds with the
  new build commit. All seven previously connected workers were back within
  about 50 seconds. No error lines in the service log after the restart.
- Payout and backup timers were enabled/active before and after. Neither the
  payout service nor the alert unit was started by the deployment.

## AIPG deposits

Enabled for the first time. `/etc/aipg/grid.env` changed
`GRID_AIPG_DEPOSITS_ENABLED` to `1` and gained six variables; the previous
file is `grid.env.before-aipg-deposits-20261110T053834Z`.

| Setting | Value |
| --- | --- |
| Price source | `GRID_AIPG_PRICE_FILE=/etc/aipg/aipg-price.json` |
| Published price | 1,000 micro-USD per AIPG, epoch `2026-11-10T0538Z`, valid to `2026-11-17T05:38:21Z` |
| Maximum price age | 691,200 seconds |
| Haircut | 300 bps |
| Per deposit | $25 |
| Per account per day | $50 |
| Network per day | $200 |

Spot on Base Uniswap was $0.001189 on 2026-11-09 with about $2,780 of pool
liquidity. Credit is therefore about 18% under spot after the haircut.
Received AIPG stays in the funding Safe.

`GET /v1/account/deposits/config` reports AIPG `available` with those values
and USDC unchanged.

## Verified live

- `reasoning_effort: high` (chat), `reasoning.effort: high` (Responses) and
  `output_config.effort: max` (Messages) return 200 on vLLM-served models that
  returned 400 before.
- Claude Code 2.1.322 pointed at `https://api.aipowergrid.io` with
  `qwen38-flash-next-nvfp4` completed a two-turn task that read a local file
  through its Read tool. It failed on its first request before this release.

## Not done

- **No real AIPG deposit has been claimed.** The rail is enabled and reports
  available, but one receipt, one credit movement, one balance increase and a
  duplicate-claim no-op have not been proven with tokens. Treat it as
  unqualified until an owner canary passes.
- No alert has been delivered through the new unit. The first scheduled payout
  run after the deployment exercises the reminder hooks.
- `POST /v1/messages/count_tokens` still returns 404, and passthrough bodies
  are still capped at 200,000 characters.

## Operating it

- Publish a new AIPG price before `2026-11-17T05:38Z`:
  `sudo /usr/bin/python3 /home/aipg/current/scripts/set_aipg_price.py --price-micro <n> --apply`.
  No restart is needed. The reminder fires in the last two days.
- To stop AIPG deposits immediately without a restart, remove or rename
  `/etc/aipg/aipg-price.json`; the rail reports `price_unavailable`.

## Rollback

Select `/home/aipg/releases/grid-core-0d2ee0b7` through the symlink, restore
`aipg-payout.service.pre-20261110T053855Z` and
`grid.env.before-aipg-deposits-20261110T053834Z`, reload systemd and restart
`aipg-gridapi`. The older release ignores `GRID_AIPG_PRICE_FILE`, so restore
the environment file with it or AIPG deposits would read the unset env price
and stay unavailable. Rolling back reintroduces the Pillow advisories.

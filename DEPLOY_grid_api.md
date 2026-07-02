# Deploying the grid_api hardening (feature/p2p-libp2p)

> Historical rollout note. The current production runbook is
> `deploy/README.md`; use that for day-2 deploys, Alembic migrations, and
> validator preview smoke checks. This file documents the older
> `feature/p2p-libp2p` hardening rollout and should not be treated as the
> canonical deploy path.

This branch contains the production-readiness work: streaming dispatch fix,
error-leak fix, no-silent-model-substitution, den persistence + anti-gaming,
free-tier quota, per-API-key rate limiting, and DB pool caps. None of it is
live until this branch is deployed.

## Pre-flight

1. **Redis must be reachable** by grid_api (DB 7). The quota and rate limiter
   both fail open if it isn't — but they only actually *function* with Redis up.
2. **New env vars** (all optional; sane defaults shown). Set in the grid_api
   process environment / .env:

   ```
   # Free-tier quota
   FREE_DAILY_LIMIT=200          # requests/day for free users
   PAID_KUDOS_THRESHOLD=1000     # kudos balance that exempts a user (paid/staked/contributor)

   # DB pool (Flask side). Budget: (DB_POOL_SIZE+DB_MAX_OVERFLOW)*num_procs < postgres max_connections
   DB_POOL_SIZE=10
   DB_MAX_OVERFLOW=15
   DB_POOL_RECYCLE=1800
   ```

3. **Raise Postgres `max_connections`** if you run many Flask procs. With 8
   procs at the defaults above that's up to 200 connections from Flask alone,
   plus grid_api's pools — set `max_connections` to ~300 or front with pgbouncer.

4. The **`grid_den_events`** table is created automatically on grid_api boot
   (idempotent, grid_api-owned). No manual migration needed.

## Deploy

```bash
# On the API host, as the deploy user:
cd /path/to/system-core
git fetch origin
git checkout feature/p2p-libp2p
git pull origin feature/p2p-libp2p

# Install any new deps (none added beyond existing requirements, but safe):
pip install -r requirements.txt

# Restart. NOTE: restart_horde.sh does pkill + nohup — a hard restart of all
# procs. Brief downtime. Consider doing this off-peak.
./restart_horde.sh
```

## Verify after deploy

```bash
# 1. Models endpoint responds (empty list is fine if no workers yet):
curl -s https://api.aipowergrid.io/v1/models

# 2. Unknown model now 404s (no silent substitution):
curl -s -X POST https://api.aipowergrid.io/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" -H "Content-Type: application/json" \
  -d '{"model":"definitely-not-real","messages":[{"role":"user","content":"hi"}]}'
# expect: 404 "Model 'definitely-not-real' is not available..."

# 3. Quota counts down (free user). After FREE_DAILY_LIMIT requests:
# expect: 429 "Free daily limit reached..."

# 4. den ledger fills as workers complete jobs:
#    SELECT count(*), sum(den) FROM grid_den_events WHERE created > now() - interval '1 hour';

# 5. Watch logs for the dispatch requeue path when a worker pops a
#    model it doesn't serve — should requeue, not strand the client.
```

## Rollback

```bash
git checkout main        # or the previously-deployed commit
./restart_horde.sh
```

(The `grid_den_events` table and new env vars are additive — rolling back the
code leaves them harmlessly in place.)

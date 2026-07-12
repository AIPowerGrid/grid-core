# Grid core production operations

## Current status

Production serves the FastAPI `grid_api` application from the `main` branch of
`AIPowerGrid/grid-core`. Database changes are Alembic-managed. The existing host
uses `/home/aipg/system-core` as its checkout path for historical reasons; the
repository name and remote are `grid-core`.

`bootstrap.sh`, the legacy Flask fleet unit, and the nginx split were written for
the old mixed Horde cutover. **Do not use `bootstrap.sh` for a new production
host in its current form.** It still clones an obsolete repository/branch and
assumes console salt sharing that no longer exists. Rebuild and review that path
before using it for fresh infrastructure.

This file documents updates to the existing managed host. It does not authorize
deploying from an agent or moving money.

## Preflight

1. Confirm the target commit is reviewed and `origin/main` is the intended
   source.
2. Back up PostgreSQL before schema changes.
3. Inspect pending migrations with `python -m alembic current` and
   `python -m alembic heads`.
4. Confirm `/etc/aipg/grid.env` is readable only by the service account and has
   the required current variables.
5. Record the state of `aipg-gridapi`, Redis, PostgreSQL, and any payout timer.
6. Refuse an in-place update when `git status --porcelain` is non-empty. Preserve
   that checkout for investigation and deploy a clean reviewed release instead;
   copying selected files into production creates an untestable runtime.

## Existing-host deploy

Run as an operator on the production host:

```bash
sudo -u aipg git -C /home/aipg/system-core fetch origin
sudo -u aipg git -C /home/aipg/system-core pull --ff-only origin main
sudo -u aipg /home/aipg/system-core/.venv/bin/pip install -r /home/aipg/system-core/requirements.txt
cd /home/aipg/system-core
sudo -E -H -u aipg ./.venv/bin/python -m alembic upgrade head
sudo systemctl restart aipg-gridapi
```

`-H` is intentional: asyncpg may otherwise inspect root's PostgreSQL client
certificate path.

Do not restart the legacy Flask fleet unless a compatibility route or shared
legacy dependency actually changed.

## Verification

```bash
systemctl status aipg-gridapi --no-pager
journalctl -u aipg-gridapi -n 200 --no-pager
curl -fsS http://127.0.0.1:7010/health
curl -fsS http://127.0.0.1:7010/v1/models
curl -fsS http://127.0.0.1:7010/v1/validator/capabilities
curl -s -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:7010/v1/validator/assignments
```

The unauthenticated validator-assignment request should return `401`. Also smoke
one authenticated non-money request through the public hostname and inspect
worker reconnects before declaring the deploy healthy.

## Money-path controls

- `GRID_CHARGING_ENABLED=0` keeps demand charging in dry-run. Do not flip it as
  part of an unrelated deploy.
- `GRID_FREE_SPENDABLE_LIVE` is a separate free-credit spending gate.
- `aipg-payout.timer` drives the live custodial worker payout CLI. Stop/restart
  it only under the settlement runbook in
  `grid_api/services/settlement/GO_LIVE.md`.
- Private payout/reporter keys stay in the service environment or secret store;
  never print them during verification.

## Rollback

Code rollback and schema rollback are separate decisions. Do not downgrade a
database by checking out old code and hoping Alembic reverses itself. Choose a
known-good commit compatible with the migrated schema, deploy it explicitly,
restart `aipg-gridapi`, and repeat the verification above. Restore a database
backup only under an incident plan.

## Asset status

- `systemd/aipg-gridapi.service` is the FastAPI unit template.
- `systemd/aipg-horde@.service` and the `/api/v2` nginx routing are legacy
  compatibility assets.
- `bootstrap.sh` is quarantined until its repo URL, branch, service topology,
  migrations, secrets, and fresh-host checks are rewritten and tested.

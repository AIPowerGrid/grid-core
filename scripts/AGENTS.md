# grid-core scripts

## Purpose

Operational and developer entrypoints that sit outside the Python packages.
This directory includes the live hourly payout wrapper, legacy queue monitoring,
tests, and an incomplete testnet model-registry helper.

## Ownership

- `payout_hourly.sh` - production systemd timer entrypoint for custodial AIPG
  payouts and failed-payout reconciliation.
- `monitor_queues.py` - legacy Flask/Horde SQL queue monitor and optional cleanup.
- `deploy_model_registry.py` - incomplete Base Sepolia ModelRegistry scaffold;
  not the production Grid Diamond deployment path.
- `run_tests.sh` - legacy test wrapper.
- `create_service_account.py` - one-time provisioning for bounded frontend or
  backend service principals; prints the new key exactly once.
- `rotate_service_key.py` - atomically revokes a service's old keys and prints
  one replacement key exactly once.

## Local Contracts

- `payout_hourly.sh` moves real funds because it always passes `--send`. Do not
  run, edit, or repoint it casually. Preserve UTC period boundaries, the
  caller-injected environment, payout idempotency, receipt verification, and
  the retry step.
- Systemd owns `/etc/aipg/grid.env`; do not source that file from the shell
  wrapper or print its values.
- `monitor_queues.py --cleanup` mutates legacy Horde tables. It does not monitor
  the Redis Streams `/v1` queue and must not be presented as current Grid queue
  observability.
- `deploy_model_registry.py` is a scaffold with no compiled deployment path.
  Never use it for Base mainnet or describe it as the canonical registry tool.

## Work Guidance

- Money-path changes belong primarily in
  `grid_api/services/settlement/payouts.py`; keep this wrapper thin.
- Add explicit dry-run defaults to any new chain, database, or cleanup tool.
- Put reusable logic in the owning service package and test it there.

## Verification

- Run `bash -n scripts/payout_hourly.sh` for wrapper edits.
- Run focused settlement tests before changing payout invocation or periods.
- Exercise monitoring/cleanup only against a disposable database.
- Run `git diff --check` and inspect commands for leaked secrets.

## Child DOX Index

No child guides are currently required; this file owns `scripts/`.

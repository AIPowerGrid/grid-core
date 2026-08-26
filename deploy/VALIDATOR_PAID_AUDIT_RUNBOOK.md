# Validator Paid-Audit Runbook

## Scope

This rail compensates workers for reviewed text-audit traffic. It does not pay
validators and grants no routing, reputation, strike, bond, or slashing
authority. Keep it off until the supervised canary below is approved.

## Preconditions

1. Deploy reviewed code only after Alembic `0026` succeeds and `alembic check`
   reports no drift.
2. Keep `VALIDATOR_PAID_AUDIT_ENABLED=0` during migration and process restart.
3. Confirm the validator node's assignment journal and dead-letter CLI are the
   reviewed release version.
4. Select one reviewed validator wallet and one non-owner worker for the canary.
5. Choose small positive limits for every scope:
   `VALIDATOR_PAID_AUDIT_DAILY_DEN`, `VALIDATOR_PAID_AUDIT_HOURLY_DEN`,
   `VALIDATOR_PAID_AUDIT_PER_VALIDATOR_DAILY_DEN`,
   `VALIDATOR_PAID_AUDIT_PER_WORKER_DAILY_DEN`, and
   `VALIDATOR_PAID_AUDIT_MAX_DEN_PER_JOB`. The per-job limit must not exceed
   any broader limit.

## Dark Verification

With the enable flag still `0`, verify `/v1/validator/capabilities` reports paid
worker audits disabled and economic effect `none`. Verify assignment health has
no paid-audit holds. Existing evidence-only probes should continue to return a
zero-den worker acknowledgment; this is expected and remains recognizable.

## Supervised Canary

1. Set the exact reviewed wallet in `VALIDATOR_PAID_AUDIT_WALLETS`, configure
   the small limits, then set `VALIDATOR_PAID_AUDIT_ENABLED=1`.
2. Request one text assignment from that validator. An unlisted validator must
   fail closed before receiving paid work.
3. Observe one reservation move `held -> settled`, one `grid_ledger` row, one
   ordinary positive-den worker ACK, and one completed assignment result.
4. Reclaim or replay the same queue job. It must publish the stored result
   without dispatching the worker and without adding a second ledger row.
5. Exceed each cap in a controlled test. The extra request must fail before GPU
   dispatch and the aggregate reservation health must show no invariant breach.
6. Confirm validator evidence remains in the protocol/capability dimension and
   has no routing, reward, strike, or slash consumer.

## Recovery

- A stale `held` reservation is released by the Core sweeper; late success is
  terminally rejected and cannot mint payout.
- A `settled` reservation carries its synthetic terminal result. Redis stale
  reclaim republishes it without a second GPU run or payout.
- A validator crash after receiving the result is recovered by its assignment
  journal and attestation outbox. Operators inspect `aipg-validator queue
  status` and explicitly run `aipg-validator queue retry-dead` after correcting
  the underlying cause.
- `GET /v1/validator/assignments/health` exposes only aggregate held, settled,
  released, stale, and invariant-breach counts. It does not expose prompts,
  outputs, wallets, workers, assignments, or signatures.

## Rollback

Set `VALIDATOR_PAID_AUDIT_ENABLED=0` to stop issuing new paid assignments.
Do not delete budget or reservation rows. Already-held work must still settle
or release through its durable lifecycle. Keep all validator evidence economic
effects disabled while investigating the canary.

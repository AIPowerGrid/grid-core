# alembic - grid database migrations

## Purpose

Migration source for Grid-owned database tables. These migrations must make the
production database match the Grid-owned schema contracts without relying on
`create_all(checkfirst=True)` to alter existing tables.

## Ownership

- `env.py` - Alembic environment.
- `script.py.mako` - revision template.
- `versions/` - ordered migration revisions. Current source head: `0052`
  (`0009` payout-pref cols, `0010` grid_revenue, `0011` grid_payout_legs,
  `0012` reservations.free_micro, `0013` universal identities, scoped keys,
  promotional grants, and reservations.promo_micro; `0014` codifies safe DB
  defaults for Grid-native inserts into the optional legacy waiting-prompts
  table; `0015` adds native service clients, expiring-key metadata, and
  reservation-time price snapshots; `0016` reconciles early production
  constraint drift for ledger idempotency and validator evidence; `0017` adds
  immutable Base deposit receipts committed with purchased-credit movements;
  `0018` records x402 reservation provenance and on-chain settlement receipts;
  `0019` adds service SIWE domain binding; `0020` adds registered validator
  identities and binds assignments/attestations to them; `0021` adds bounded,
  reclaimable validator-probe leases; `0022` adds shared probe groups and
  distinct-validator quorum constraints; `0023` adds the fail-closed bonded
  media reference-worker snapshot pool; `0024` adds one leased media execution
  and one committed frozen witness set per probe group; `0025` adds a bounded
  durable assignment result used to recover completed-but-unattested probes;
  `0026` adds opaque operator groups, expiring independence-review
  state, and bounded qualification heartbeat samples; `0027` persists finalized
  WorkerRegistry block/facet proofs plus one authority-scoped sync cursor;
  `0028` adds private, expiring, identity-bound media-worker common-control
  reviews; `0029` adds the private compensated-audit job and hourly budget-
  counter foundation, without enabling audit dispatch or worker payout;
  `0030` adds bounded validator/account pairing slots and signed visibility
  associations, without moving node accounts or enabling any economics;
  `0031` adds empty OAuth public-client and authorization-state tables for the
  disabled-by-default remote MCP authorization foundation; `0032` adds private
  validator shadow-run, observation, outcome, capacity-sample, and bounded-error
  tables without enabling collection, routing, or economics; `0033` adds the
  database-enforced single-running-shadow invariant; `0034` adds the stable,
  private per-job commitment required for exact ledger coverage; `0035` adds
  an initially empty bounded recent-heartbeat window without rewriting existing
  qualification timestamps, counters or reviews).
  `0036` adds empty private validator compensation contracts, allocations and
  globally unique work claims. It neither funds a campaign nor creates a payout.
  `0037` adds empty immutable, allocation-bound validator payout consent.
  It does not migrate account wallets into recipients or enable any sender.
  `0038` adds immutable validator transaction bytes, nonce and receipt state;
  it grants no payment approval and enables no background sender.
  `0039` adds empty bounded operator wallet/node consent requests; no recipient
  is approved and no account wallet is adopted automatically.
  `0040` adds nullable private media recovery metadata to reservations without
  changing historical credit, completion, reward, or identity records.
  `0041` adds empty immutable hourly worker payout plans. It adopts no historical
  payout row and does not enable any sender or timer.
  `0042` adds zero-default externally funded credit lineage.
  `0043` adds empty private async media request/execution journals. Candidate
  submission defaults off; the bounded outbox still drains accepted work.
  `0044` adds nullable original-model/session leases and an original-worker
  recovery index without reconstructing any historical execution authority.
  `0045` adds nullable exact private-source retention authority and cleanup
  timestamps, without adopting historical URLs or changing economic records.
  `0046` creates empty immutable private video quotations, with owner/key,
  job UUID and derived-nonce uniqueness, positive bounded amount and expiry
  constraints. No signature, reservation, payment, dispatch or backfill exists.
  `0047` creates empty quote-bound payment attempts and immutable receipt
  evidence, with unique authorization/transaction and coherent review/proof
  constraints. It stores no spending signature and grants no funded admission.
  `0048` creates empty shared asset/transaction and economic-reference claims.
  It invents no historical receipt/token provenance and enables no video API.
  `0049` creates empty private quote-input staging with exact object authority,
  lease fencing and stored-byte proof. No payment, outbox or public route exists.
  `0050` adds nullable abandonment and repeatable cleanup-attempt/ACK metadata,
  with coherent state constraints and a rotation index. No history is closed
  automatically and no historical URL becomes deletion authority.
  `0051` creates empty quote/receipt-bound prepaid video funding and unsent
  refund obligations. No historical payment, hold or job is admitted.
  `0052` creates empty reviewed Safe refund intents/proofs with unique nonce/
  receipt bindings. Shared receipt ownership constraints are unchanged; no
  signature, sender or historical refund is adopted.

## Local Contracts

- Apply `0052` before optional private refund tooling. Keep nonce-bound intent
  and proof-aware tools plus all recorded history on rollback. Downgrade refuses
  any prepared/refunded row or retained refund-namespaced receipt claim; source
  obligations remain historical after repayment. No ordinary route or loop
  reads this optional table, and migration enables no signing or sending.

- Apply `0051` before candidate financial/backing readers start, even with
  video payment dark. Retain new `video_prepaid` terminal/reward handlers and
  every payment/refund row on rollback; older release code could restore credit
  instead of recording USDC owed. Downgrade refuses all nonempty history.
  Migration creates no payment sender or enabled admission API.

- Apply `0050` before using the updated optional staging/first-attempt service
  or enabling `VIDEO_UNPAID_CLEANUP_ENABLED`; default-off startup does not read
  this table. Keep abandonment-aware code and retained metadata on rollback;
  downgrade refuses any cleanup history, including unsuccessful rotation.
  Every recorded payment attempt excludes its inputs from this cleanup.

- Apply `0049` before calling optional private quote staging. Keys commit before
  storage I/O; no historical inputs are adopted. Retain rows on rollback;
  downgrade refuses even a partial allocation. Candidate `0050` adds repeatable
  attempt-free cleanup; transfer to funded-job retention is still unimplemented.
  Provider lifecycle remains a backstop, not application cleanup proof.

- Apply `0048` before any updated deposit or text-x402 reconciliation writer,
  even with video disabled. Quiesce all financial writers before the migration/
  release switch and upgrade every replica/offline runner before resuming.
  Older writers can bypass these claims, so a rolling mixed-writer deployment
  is unsafe. Historical books are exclusion evidence, not fabricated backfills.
  Rollback retains this table and claim-aware writers; downgrade refuses any
  retained ownership. Shared ownership alone grants no funded video admission.

- Apply `0047` before using the optional non-routable video attempt journal.
  New initial `begin` calls now additionally require current ready staging
  through `0050`;
  historical attempt reads acquire no new staging or send authority.
  Normal credit/deposit/text x402 and quote routes do not query the new table.
  Application rollback retains it; downgrade refuses any attempt, including
  unresolved `settling` and reviewed state. No historical attempt/payment is
  reconstructed, and no sender, reserve, outbox or API is enabled by migration.

- Apply `0046` before using the optional quote routes, even for recovery after
  their creation flag is rolled back. Ordinary video/credit paths do not read
  this new table. Retain it on rollback; downgrade refuses any quotation,
  including expired state, rather than silently freeing a reused request key.

- Apply through `0045` before starting this candidate, even with admission off.
  Ordinary journal reads and the independent source-cleanup loop select the new
  columns. Retain them on application rollback. Downgrade refuses recorded
  source authority or cleanup history, including completed/refunded jobs.

- Apply through `0044` before starting this candidate, even dark. Downgrade
  refuses any stored lease, including terminal jobs. Retain both lease-aware
  financial and worker transport code on rollback after first use; older writers
  could let a replaced connection charge or refund its successor. Existing NULL
  lease rows stay uncertain until the existing financial backstop closes them.

- Apply `0043` before creating any async media job. No history is backfilled,
  paid job is queued or API is enabled by migration. Retain the table and
  execution-fence-aware worker transport on rollback after its first use;
  downgrade refuses any stored identity, including refunded or completed jobs.

- `0042` adds zero-default funded balance/ledger metadata and the funded-subset
  constraint without changing spendable balances. Drain holds and pause credit
  writers before the reviewed opening reconciliation, then deploy every
  provenance-aware writer before resuming. Downgrade refuses any nonzero
  funded history, even when it has already been spent. Application rollback
  must retain lineage-aware writers; an old writer can silently omit funded
  movements. See `docs/architecture/FUNDED_CREDIT_LINEAGE.md`.

- Apply `0041` before running the frozen-period worker sender. Retain it on
  application rollback; downgrade refuses any recorded plan, including an
  empty allocation for a closed hour. Historical obligations are not backfilled.

- Apply `0040` before deploying media result recovery: ordinary reservation
  inserts and terminal UPDATEs reference its columns, including text jobs.
  Roll back the application while retaining these additive columns; explicit
  downgrade refuses any existing client correlation or media result. Migration
  presence is not proof of Gallery recovery or a successful paid canary.

- **Hot-path columns migrate FIRST:** `0009`'s payout-preference columns are
  SELECTed by `resolve_api_key` on every request — deploying code before the
  migration fails v2 auth GLOBALLY. Prod is create_all + manual ALTER (see the
  prod-schema notes); these migrations keep every other deploy path safe.
- Use `op.batch_alter_table` for column alters so SQLite (gateway-in-a-box)
  can upgrade to head — plain `ALTER COLUMN` broke it once (`0008`).

- Every schema change in `grid_api/v2/schema.py` requires a matching Alembic
  revision, including constraints and indexes.
- Deploy through `0022` before shared-quorum validator code. Older evidence remains
  readable because its new `validator_id` is nullable; all newly authoritative
  evidence must be registration-bound in application code.
- A validator assignment may have only one active probe lease. Retries are
  bounded, and a late result may update only the matching current job id. A
  completed assignment may replay only its stored result to the same registered
  validator during the attestation grace window.
- `0026` is operational metadata, not a trust grant. Existing registrations
  backfill to `unreviewed`; only the preview-first maintainer review tool may
  move them through candidate qualification to an expiring verified state.
- `grid_validator_reference_workers` is a background-sync cache, not worker
  self-report. Keep it empty until the reviewed cooldown-backed WorkerRegistry
  is deployed; media validation must fail closed when snapshots are stale or
  identities are not independent.
- Apply `0027` before any release that starts the bond-sync loop or selects
  bonded references. Existing rows backfill with NULL proof fields and remain
  ineligible until a successful reviewed sync writes the full proof.
- Apply `0028` before any release with the three-control-domain media selector.
  It intentionally backfills no trust: existing candidates and references stay
  ineligible until a preview-first worker-control review is explicitly applied.
- Apply `0029` before any code may reserve a compensated validator audit. The
  migration creates empty private state only: it backfills no budget, queues no
  work, and grants no validator or worker economic authority.
- Apply `0030` before enabling optional validator-account pairing. The new
  tables start empty, and existing validator/authentication paths do not query
  them. Operational rollback is flag-off, not deleting identity configuration
  or downgrading the database.
- Apply `0031` before deploying remote MCP OAuth code. The migration only adds
  empty tables and indexes; it does not register clients, authorize accounts,
  issue tokens, or enable routes. Keep `GRID_MCP_OAUTH_ENABLED=0` until the
  separate Console consent and remote MCP rollout gates pass.
- Apply `0032` before any validator shadow collection is enabled. A dark
  migration creates empty observer tables only. Production routing and economic
  paths do not query them, and operational rollback is
  `VALIDATOR_SHADOW_OBSERVER_ENABLED=0`, not deleting replay evidence or
  downgrading the database.
- Apply `0033` before starting a shadow run. It prevents overlapping experiments
  even when two operators race the preview/apply workflow.
- Apply `0034` before enabling shadow collection. It fails closed unless the
  observation table is empty because historical route attempts cannot be
  linked to raw ledger job ids without fabricating evidence.
- Migrations must be idempotent only where Alembic expects them to be; do not
  hide failed DDL with broad exception swallowing.
- Apply `0035` before deploying recovery-window code: validator reads select
  these columns. It backfills no heartbeat evidence or independence. During
  the first 72 hours of real collection the existing coverage basis is preserved.
  Rollback the application while retaining the additive columns; explicit
  downgrade discards recent observations, never the legacy qualification fields.
- Economic constraints matter: unique `grid_ledger.job_id`, non-null credit refs
  for value-moving rows, and FK consistency are money-safety properties.
- Apply `0036` before using the manual validator allocation command. No ordinary
  API or timer reads these tables. Rollback retains them; explicit downgrade
  refuses any nonempty compensation history, including an open contract.
- Apply `0037` before using the private recipient-consent command. Application
  rollback keeps the table; explicit downgrade refuses existing consent rows.
- Apply `0038` before updated worker payout code runs: its shared nonce lookup
  reads the new table even with validator sending off. Before any validator
  transfer, upgrade every treasury-sharing runner. After a validator nonce is
  bound, do not roll back to worker code that cannot see it. Retain financial
  history; explicit downgrade refuses any payment row.
- Apply `0039` before enabling `VALIDATOR_COMPENSATION_OPERATOR_ENABLED`.
  Rollback disables the flag and retains pending proof; downgrade refuses
  nonempty request state. This flag never enables payment sending.
- Do not edit or depend on generated `__pycache__` files.

## Work Guidance

- Name revisions with the next sequence and a short description.
- Include data backfill or validation steps when tightening nullable columns.
- Keep downgrade honest; if a downgrade is unsafe, state that explicitly in code
  comments.
- After changing migrations, update deploy/runbooks if production upgrade
  commands or order change.

## Verification

- Run migration upgrade/downgrade against a disposable DB when practical.
- At minimum, run `pytest grid_api/services/tests/test_credits_billing.py` for
  credit-ledger or account schema changes.

## Child DOX Index

- None - leaf.

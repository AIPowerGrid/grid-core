# Base funding canary runbook

Status: **commands and schema flow tested on disposable Postgres; real Base
transfers require operator authorization.**

This runbook is the release gate for accepting Base USDC. It does not authorize
a production deploy, a treasury change, or a transfer. AIPG, direct ETH, and
x402 remain separate flags and must not be enabled by accident.

## Invariants

- Purchased balances and ledger movements are integer micro-USD.
- Credits buy Grid service. They are non-transferable and nonwithdrawable.
- One Base transaction creates at most one immutable deposit receipt and one
  positive credit-ledger movement.
- The claiming wallet must already be linked to the authenticated account.
- A duplicate claim is a read-only idempotent success.
- Worker payout from x402 work remains blocked until the payment row is
  durably `settled`.

## 1. Dark deploy

Deploy migration `0018` before the application release. Keep every new rail
dark:

```dotenv
GRID_DEPOSITS_ENABLED=0
GRID_AIPG_DEPOSITS_ENABLED=0
GRID_ETH_CONVERSION_MODE=disabled
GRID_X402_ENABLED=0
```

Run from the immutable release directory:

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic check
```

Expected: current revision `0018` and `No new upgrade operations detected`.
Confirm Core health and ordinary free/paid inference before changing a funding
flag.

## 2. USDC preflight

Use a dedicated, monitored Base treasury address. Core needs only its public
address and RPC URL; it must not hold the treasury or payout private key.

The production target is Base Safe
[`0xd19a391FAB4aeFd5f59e4be4918364f57b9c5346`](https://basescan.org/address/0xd19a391FAB4aeFd5f59e4be4918364f57b9c5346).
Read-only Base verification on 2026-07-27 found deployed bytecode, two distinct
owners, and threshold 2. It is dark-configured in Core; this entry does not
authorize enabling deposits or sending the canary.

```dotenv
GRID_BASE_CHAIN_ID=8453
GRID_BASE_RPC=<monitored Base RPC>
GRID_DEPOSIT_CONFIRMATIONS=3
GRID_DEPOSIT_MIN_MICRO=10000
GRID_USDC_MAX_DEPOSIT_MICRO=10000000000
GRID_USDC_ACCOUNT_DAILY_MICRO=25000000000
GRID_USDC_NETWORK_DAILY_MICRO=100000000000
GRID_USDC_CONTRACT=0x833589fcd6edb6e08f4c7c32d4f71b54bda02913
GRID_USDC_TREASURY=0xd19a391FAB4aeFd5f59e4be4918364f57b9c5346
GRID_DEPOSITS_ENABLED=1
GRID_AIPG_DEPOSITS_ENABLED=0
GRID_ETH_CONVERSION_MODE=disabled
GRID_X402_ENABLED=0
```

Restart Core and verify authenticated
`GET /v1/account/deposits/config` reports:

- chain id `8453`;
- USDC enabled with 6 decimals;
- the intended treasury and canonical USDC contract;
- credits non-transferable and nonwithdrawable;
- AIPG unavailable and ETH direct-send disabled.

Stop if any value differs.

## 3. $0.01 USDC canary

1. Sign in to the Console with the operator account.
2. Link the Base wallet that will send the canary.
3. Open Funding and send exactly `0.01 USDC` to the displayed treasury.
4. Wait for the configured confirmations and claim the transaction.
5. Record the account id, transaction hash, pre/post balance, receipt id, block,
   source wallet, and timestamp in the release evidence.

The UI must show one immutable USDC receipt and a `+0.01` purchased-balance
change. BaseScan must show canonical Base USDC moving directly from the linked
wallet to the configured treasury.

Database evidence:

```sql
SELECT account_id, chain_id, asset, token_address, tx_hash, block_number,
       from_address, treasury_address, amount_raw, amount_decimals,
       price_micro, price_source, credited_micro, refund_address, status
FROM grid_deposits
WHERE chain_id = 8453 AND asset = 'USDC' AND tx_hash = '<lowercase tx hash>';

SELECT account_id, delta_micro, reason, ref
FROM grid_credit_ledger
WHERE ref = 'base:8453:usdc:<lowercase tx hash>';
```

Expected: exactly one row from each query, `amount_raw=10000`,
`price_micro=1000000`, `credited_micro=10000`, `delta_micro=10000`, and the
same account id.

For that account, prove the purchased-balance cache equals its append-only
ledger:

```sql
SELECT c.balance_micro, l.ledger_micro
FROM grid_credits c
JOIN (
  SELECT account_id, SUM(delta_micro) AS ledger_micro
  FROM grid_credit_ledger
  WHERE account_id = '<account uuid>'
  GROUP BY account_id
) l ON l.account_id = c.account_id;
```

Expected: `balance_micro = ledger_micro`.

## 4. Replay proof

Submit the same transaction hash through
`POST /v1/account/deposits/claim` using the same authenticated account.
Expected: `already_claimed=true`, the same receipt, no balance change, and still
exactly one deposit row and one ledger row.

Attempting to claim the transaction from a different account must fail because
the on-chain sender is not that account's linked wallet.

## 5. x402 canary

### Read-only checks while disabled

Keep `GRID_X402_ENABLED=0` until the configuration and supervised payment
canaries are approved. With the intended environment loaded, run:

```bash
.venv/bin/python -m grid_api.services.x402_payments --preflight
```

This command checks the recipient/token configuration, receipt verifier's
chain and canonical USDC, the actual RPC chain ID, and the facilitator's
advertised v2 `upto` support. It works while the rail is disabled. It does not
connect to the database, dispatch inference, sign a payment, send funds, or
enable the rail. A nonzero exit reports only a bounded error type, not raw
SDK/RPC exceptions or authenticated URLs. The successful report's `enabled`
field reflects the existing flag, not a change to it.

For an isolated Sepolia deployment, configure **all** of:

- `GRID_X402_NETWORK=eip155:84532`
- `GRID_BASE_CHAIN_ID=84532`
- `GRID_BASE_RPC=https://sepolia.base.org` (or a reviewed Sepolia RPC)
- `GRID_USDC_CONTRACT=0x036cbd53842c5426634e7929541ec2318f3dcf7e`
- `GRID_X402_FACILITATOR_URL=https://x402.org/facilitator`
- `GRID_X402_PAY_TO=<reviewed Sepolia recipient>`
- `GRID_X402_MAX_AUTH_MICRO=50000`

Use a dedicated staging environment/database. Do not change the shared
production deposit verifier's chain or token to testnet. `GRID_BASE_RPC` is the
receipt verifier setting; another component's `BASE_RPC_URL` does not configure
it. The public test facilitator advertises Sepolia support; Base mainnet uses
the authenticated CDP facilitator and dedicated credentials. Do not reuse
another project's CDP key. Restrict a new facilitator key to Core's verified
outbound IP and leave unrelated wallet/portfolio permissions disabled.

### Paid proof

Do this only after the USDC deposit canary passes. Start on Base Sepolia with
matching test USDC, facilitator, network, and treasury values. Use a fresh
low-value payer and `GRID_X402_MAX_AUTH_MICRO=50000` ($0.05 maximum).

Evidence required:

1. Initial request returns a valid x402 payment requirement.
2. Paid retry produces one non-streaming text completion.
3. Reservation records grid-counted actual usage below the authorization.
4. Payment transitions `verified -> settling -> reported -> settled`.
5. Base receipt proves canonical USDC from payer to recipient for exactly
   `settled_micro`.
6. One repeated authorization cannot open or settle a second job.
7. Worker payout excludes `verified`, `settling`, `reported`, and
   `manual_review`, then includes only independently proven `settled`.
8. A forced ambiguous failure enters `manual_review` and alerts.

Capture the exact candidate SHA, SDK version, network, capped authorization,
trusted actual charge, Grid job ID, transaction hash and final receipt state.
Never include signatures, payer private keys, API keys or JWTs in the evidence.
A paid response lost after transfer must not be described as unpaid merely
because the client received a 402: retain the durable attempt and reconcile it.

For an ambiguous real transfer, reconcile only with the exact Base transaction:

```bash
.venv/bin/python -m grid_api.services.x402_payments \
  --reconcile-job <job-uuid> \
  --tx <base-transaction-hash>
```

Repeat on Base mainnet with the same $0.05 ceiling only after Sepolia passes.
Do not raise the ceiling or add streaming/media until an automated chain
indexer/reconciler is deployed.

### Verification checkpoint: October 14, 2026

- Production was inspected with x402 disabled. Its unpaid route returned 404;
  CDP facilitator credentials were absent. This is an observation, not an
  authorization to change production configuration.
- The real pinned x402 2.16.0 SDK returned an unpaid 402 challenge using the
  official public Sepolia facilitator; the protected handler was not called.
  The read-only preflight also passed against the real Sepolia RPC/facilitator
  with the feature flag still off. No signatures or funds were used.
- `test_x402_http_runtime.py` covers the actual SDK, production router and
  FastAPI middleware with **synthetic facilitator verification/settlement and
  worker output**. It checks actual-vs-ceiling settlement, replay rejection,
  invalid/malformed payments, streaming rejection, durable attempt failures,
  lost-receipt payout exclusion and redacted failure logs. It is not paid E2E.
- The x402 duplicate-authorization and single-settlement races passed on a
  disposable local PostgreSQL 16.15 instance. The targeted x402/credits race/
  safe-logging suite passed 51 cases on Python 3.12.9 with hash-locked production
  dependencies; adjacent deposit/credit/free-allowance/quote/payout regressions
  passed another 141 cases. These are targeted checks, not the full Grid suite.
  No live financial tables were used.
- **Not yet proven:** a paid Sepolia transaction, a paid mainnet completion,
  or a real worker/chain/receipt end-to-end result. Production enablement and
  mainnet spending require separate explicit operator approval.

### Credential checkpoint: October 15, 2026

- The owner approved a dedicated CDP facilitator credential with Core's
  verified outbound-IP allowlist and no portfolio/wallet permissions. The
  original credential's one-time secret was not retained, so an equivalent
  replacement was created after owner-completed Coinbase verification.
- The replacement was matched to the open Coinbase key window, transferred
  over SSH stdin, and installed only as `CDP_API_KEY_ID` and
  `CDP_API_KEY_SECRET` in root-owned mode-0600 `/etc/aipg/grid.env`. The install
  preserved every other parsed environment field, retained a root-only backup,
  and did not restart any service. Temporary local credential copies were
  removed after verification. Codebase credentials were not reused or changed.
- A separate read-only process using the deployed `1bca2948` release and the
  protected environment successfully authenticated the CDP facilitator's
  `/supported` request, confirmed v2 `upto` support on `eip155:8453`, and checked
  the actual receipt RPC chain ID and canonical Base USDC configuration.
- `GRID_X402_ENABLED=0` remains unchanged. No verify/settle request, wallet
  signature, payment, inference job, deployment or service restart was performed.
  Authentication/support discovery is not a paid end-to-end proof; the paid
  Sepolia/mainnet gates above remain open.
- After separate explicit owner approval, the inaccessible original credential
  was disabled, not deleted. The replacement remained enabled and passed the
  authenticated facilitator/RPC check again. Other projects' keys were unchanged.

### Paid Sepolia checkpoint: October 15, 2026

The isolated protocol canary passed using source `3fbd48a3`, pinned x402 2.16.0,
Python 3.12.9 and a private local PostgreSQL 16.15 instance. See the
[public structured evidence](X402_SEPOLIA_CANARY_2026_10_15.json).

- Coinbase's free faucet funded a fresh disposable testnet payer. No Ledger,
  production payer, live financial database or mainnet funds were used.
- The actual Core router and official SDK/FastAPI middleware returned initial
  402, accepted an SDK-created signed `upto` authorization, and returned 200
  after the real public facilitator settled canonical Sepolia USDC.
- The authorization ceiling was 50,000 micro-USDC. Trusted fixture terminal
  usage settled 23 micro-USDC, independently verified as exactly one payer-to-
  recipient Transfer in block 48152592. The
  [settlement transaction](https://sepolia.basescan.org/tx/0x935b414e922a444c3aa7abccc9490a3644fe58a11b13462d5da647518d091e48)
  passed Core's own receipt/confirmation verifier before becoming `settled`.
- Observed states were `verified -> settling -> reported -> settled`. A replay
  returned 402 with still one job, reservation, payment row and settlement
  attempt; neither balances nor transfer count changed again. No purchased
  credits or credit-ledger rows were created.
- The synthetic worker's ledger entry was excluded from aggregation while
  `reported` and included only after independent chain proof. No actual worker
  or validator payout was sent.
- The remaining test-USDC Permit2 allowance was
  [revoked](https://sepolia.basescan.org/tx/0x186094b8e2ed165fc566a6355528f7d2d788e6741b595df114901d83407aa5bd)
  and independently read back as zero. Immediate public-RPC state reads lagged
  mined approval receipts; subsequent read-only checks confirmed both approval
  and revocation without broadcasting duplicates.

**Boundary:** HTTP used in-process ASGI, not a publicly deployed Core instance.
The worker account, output, usage and ledger entry were explicit synthetic
fixtures; no real model/GPU or Grid worker transport ran. Real signatures,
facilitator calls, test tokens, chain settlement and receipt reconciliation were
not mocked. This closes the paid Sepolia protocol gate only. The real worker,
bounded mainnet canary and deployment/admission gates remain open. Production
x402 stayed disabled and no production configuration changed for this canary.

The post-canary replay review found that the original Core fingerprint used
the nonce's JSON text, whereas the SDK's EIP-712 message uses its uint256 value.
Leading-zero aliases produced different authorization IDs in two reproduced
failing regressions. The candidate now validates a bounded ASCII decimal
uint256 and hashes its canonical numeric spelling. SQLite reservation and
twenty-way PostgreSQL alias races pin one job per numeric authorization. This
is a candidate hardening change, not a claim that an attacker exploited the
disabled production rail. The updated targeted selection passed 208 cases
(67 x402/concurrency/safe-log cases plus 141 adjacent money-path regressions)
on the same locked Python 3.12/PostgreSQL 16 test setup.

The real Sepolia protocol run was repeated on exact hardened source
`1acd661f`. The facilitator accepted the unchanged signature with a leading-zero
nonce; Core assigned it the canonical authorization identity. That paid retry
returned 200 and independently settled exactly 23 micro-USDC in
[block 48152961](https://sepolia.basescan.org/tx/0x8534d3bc40a7076fb34ce360e002a956fd7681714f9a62ce77aa8b44e1c87a46).
Replaying the canonical nonce spelling returned 402 with still one job and
one settlement attempt. The remaining allowance was again revoked and verified
as zero. Both runs retain the same synthetic-worker/ASGI qualification boundary;
neither is a mainnet or deployed-GPU claim.

### Dark deployment checkpoint: October 15, 2026

The owner-approved PR228 tree is deployed as exact main commit
`607d1c03bdab58350e63b8e966555d593b5b1565` / Alembic `0045` after green PR
and main checks, fresh production restore proof and a drained cutover. Eight
workers/models and all existing runtime controls are preserved. Independent
origin/public checks and the selected release's read-only preflight pass.
Ordinary credit charging remains on; x402 remains off with no payment rows.
See [the deployment record](../deploy/X402_DARK_2026_10_15.md), including the
retained operator-probe rollbacks. No real-GPU or mainnet canary is claimed;
those require separate bounded approval before any public admission.

### Private mainnet checkpoint: October 15, 2026

A subsequent owner-approved USD 0.03 USDC / USD 0.10 gas private test on the
same deployed source completed real `gpt-oss-120b` worker usage, but returned
402 during settlement. The payment has one attempt, actual usage 7 micro-USDC
and no transaction hash; it is not revenue or payout-eligible. No user credit
ledger entry moved. The full dedicated USD 0.05 USDC balance remains and its
Permit2 allowance is independently verified zero after revocation. Approval
and cleanup used approximately USD 0.00131 gas. Replay was not exercised;
no paid retry or public enablement occurred. See the deployment record's
[failed mainnet attempt](../deploy/X402_DARK_2026_10_15.md#subsequent-private-mainnet-attempt-settlement-failed)
for retained evidence, diagnostic limitations and remaining gates. This
supersedes the empty-payment-table observation at the earlier dark cutover.

## 6. Holds

- **AIPG:** keep dark until a named price-epoch owner, expiry alert, refund
  owner, and conservative exposure caps are operational.
- **ETH:** Core can verify `swap_receipt` and credit only actual canonical USDC
  proceeds, but the Console still needs a reviewed quote/router adapter and
  transaction-intent binding.
- **Cards:** later adapter into the same non-transferable credit ledger; no
  separate balance system.

## Rollback

Set `GRID_DEPOSITS_ENABLED=0` and `GRID_X402_ENABLED=0`, then restart Core.
This stops new claims and x402 requests without deleting receipts or changing
existing balances. Never roll back by deleting economic rows. Investigate any
credited mistake through the refund/adjustment process with a new durable ledger
reference.

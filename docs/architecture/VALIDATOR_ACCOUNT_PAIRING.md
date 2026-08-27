# Validator Account Pairing

Status: Core implementation under review, default off. The Console and local
operator-app integration are not implemented in this change. Nothing here is a
production rollout or validator economic activation.

## Decision

Keep the node's dedicated account, signer and validator-scoped API key. Add an
explicit, private association to the operator's existing human account. Do not
move the validator to that account, merge accounts, attach its signer as a login
identity, issue a different key, or change either account's payout wallet.

This deliberately differs from the older proposal to enroll the node's signer
directly on the operator account. Current validators enforce one registration per
canonical account and bind work to that account's linked signer. A separate
association preserves that security contract and existing independent enrollment.
One human may associate several separately enrolled nodes; this does not prove
independence or grant additional quorum seats under an independence review.

The association grants visibility of the node's ID, signer, software version,
status and heartbeat in the human's authenticated account. It is **not** an
authentication, account recovery, validator control, payment, reward, or trust
credential. Losing the node key still follows the existing recovery rules.

## Flow

1. An already registered node explicitly starts pairing. Core authenticates its
   existing validator key and rechecks the account, registered signer and status.
2. Core creates one 256-bit opaque pairing ID and a ten-minute deadline. Repeated
   start calls reuse that pending attempt; no browser login happens automatically.
3. The local app opens the configured HTTPS Console approval URL. The operator
   authenticates with a recent Core-verified Google or wallet proof and explicitly
   approves the displayed node. Google/GitHub are not prerequisites for running
   a node; wallet login remains an account-pairing option.
4. Core fixes the operator account for that attempt. Another account cannot replace
   it. The Console displays a comparison code, and the node polls using its own
   existing key. No node key or signer secret ever passes through the Console.
5. The local app shows the approval and requires a second explicit confirmation
   after the operator compares the code. It must not automatically sign merely
   because a poll reports `approved`.
6. The node signs Core's exact canonical pairing payload. Core verifies the fresh
   attempt, current node identity and signature, then commits the association and
   `linked` transition in one SQL transaction. Failed commits leave it approved
   for a retry; repeat successful confirmations are idempotent within the deadline.
7. After a delayed response or restart, query both the current pairing and current
   association. An expired confirmation cannot create a link. A previously
   committed association remains visible through the association endpoint even
   after the short-lived pairing expires.

The signer uses EIP-191 over UTF-8 JSON with sorted keys, compact separators and
these exact fields: `purpose` (`aipg.validator.account-link.v1`), `audience`,
`pairing_id`, `validator_id`, `node_account_id`, `operator_account_id`,
`signing_wallet`, `comparison_code`, integer Unix `expires_at`, and
`permissions: ["validator.account_visibility"]`. The client verifies the expected
purpose, audience, node identity, permission list and expiry before offering to
sign. This is an off-chain signature; no wallet transaction or gas is required.

## API Contract

All endpoints are behind `VALIDATOR_PAIRING_ENABLED=0` by default. Node routes
require the existing `validator.attest` scope and the current registered signer.
Self-suspended nodes may pair; maintainer-revoked nodes cannot. Account approval
and removal require `account.manage`, a Core-issued user token, and a Google/SIWE
proof no older than ten minutes. Static session/service keys cannot approve.

| Method and path | Result / action |
| --- | --- |
| `POST /v1/validator/account-pairings` | Start/recover the node's current attempt; returns HTTPS approval URL, never credentials |
| `GET /v1/validator/account-pairing` | Authenticated node poll; approved state includes exact signing payload and comparison code |
| `POST /v1/validator/account-pairings/{id}/confirm` | Body `{signature}`; verify exact Core payload and atomically associate |
| `POST /v1/validator/account-pairings/{id}/cancel` | Cancel a not-yet-linked attempt; cannot cancel a committed association |
| `GET /v1/validator/account-link` | Node's current association and a short-lived unlink payload, or `status: none` |
| `POST /v1/validator/account-link/unlink` | Body `{pairing_id, issued_at, signature}`; registered signer removes the current association |
| `GET /v1/account/validator-pairings/{id}` | Freshly authenticated human inspects one attempt |
| `POST /v1/account/validator-pairings/{id}/approve` | Fix caller's canonical account on that attempt; no link exists yet |
| `GET /v1/account/validators` | `account.read` user-token view of up to 100 current associations, newest first |
| `POST /v1/account/validators/{validator_id}/unlink` | Fresh proof plus body `{pairing_id}` removes only that exact association |

Unknown/replaced IDs return 404; identity/authentication failure is 401/403;
expiry, cancellation, mismatched account, or conflicting state returns 409. Body
schemas reject extra fields. Successful responses are `no-store`. No route
publishes account identities, pairing metadata, signatures, or operator links in
public scorecards/health. The Console must use no-store responses and a
no-referrer page, must derive the user token from its authenticated server-side
session, and must never accept a browser-supplied account ID.

## Recovery and Revocation

- Cancel on the node before confirmation, then start again to choose another
  human account. Cancellation cannot race into a successful confirmation.
- Remove a completed association from the authenticated human account or from
  the node using its dedicated signer. Neither path stops the node, revokes its
  key, disconnects its wallet, or affects its evidence history.
- Node unlink signs `aipg.validator.account-unlink.v1` with the exact audience,
  both accounts, node ID, signer, pairing ID, issued time and ten-minute expiry.
  A signed removal for an old pairing ID cannot remove a later association.
- Signer rotation or account retirement makes the association stale and invisible;
  it never follows an account alias. A new explicit pairing is required. Account
  merges racing a link may leave stale operational metadata but grant no access
  to the successor account. Neither pairing nor listing initiates a merge.
- Copying a node configuration copies the same identity, not an independent node.
  Pairing cannot resolve shared-control claims or grant an independence review.

## Persistence and Concurrency

Alembic `0030` creates two empty Grid tables. `grid_validator_pairings` has one
replaceable slot per validator, with one current unique opaque attempt ID.
`grid_validator_account_links` retains the current signed association and its
revocation time. This is bounded operational metadata, not an append-only money
ledger or an immutable association history. A new link replaces a revoked/stale
link only after a new two-party proof.

Every state transition serializes on the registered validator row in PostgreSQL.
Confirm additionally conditionally changes approved -> linked and commits the
link in that same transaction. Database uniqueness prevents two current links.
Expiry uses PostgreSQL's actual clock after lock acquisition, not transaction
start time. No Redis lease is an authority for this transition.

The canonical registration, API keys, account identities, balances, ledger,
payout addresses, and reviewed control groups are unchanged. Existing request
authentication and validator work do not query the new tables. Keeping the flag
off leaves old validators and inference paths unaffected.

## Rollout and Verification

1. Review Core, Console and node changes together. Apply `0030` before enabling
   the routes. Deploy dark first; inspect the feature advertisement and empty
   tables. Do not enable pairing while clients are absent.
2. Verify clean Windows/Linux local-app setup, explicit two-sided consent,
   code mismatch rejection, private-key non-disclosure, cancellation, account
   confusion, stale sessions, node restart, and post-commit response loss.
3. Run the lifecycle and real PostgreSQL tests in
   `grid_api/services/tests/test_validator_pairing.py`; never point their
   disposable database setting at production. Run Alembic upgrade, drift check,
   downgrade to `0029`, and re-upgrade on scratch PostgreSQL and SQLite.
4. Run a supervised association/unlink canary with existing non-funded test
   identities. Confirm balances, keys, wallets, independence reviews and normal
   evidence submission are unchanged before public enablement.
5. Roll back by disabling `VALIDATOR_PAIRING_ENABLED`, then roll back code if
   needed. Retain the tables and node configurations. An explicit database
   downgrade discards only association state, but is not the operational rollback.

Remaining: Console page, local-app controls, both-platform live pairing proof,
and supervised deployment. This Core change alone does not close the onboarding
goal or the five-independent-operator qualification.

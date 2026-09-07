# Validator Payout Consent

Status: **Core API implemented dark; operator-app and Console screens not yet
implemented or released.** Migration `0039` and
`VALIDATOR_COMPENSATION_OPERATOR_ENABLED` are separate from the payment sender
flag. No new API approves a recipient, creates an earning campaign, or moves
funds. Keep the flag off until both clients and their end-to-end tests ship.

## Operator Journey

1. The existing node app shows approved pilot windows and finalized allocations.
   An open campaign is scheduled, earning, or awaiting finalization, not a
   computed balance. A finalized allocation shows reviewed work, exact AIPG
   amount and wallet/payment state.
2. The operator starts payout setup for a positive allocation. Reuse the existing
   optional Console account association for the browser handoff. If absent,
   use the account-link flow first; never enroll a replacement node or reset
   qualification. Node operation still does not require Google login. Human
   Console login supports Google or a wallet.
3. The linked human opens the fixed Console URL, selects a payout wallet and
   reviews the amount, Base network and destination. The wallet signs the exact
   recipient consent, not a transaction or token approval.
4. The node fetches that same request, displays destination and amount, and
   requires explicit local confirmation before its existing signer signs.
   Account association alone is never payout authority; no raw-message signing
   endpoint exists.
5. Both signatures produce `review_required`, not a payable recipient. The sole
   maintainer independently confirms the destination with the known operator,
   exports the proof privately and uses the existing digest-bound recipient
   approval command. The separately approved sender operates later.

The Core supports this sequence; its node-app and Console UI remain delivery
requirements. Do not send administrative commands to node operators.

## HTTP Contract

All routes are private, rate-limited, no-store and default-off. Node reads use
`validator.read`, writes use `validator.attest`. Human routes require a Core
user token: reads use `account.read`, mutations use `account.manage` and recent
Google/SIWE step-up. API keys, service identities and request IDs alone cannot
replace a human session. The association must remain current and canonical,
bound to this node and signer. Pairing's existing global or expiring allowlist
gate also applies to request operations.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/validator/compensation` | Own pilot windows and finalized rewards |
| POST | `/v1/validator/compensation/{allocation_hash}/requests` | Start or resume wallet setup |
| GET | `/v1/validator/compensation/requests/{request_id}` | Node reads its pending consent |
| POST | `/v1/validator/compensation/requests/{request_id}/confirm` | Node supplies consent signature |
| POST | `/v1/validator/compensation/requests/{request_id}/cancel` | Cancel before node confirmation |
| GET | `/v1/account/validator-compensation/requests/{request_id}` | Linked human reads request |
| POST | `/v1/account/validator-compensation/requests/{request_id}/prepare` | Select recipient and obtain signing text |
| POST | `/v1/account/validator-compensation/requests/{request_id}/approve` | Supply recipient signature |

Status uses `aipg.validator.compensation.operator.v1`. `items` are pages of 25
with `offset`/`next_offset`; the latest 25 applicable campaigns have a separate
`campaigns_has_more` indicator. Amounts/caps are integer base-unit strings,
never floating point. AIPG has 18 decimals on Base. A cap is not an earned amount.
Status excludes account/control-group IDs, signatures, raw transactions, private
reviews and other beneficiaries. Its bounded joined allocation query does not
load signatures/transaction blobs. Status makes no Base RPC calls.

Prepare accepts exactly `recipient`, a canonical lowercase Base address.
Approval/confirmation accept exactly `review_hash` (SHA-256 of consent) and a
bounded hex `signature`. Bodies are capped at 20,000 bytes before JSON parsing;
duplicate keys and encoded bodies are rejected. Validation/storage failures
return fixed errors, never input fields or SQL parameters.

Private request views include ID, allocation hash, status, expiry, fixed Console
URL and `payment_authorized: false`. Prepared requests also include consent,
exact signing text and review hash. This consent contains only the beneficiary's
precise binding fields from `VALIDATOR_COMPENSATION_PILOT.md`, not public
scorecard metadata. Neither signature is returned. Clients must render fields
as text and keep consent out of URLs, analytics, logs and public diagnostics.

## Client Verification

The node must fetch from its pinned official Grid and reconstruct
`consent_message` locally. Verify schema/audience, Base chain, AIPG
token/decimals, node ID/configured signer, amount, allocation, expiry and the
fresh hash against what was explicitly shown. Never sign caller-supplied text
or trust a returned `message` without reconstruction. The node key stays local.

Console reuses its authenticated backend and wallet connectors. Derive the
human account from the server-verified session, not a browser-provided ID.
Display exact recipient/amount/chain before `personal_sign`. EOA verification
is offline; deployed contract recipients require Base-pinned EIP-1271.
Individual wallet support still requires client testing.

## State And Recovery

One replaceable slot exists per allocation. A random `vpc_*` ID locates it but
is not an authentication capability. It freezes the current account link and
expires after 24 hours. Repeated start resumes the unexpired slot. The human
may change the draft destination before signing; that invalidates any older
review hash. The destination is frozen after wallet approval.

The node can cancel before its signature. A node-confirmed request requires
maintainer review and cannot be cancelled by this API. Expired/cancelled unbound
slots can be replaced; old IDs cannot act on replacements. Approved recipients
prevent replacement and remain immutable. Lost responses are recovered by
reading/retrying the same request, never creating a new identity or payment.

Node-row locks serialize mutations with link removal and signer changes.
Contract-wallet RPC runs before those locks; afterward Core rechecks link,
consent, identity and expiry. No global payout lock is held during public wallet
RPC. Signature collection never writes credits, worker den, approved recipients
or payments.

The private maintainer export reads `request_id` and explicit `approval_ref`
from an owned file, revalidates both signatures, and creates a new `0600` output:

```sh
.venv/bin/python scripts/manage_validator_recipient.py export \
  --input /private/review-request.json --output /private/signed-consent.json
.venv/bin/python scripts/manage_validator_recipient.py bind \
  --input /private/signed-consent.json --output /private/recipient-preview.json
```

Paths are placeholders. Export is read-only. Later `bind --apply` requires the
exact reviewed digest. No public flow supplies an automatic approval reference,
binds a recipient or sends funds.

## Rollout Gates

Apply `0039` before enabling the operator API. Ship and test both clients,
including refresh, cancellation, duplicates, changed links and unavailable
wallets. Keep payment authorization and cohort qualification separate. Rollback
disables the operator flag and retains pending proof; downgrade refuses nonempty
state. Approved recipients and signed payments remain immutable `0037`/`0038`
records. The UI and live rollout are not proven by backend tests.

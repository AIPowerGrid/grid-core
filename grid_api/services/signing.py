# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Worker output signatures — the opt-in "signed" trust tier (Part B).

A worker MAY sign its output commitment with the private key of the wallet it
is paid to. The grid recovers the signer and checks it matches the worker's
resolved payout wallet: a valid signature means the entity that produced this
output *controls the address that gets paid* — and, once bonding lands (task
#59), the stake that gets slashed. Attribution + payment + slashable stake all
collapse onto one address, no separate PKI.

Signing is OPTIONAL. A worker that doesn't sign is a "floor" worker: it still
runs and still gets paid; its jobs just carry no `worker_sig` and score lower
for trust-sensitive routing. Verification is FAIL-CLOSED — a malformed sig, an
unknown signer, or any error stores NULL (unsigned), never a bogus "signed"
row. We only ever persist a signature we have positively verified.

The signature also proves *output agreement*: both sides derive the signed
message from `result_hash` (sha256 of the identical output the grid witnessed),
so a signature that verifies also attests the worker hashed the same bytes the
grid relayed. A worker that lies about its output can't produce a matching sig.
"""

import logging

from eth_account import Account
from eth_account.messages import encode_defunct

logger = logging.getLogger("grid_api.signing")

# Domain prefix so a worker signature can NEVER be replayed as some other signed
# message (a login challenge, an ERC-191 personal_sign for a transfer, etc.).
_DOMAIN = "aipg-job"


def signed_message(job_id: str, result_hash: str) -> str:
    """The canonical string a worker signs: binds the output commitment to the
    job, under an AIPG-specific domain. Deterministic from data the grid already
    holds, so the grid reconstructs it independently to verify."""
    return f"{_DOMAIN}:{job_id}:{result_hash}"


def verify_worker_sig(job_id, result_hash, worker_sig, allowed_addresses) -> str | None:
    """Return `worker_sig` IFF it is a valid signature over
    `signed_message(job_id, result_hash)` recovering to one of
    `allowed_addresses` (the worker's payout/login wallet); else None.

    Fail-closed: no sig, no result_hash, no allowed address, a bad signature, or
    a signer outside the allowed set all return None (the job records unsigned).
    """
    if not worker_sig or not result_hash or not job_id:
        return None
    allowed = {a.lower() for a in allowed_addresses if a}
    if not allowed:
        return None
    try:
        message = encode_defunct(text=signed_message(str(job_id), result_hash))
        recovered = Account.recover_message(message, signature=worker_sig)
        if recovered.lower() in allowed:
            return worker_sig
        logger.info(
            "worker_sig recovered %s not in allowed set for job %s (stored unsigned)",
            recovered, job_id,
        )
    except Exception as e:
        logger.debug("worker_sig verify failed job=%s: %s", job_id, e)
    return None

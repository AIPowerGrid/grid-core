# Validator Signed-Claim Attack Study

## Scope

September 16 local qualification exercises the actual Core HTTP registration,
scoped-key authentication, assignment, targeted probe and attestation routes,
worker WebSocket handling, Redis transport and disposable PostgreSQL storage.
The test is
`grid_api/routers/tests/test_validator_hostile_path.py` and uses the existing
isolated process-crash fixture. It does not connect to production.

Worker output is scripted with the existing public-template solver. Three
ephemeral signing identities register through HTTP in each scenario. No test
inserts completed assignments or votes. The real probe builds its evidence
commitment and scores the worker response before signed reports are submitted.

The matrix has four task families (instruction, tool call, stop sequence and
token limit), correct/broken worker output, and truthful/false validator votes:
16 scenarios and 48 probe executions. They are correlated synthetic cases,
not 48 independent GPU/model samples or a measured population detection rate.

## Results And Meaning

Initial complete matrix passed on Python 3.13 and local PostgreSQL 14:

| Attack or control | Observed boundary |
| --- | --- |
| Valid signature over fabricated evidence hash or invented nonce | HTTP 400; cannot insert that claim |
| Signed claim before the assigned probe executes | HTTP 400; incomplete probe rejected |
| Missing API credential / inference-only credential | HTTP 401 / 403 |
| Another registered signer claims the completed assignment | HTTP 400; account ownership fails |
| Exact signed retry | Duplicate, not another vote |
| Same validator signs a contradictory second vote | HTTP 400; one vote per group retained |
| Three false votes agreeing with one another | Stored as signed authoritative claims; raw quorum can say accepted |
| Those false votes enter compensation work verification | All excluded as incorrect task score |
| Those false votes enter observation evidence selection | Zero supporting rows after finalization |
| Truthful votes on correct or broken worker responses | Remain countable work and eligible observation support |

Malformed tool arguments, ignored stop markers and premature truncation produce
failed Core task results. Correct scripted controls pass. Customer credit,
reservation and worker-ledger snapshots remain unchanged throughout.

The finalization clock is accelerated only in the isolated parent test process.
Operator review/qualification fields are synthetic fixtures so truthful controls
can prove the observation selector is not merely rejecting everybody. This does
not prove real operator independence, 72 hours of availability, or elapsed
campaign qualification. Compensation is tested through its actual per-report
verifier, not a finalized campaign allocation or a transfer. Required PostgreSQL
16 CI remains the release qualification gate; local PostgreSQL 14 is supplementary.

## Important Limitation

**Accepted storage and raw quorum are not proof that a verdict is true.**
The API deliberately preserves signed disagreements. Scorecards separately
expose Core-matched and Core-disagreed votes, while current generated probes
remain ineligible for a quality score. Consumers must not promote raw
`authority`, `quorum_status`, `healthy_rate` or signature validity into economic
or model-identity trust without the downstream verification contract.

These tests prove defenses against specific false claims under an honest Core;
they do not secure a malicious coordinator or establish independence through
signatures alone. The no-model template solver passes the correct controls:
this remains an explicit anti-cheating gap, not model-verification success.
Copied logprobs, probe-aware model switching, real model substitution, media
fidelity, and end-to-end compensation allocation require their separate studies.

No production policy, runtime, historical score or compensation contract changes
are part of this study. The client GPT backend remains off-limits.

<!--
SPDX-FileCopyrightText: 2026 AI Power Grid
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Release ac2167eb: single chat choice and faster routed dispatch (2026-11-16)

- Approved scope: the owner's "deploy all" on 2026-11-16 for PRs #257 and
  #258.
- Release: `ac2167eb95f632a74d77e2710e27a0945594a998`, at
  `/home/aipg/releases/grid-core-ac2167eb`. Previous: `grid-core-b1370d34`.
  Cutover about 17:45 UTC. Code only; no migration, dependency, unit or
  environment change.
- What changed:
  - `POST /v1/chat/completions` refuses `n` other than 1 at validation. Core
    used to forward `n` up to 4; the worker stream carries one choice, so two
    completions came back interleaved and both were billed.
  - A connected worker blocks on the job stream for `POLL_BLOCK_MS` (250 ms,
    `GRID_JOB_POLL_BLOCK_MS`) instead of 5 s. A job handed to a compatible
    peer's routing inbox waited for that peer's next poll cycle, which put a
    random 0-5 s before the first token of most routed chat jobs.
- Checks: PR checks passed; the staged release imported with the production
  environment and reported `POLL_BLOCK_MS` 250; `/health` reported the release
  SHA; 8 workers were connected before the restart and 8 after; public
  `/health` 200, unauthenticated validator assignments 401, retired heartbeat
  410; an unauthenticated chat request with `n: 2` returned 422 with the new
  message and one without `n` reached the API-key check.
- Not checked: the first-token delay was not measured after the release. No
  paid request was sent.
- Rollback: repoint `/home/aipg/current` to `grid-core-b1370d34` and restart.
  The schema is unchanged. To keep the release and only restore the old poll
  interval, set `GRID_JOB_POLL_BLOCK_MS=5000` and restart.

## Follow-up release 30d91617 (same day)

- Approved scope: the owner's "deploy" on 2026-11-16 for PR #261.
- Release: `30d916178a648284a9be1ac7cdff709edd1b4009`, at
  `/home/aipg/releases/grid-core-30d91617`. Previous: `grid-core-ac2167eb`.
  Cutover 18:43 UTC. Code only; no migration, dependency, unit or environment
  change. PR #260 (copyright headers and a README paragraph) rode along.
- What changed:
  - A worker connection reads the shared job stream only while it has no job
    in hand. A busy worker used to park the next job until its own finished;
    the logs showed a stale reclaim about five and a half minutes after each
    of two reported client timeouts, with no worker disconnect.
  - A settled chat reply carries `grid.billed_completion_tokens`.
  - `grid_workers.last_seen` is refreshed about once a minute while a worker
    is connected.
- Checks: PR checks passed; the staged release imported with the production
  environment; `/health` reported the release SHA; 8 workers were connected
  before the restart and 8 one minute after; public `/health` 200,
  `/v1/models` 200, unauthenticated validator assignments 401, retired
  heartbeat 410. In the first two minutes one text worker completed five jobs
  in a row, so the read gate reopens after a finished job. Every connected
  worker's `last_seen` had moved about a minute after it connected. No
  reclaim, traceback or critical log line in that window.
- Not checked: the billed-token field on a live reply (no paid request was
  sent), a long job on one worker with a second request waiting, and the
  other seven workers taking a second job (they had no traffic yet).
- Rollback: repoint `/home/aipg/current` to `grid-core-ac2167eb` and restart.
  The schema is unchanged.

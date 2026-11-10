# Release c96b1d17: log-only prompt screening enabled

## Posture

Production selected immutable Core `c96b1d173a838d861c0982dd7b1dce1ec64a5453`
at `2026-11-10T19:57:01Z`, replacing `a6cd1166`. Alembic stayed at `0046`; no
migration was pending. The only code change is #248. The owner approved
enabling screening before the content policy is published.

## What is on

`PROMPT_SCREEN_MODE=shadow`. Admitted prompts on OpenAI chat, Anthropic and
Responses passthrough, waiting media and async video are scored in the
background by OpenAI's moderation endpoint (`omni-moderation-latest`) and only
the `sexual/minors` score is read. Nothing is delayed or refused. Threshold is
the default `0.5`; a score at or over it logs `over=1` and emits a
deduplicated operator alert.

Prompt text is sent to OpenAI after credential redaction. It is not logged or
stored by Core. The published content policy does not yet disclose this; the
draft in `aipg-documentation` now carries the line.

## Procedure evidence

- Release built beside the live tree from a detached clean checkout;
  hash-locked install; `pip check` clean.
- Backup `grid-postgres-20261110T195456Z.dump` (315 MB) verified; restore
  proof passed against the candidate: `No new upgrade operations detected`,
  Alembic `0046`.
- All six systemd units, the nginx site and `alert_ops.py` were byte-identical
  to the candidate's, so none were reinstalled.
- `/etc/aipg/grid.env` gained `PROMPT_SCREEN_MODE` and `PROMPT_SCREEN_API_KEY`;
  the previous file is `grid.env.before-prompt-screen-20261110T195649Z`. Mode
  and ownership stayed `root:root 600`. The key is an OpenAI project key
  restricted to `/v1/moderations` (it is refused on chat and model listing).
- Before the restart, the candidate loaded the new settings in a one-off
  process as the service user, started the screen and scored one benign prompt
  against the live endpoint.
- `aipg-gridapi` restarted once. `/health` answered within 4 seconds with the
  new build commit; all four API processes logged
  `Prompt screening started in shadow mode`. The three workers connected
  before the restart reconnected. Payout and backup timers were active before
  and after.

## Verified live

Two benign requests through the public hostname produced:

```
prompt_screen path=openai-chat model=grid/qwen3:8b account=<opaque> score=0.000005 over=0 chars=42
prompt_screen path=anthropic model=grid/qwen3:8b account=<opaque> score=0.000001 over=0 chars=16
```

## Not done

- No over-threshold prompt was sent through production, so the hit alert has
  not been delivered end to end. It is covered by unit tests only.
- Media and async-video observation points were not exercised live.
- Refusal is not implemented. Generated outputs and negative prompts are not
  screened.
- The threshold has not been calibrated on real traffic.

## Reading the data

`journalctl -u aipg-gridapi | grep 'prompt_screen path='` gives one line per
scored prompt. `prompt_screen unavailable` lines are classifier failures.

## Turning it off

Set `PROMPT_SCREEN_MODE=off` in `/etc/aipg/grid.env` and restart
`aipg-gridapi`, or revoke the `aipg-grid-moderation` key in the OpenAI
project: screening then logs failures and requests are unaffected.

## Rollback

Select `/home/aipg/releases/grid-core-a6cd1166` through the symlink and
restart `aipg-gridapi`. The older release ignores the two new variables.

# Releases 27080900 and 0f0bad60: step-up fix, OAuth refresh tokens enabled

## Posture

Two releases on 2026-11-11, both approved by the owner:

| Time (UTC) | Release | Contents | Alembic |
| --- | --- | --- | --- |
| 01:17:41 | `270809005982e593ddf8bed3b1284bfc90c39581` | #251 application tokens never satisfy account step-up | `0046` |
| 01:40:46 | `0f0bad60364c652712cc6948cb9f023917ccc81e` | #250 rotating OAuth refresh tokens | `0047` |

Previous release: `c96b1d17`.

## Step-up fix (#251)

Found by an independent review of #250. A token issued to an OAuth
application carried the user's proof method and a fresh proof time, so it
passed `require_recent_step_up`; the asserted wallet-link route had no scope
requirement. The fix rejects client-bound tokens centrally and on that route.
It was live from the OAuth server's first enablement until 01:17:41Z.

Evidence of use: none found. Two OAuth clients were registered in total and
both had been used; the last account merge was 2026-10-27; retained logs show
no requests to the route. Authorization rows are pruned after a day, so this
evidence is limited. The fix was verified in the candidate's source and by
tests, not by a live application-consent flow.

## Refresh tokens (#250)

- Migration `0047` (`grid_oauth_refresh_tokens`, additive) was proven on a
  restored copy of backup `grid-postgres-20261111T013835Z.dump`
  (`No new upgrade operations detected`, Alembic `0047`), then applied to
  production from the candidate release while `27080900` kept serving.
- `/etc/aipg/grid.env` gained `GRID_OAUTH_REFRESH_TOKENS_ENABLED=1`; the
  previous file is `grid.env.before-oauth-refresh-20261111T014043Z`.
- The candidate loaded the settings in a one-off process and reported both
  grants and the `offline_access` scope before the restart.
- Console `1ad6ed29` (Vercel `dpl_3uLDcoCKjdnST5FPRb2x1jd6P1L7`) was promoted
  at 01:37Z first, so the consent screen can show `offline_access`.

## Procedure evidence (both releases)

Detached clean checkout, hash-locked install, `pip check` clean, verified
backup, restore proof against the candidate, unit files and nginx site
byte-identical so not reinstalled, one `aipg-gridapi` restart each, `/health`
within 4 seconds with the new build commit, no error lines after either
restart, payout and backup timers active before and after.

## Verified live after 0f0bad60

- Discovery advertises `authorization_code` and `refresh_token`,
  `offline_access`, and the revocation endpoint.
- A public client registered with both grants; `/v1/oauth/authorize` with
  `offline_access` redirected to the Console consent page.
- `grant_type=refresh_token` with an unknown token returned `invalid_grant`;
  `/v1/oauth/revoke` returned 200; `/v1/oauth/sessions` without a Console
  login returned 401.

## Not done

- No consent has been approved, so no refresh token has been issued, rotated
  or revoked in production. The full flow needs a signed-in owner.
- The Console has no page for listing and ending sessions; only the API exists.
- One test client named "AIPG go-live check (safe to delete)" was registered.
  It was never authorized, so the OAuth sweeper removes it after a day.
- Three workers were connected after the second restart. The Titan workers
  had disconnected on their own at 01:26Z, before it.

## Turning refresh tokens off

Set `GRID_OAUTH_REFRESH_TOKENS_ENABLED=0` and restart `aipg-gridapi`. New
refresh grants are refused; the revoke and session routes keep working.

## Rollback

To `27080900`: select it through the symlink, restore
`grid.env.before-oauth-refresh-20261111T014043Z` and restart. Leave migration
`0047` applied; the older release ignores the table. Do not roll back past
`27080900`, which would reopen the step-up gap.

# Remote MCP Authorization

## Status

The OAuth foundation is implemented behind `GRID_MCP_OAUTH_ENABLED=0`. The
remote Streamable HTTP resource is implemented in unpublished `grid-skill`
source `0.2.0`. Its loopback process, least-privilege introspection principal,
and exact production reverse-proxy route were deployed dark on 2026-08-30 and
passed the unauthenticated dark preflight. Core OAuth remains disabled, so this
is not a usable public product: discovery and client registration still return
404 and no remote MCP user token can be issued. Do not describe the endpoint as
live until a supervised production authorization and paid tool call pass.

## Objective

Let an MCP-capable agent connect to AI Power Grid without asking a human to
paste a long-lived Grid API key into the agent. The human signs in through the
existing Grid Console account, reviews the requested scopes, and grants a
short-lived token to one registered public client.

Core is the OAuth authorization server and the API origin is the protected
resource. Treating `https://api.aipowergrid.io` as one resource lets one token
authorize both `/v1/mcp` and the existing generation endpoints without passing
a token across resource boundaries.

## Flow

```text
MCP client                 Core OAuth                 Grid Console             Core API / MCP
    |                           |                           |                         |
    |-- dynamic registration ->|                           |                         |
    |<-- public client_id ------|                           |                         |
    |                           |                           |                         |
    |-- authorize + S256 PKCE ->|                           |                         |
    |<-- redirect to consent ---|-------------------------->|                         |
    |                           |    service key + delegated Console user token       |
    |                           |<-- inspect opaque request --------------------------|
    |                           |<-- approve/deny ------------------------------------|
    |<-- one-use code ----------|                           |                         |
    |-- code + verifier ------->|                           |                         |
    |<-- 15-minute gridu_ token-|                           |                         |
    |------------------------------------------------ bearer token ----------------->|
```

## Security Contract

- Public clients only: no client secret is issued or accepted.
- S256 PKCE is mandatory. Plain PKCE is not supported.
- Web redirects require HTTPS. Native clients may use HTTP only on
  `127.0.0.1`, `::1`, or `localhost`; an ephemeral loopback port is allowed.
- Redirect URIs are validated before any client-controlled redirect occurs.
- `state` is mandatory and returned unchanged. Successful and denied callbacks
  also carry the authorization-server issuer.
- Requested scopes are an exact subset of `account.read` and
  `inference.submit`.
- Request capabilities and authorization codes are random, short-lived, and
  stored only as SHA-256 hashes.
- A code is bound to the client, redirect, resource, account, scopes, and PKCE
  challenge. PostgreSQL row locking makes redemption single-use.
- Access tokens expire after 15 minutes, carry the exact resource audience and
  OAuth client id, and have no refresh token.
- Consent inspection and decisions require both the `grid-console` service key
  and a delegated Console user token backed by a Google or SIWE proof no more
  than ten minutes old. Neither credential is exposed to browser JavaScript by
  Core.
- The MCP resource introspects tokens with a separate `grid-mcp` service key
  carrying only `oauth.introspect`. It must not have `identity.exchange`,
  `identity.assert`, `inference.submit`, or `inference.service_submit`.
- Introspection is loopback-only at the deployment edge: public Nginx returns
  an exact `404` for `/v1/oauth/introspect`. The MCP process coalesces identical
  checks, keeps at most five seconds of positive cache, bounds cache and
  in-flight entries, and never caches a failed check.
- Registration JSON and token forms are size-limited before parsing and are
  rate-limited. Token and registration responses use `Cache-Control: no-store`.
- A background retention loop deletes authorization rows after one day and
  removes old clients only when they never completed a token exchange and no
  retained authorization still references them.

## Non-Goals

- No refresh tokens, offline access, confidential clients, client credentials,
  wallet signatures inside the OAuth protocol, or economic authority.
- OAuth does not change account ownership, link identities, create credits, or
  bypass normal request metering.
- The MCP backend is not trusted to invent user identity. It may only
  introspect a Core-issued token and forward that same resource token.

## Provisioning

After migration `0031`, provision the MCP backend principal locally on Core:

```bash
python scripts/create_service_account.py \
  --id grid-mcp \
  --name "Grid remote MCP" \
  --scope oauth.introspect
```

Store the one-time key only in the MCP server secret store. Do not put it in
the Console, docs, images, CI output, or a browser environment variable.

## Rollout Gates

1. Apply and verify Alembic `0031` with OAuth still disabled.
2. Deploy Core and confirm discovery, registration, and authorization routes
   return 404 while disabled.
3. Deploy the Console consent page. Its server routes must hold the Console
   service key and delegated user token; the browser receives only the bounded
   request summary and final redirect.
4. Deploy `/v1/mcp` with the introspection-only `grid-mcp` key. A 401 response
   must advertise protected-resource metadata. Keep the Node process on
   loopback; proxy only the MCP route from the API origin. Verify its Host and
   Origin guards and 256 KiB request limit. Core's introspection rate limit must
   be load-tested for per-request MCP authorization before public traffic.
   Prove both same-token coalescing/cache behavior and bounded distinct-token
   pressure. Confirm the public introspection URL stays `404`.
5. Enable OAuth only in an isolated production canary deployment.
6. Prove registration, consent, generation, denial, expiry, wrong verifier,
   wrong redirect, duplicate-code redemption, charging, and revocation behavior.
7. Monitor registration/auth rate limits and database growth before making the
   endpoint public.

As of 2026-08-30, gates 1-3 and the dark infrastructure portion of gate 4 have
passed in production. Gate 4's authenticated load and long-running SSE checks,
plus gates 5-7, remain open because no valid remote token exists while OAuth is
disabled.

Rollback is one flag: set `GRID_MCP_OAUTH_ENABLED=0`. This invalidates OAuth
resource tokens at Core without affecting ordinary API keys or existing
frontend service delegation. A remote MCP process may retain a successful
introspection for at most five seconds before observing the rollback.

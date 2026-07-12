# Service Accounts and Native User Sessions

## Roles

- **User API key:** long-lived user-held inference credential. It cannot manage
  an account unless it is a legacy session key and the rollback flag is enabled.
- **Service key:** long-lived server-only credential for one application. It can
  run service-owned jobs and exchange only its own app-local subjects.
- **Core user token:** 15-minute token issued after app exchange or direct
  Google/SIWE proof. It carries the canonical account and service audience.
- **Step-up token:** a Core user token whose recent proof method is Google or
  SIWE. Only this may change payout settings or API keys.

Service keys are not removed: public tools, scheduled jobs, bots, health probes,
and application-owned generation can charge the service account directly. User
generation uses the same service key plus `X-Grid-User-Token`, or the user token
directly as Bearer auth.

## Provisioning

Run after Alembic `0015` from a trusted Core host. The command prints the key
once; put it in the application's server-side secret store.

```bash
./scripts/create_service_account.py \
  --id grid-console --name "Grid Console" \
  --provider app --provider google \
  --google-audience "$GOOGLE_ID" \
  --per-request-micro 1000000 --daily-micro 100000000

./scripts/create_service_account.py \
  --id aipg-art --name "AIPG Art" \
  --provider app --provider google \
  --google-audience "$GOOGLE_CLIENT_ID" \
  --per-request-micro 1000000 --daily-micro 250000000

./scripts/create_service_account.py \
  --id aipg-chat --name "AIPG Chat" \
  --provider app \
  --per-request-micro 500000 --daily-micro 100000000
```

The example ceilings are conservative deployment defaults, not economics. Tune
them from observed traffic. Redis enforces the daily exposure ceiling
fail-closed and idempotently by job reference.

Rotate one service without affecting the others:

```bash
./scripts/rotate_service_key.py --id aipg-art
```

## Exchanges

- `POST /v1/auth/service/exchange`: service-local subject to inference token.
- `POST /v1/auth/google/exchange`: Core verifies Google signature, issuer,
  lifetime, and the service's configured OAuth audience before issuing a token.
- `POST /v1/auth/service/bind`: after recent Google/SIWE proof, bind one local
  service subject to the canonical account.
- `POST /v1/accounts/wallet/verify`: Core verifies its one-use wallet nonce and
  exact signed message, then issues a direct step-up token.

Legacy `/v1/accounts/session` and `/v1/accounts` return `410` unless
`GRID_LEGACY_INTERNAL_SESSION_ENABLED=1`. Legacy session API keys are rejected
unless `GRID_LEGACY_SESSION_KEYS_ENABLED=1`.

## Deployment Order

1. Back up PostgreSQL and apply Alembic through `0015`.
2. Set `GRID_USER_TOKEN_SIGNING_KEY` to a new 32-byte-or-longer secret.
3. Provision and fund/credit each service account; configure ceilings.
4. Deploy Core with charging and free/promo spending flags unchanged.
5. Deploy Console, Art, and Chat with their distinct service keys.
6. Canary service-owned work and delegated-user work for every modality.
7. Confirm no production caller uses `GRID_INTERNAL_TOKEN`, then remove it.
8. Enable charging/free/promo independently only after shadow parity.

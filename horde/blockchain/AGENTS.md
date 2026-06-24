# horde/blockchain - legacy model registry integration

## Purpose

Legacy Flask-side helpers for reading on-chain model registry state and applying
model validation to Horde compatibility flows.

## Ownership

- `config.py` - blockchain env/config for legacy integration.
- `model_registry.py` - Web3 ModelRegistry helper.
- `__init__.py` - package setup.

## Local Contracts

- This is legacy/Horde integration. New Grid-side model and recipe sync belongs
  in `grid_api/services/model_registry.py` and `grid_api/services/recipes.py`.
- Web3 reads must not run on hot paths when a cached registry can be used.
- Contract addresses and env var names must match docs and deploy templates.

## Work Guidance

- Keep Base Sepolia/mainnet distinctions explicit.
- If changing ABI assumptions, update `docs/BLOCKCHAIN_INTEGRATION.md` and the
  integration package SDKs where relevant.

## Verification

- No standalone tests currently own this module.
- Use read-only RPC checks with explicit user approval/network access when needed.

## Child DOX Index

- None - leaf.

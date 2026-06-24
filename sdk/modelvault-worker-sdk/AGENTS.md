# sdk/modelvault-worker-sdk - TypeScript worker SDK

## Purpose

TypeScript package for workers or tooling that need to interact with the
ModelVault/ModelRegistry contract layer.

## Ownership

- `src/client.ts` - client behavior.
- `src/types.ts` - public TypeScript types.
- `src/abi.ts` - embedded ABI data.
- `src/index.ts` - package exports.
- `package.json`, `tsconfig.json` - package build config.
- `README.md` - package usage.

## Local Contracts

- Keep ABI, types, README examples, and generated archives aligned.
- Do not commit private keys, RPC tokens, or operator secrets.
- If the canonical contract ABI moves to `aipg-smart-contracts`, update this
  package from that source rather than hand-editing divergent signatures.

## Work Guidance

- Prefer typed return values and explicit chain IDs.
- Document Base mainnet and Base Sepolia separately.
- Rebuild packaged archives only when the user explicitly wants distributable
  artifacts updated.

## Verification

- Run the package build/test command if present in `package.json`.

## Child DOX Index

- None - leaf.

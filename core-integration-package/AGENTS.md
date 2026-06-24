# core-integration-package - Base contract integration artifacts

## Purpose

Standalone contract/SDK package for integrating AIPG Core with Base-chain model
validation, RecipeVault workflow storage, and JobAnchor receipt anchoring.

## Ownership

- `contracts/` - sample Solidity contracts: `ModelRegistry.sol`,
  `RecipeVault.sol`, `JobAnchor.sol`.
- `abis/` - JSON ABIs consumed by SDK/examples and sometimes mirrored into core.
- `sdk/` - JavaScript SDKs for ModelRegistry, RecipeVault, and JobAnchor.
- `examples/` - worker integration and job anchoring examples.
- `README.md` - package usage guide.

## Local Contracts

- This package is an integration artifact, not necessarily the canonical live
  contract source if `aipg-smart-contracts` has newer Diamond modules. Verify
  ownership before changing ABI semantics.
- Keep ABI, SDK method signatures, examples, and README snippets aligned.
- Base mainnet/testnet addresses must not be invented. Use placeholders unless
  verified from deployment docs or chain.
- Job anchoring should be batched/epoch-oriented by default; per-job anchoring is
  expensive and should be opt-in.

## Work Guidance

- Do not add private keys or RPC secrets to examples.
- Prefer deterministic hashing helpers for model, recipe, input, and output IDs.
- If changing contracts here, note whether downstream `grid_api/_abi.py` or
  `grid_api/abis/` must be updated.

## Verification

- No package-level test script is currently defined.
- For JS SDK changes, run the package's Node tests/build if added.

## Child DOX Index

- None - leaf.

<!--
SPDX-FileCopyrightText: 2026 AI Power Grid

SPDX-License-Identifier: MIT
-->

# ModelVault Worker SDK

TypeScript SDK for querying the on-chain ModelRegistry. No wallet required for reads.

## Install

```bash
npm install @aipowergrid/modelvault-worker-sdk viem
```

## Quick Start

```typescript
import { createModelVaultClient, ModelVaultClient } from '@aipowergrid/modelvault-worker-sdk';

// Default: Base Sepolia testnet
const client = createModelVaultClient();

// Or custom config
const client = new ModelVaultClient({
  rpcUrl: 'https://mainnet.base.org',
  contractAddress: '0x...', // mainnet address
  chainId: 8453,
});
```

## API

### Check Model Registration

```typescript
const isRegistered = await client.isModelRegistered('flux1-dev.safetensors');
// true/false
```

### Validate Job Parameters

```typescript
const result = await client.validateParams('flux1-dev.safetensors', {
  steps: 30,
  cfg: 4.0,
  sampler: 'euler',
  scheduler: 'normal',
});

if (!result.isValid) {
  console.error(result.reason);
  // "steps 100 exceeds max 50"
  // "sampler 'invalid' not allowed"
  // "Model 'x.safetensors' not registered on-chain"
}
```

### Get Constraints

```typescript
const constraints = await client.getConstraints('flux1-dev');
// {
//   stepsMin: 1,
//   stepsMax: 50,
//   cfgMin: 1.0,
//   cfgMax: 20.0,
//   clipSkip: 0,
//   allowedSamplers: ['euler', 'euler_a', 'dpmpp_2m'],
//   allowedSchedulers: ['normal', 'karras', 'sgm_uniform'],
// }
```

### Get Model Info

```typescript
const info = await client.getModel('flux1-dev.safetensors');
// { displayName, description, baseModel, architecture, isNSFW, ... }
```

### Hash a Model Filename

```typescript
const hash = ModelVaultClient.hashModel('flux1-dev.safetensors');
// 0x...
```

## Worker Integration Example

```typescript
import { createModelVaultClient } from '@aipowergrid/modelvault-worker-sdk';

const vault = createModelVaultClient();

async function handleJobPop(job: Job) {
  // Validate before processing
  const validation = await vault.validateParams(
    `${job.model}.safetensors`,
    {
      steps: job.params.steps,
      cfg: job.params.cfg_scale,
      sampler: job.params.sampler_name,
      scheduler: job.params.scheduler,
    }
  );

  if (!validation.isValid) {
    return { error: validation.reason };
  }

  // Process job...
}
```

## Networks

| Network | Chain ID | Contract |
|---------|----------|----------|
| Base Sepolia | 84532 | `0xe660455D4A83bbbbcfDCF4219ad82447a831c8A1` |
| Base Mainnet | 8453 | TBD |

## No Wallet Required

All read operations are free and require no private key or gas. The SDK uses public RPC endpoints.





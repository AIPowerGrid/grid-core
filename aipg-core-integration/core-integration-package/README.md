# AIPG Core Blockchain Integration Package

Contracts and SDKs for integrating AIPG Core with on-chain model validation, workflow storage, and job tracking.

## 📦 What's Included

| Component | Purpose |
|-----------|---------|
| **ModelRegistry** | Register/validate AI models, enforce generation parameters |
| **RecipeVault** | Store ComfyUI workflows on-chain |
| **JobAnchor** | Anchor job receipts for verification & rewards tracking |

## 🚀 Quick Start

### Installation

```bash
npm install ethers pako
```

### Connect to Contracts

```javascript
const { ethers } = require('ethers');
const ModelRegistrySDK = require('./sdk/modelregistry-sdk');
const { RecipeVaultSDK } = require('./sdk/recipevault-sdk');
const JobAnchorSDK = require('./sdk/jobanchor-sdk');

// Base Mainnet
const provider = new ethers.JsonRpcProvider('https://mainnet.base.org');

// Contract addresses (deploy these first, or use testnet addresses)
const MODEL_REGISTRY = '0x...'; // Deploy ModelRegistry.sol
const RECIPE_VAULT = '0x...';   // Deploy RecipeVault.sol
const JOB_ANCHOR = '0x...';     // Deploy JobAnchor.sol

// Initialize SDKs
const modelRegistry = new ModelRegistrySDK(MODEL_REGISTRY, provider);
const recipeVault = new RecipeVaultSDK(RECIPE_VAULT, provider);
const jobAnchor = new JobAnchorSDK(JOB_ANCHOR, provider);
```

---

## 📋 ModelRegistry Integration

### Worker: Check if model is registered

```javascript
// Check if worker's model exists
const modelHash = '0x...'; // SHA256 of model file
const exists = await modelRegistry.contract.isModelExists(modelHash);

// Batch check multiple models
const workerModels = ['0x...', '0x...', '0x...'];
const results = await modelRegistry.contract.batchCheckExists(workerModels);
```

### Worker: Get model constraints for validation

```javascript
// Get constraints for a model
const constraints = await modelRegistry.getModelConstraints('flux-dev-fp8.safetensors');

if (constraints) {
  console.log('Steps:', constraints.steps.min, '-', constraints.steps.max);
  console.log('CFG:', constraints.cfg.min, '-', constraints.cfg.max);
  console.log('Clip Skip:', constraints.clipSkip);
}
```

### Worker: Validate job parameters

```javascript
const params = {
  steps: 28,
  cfg: 3.5,
  sampler: 'euler',
  scheduler: 'simple'
};

const validation = await modelRegistry.validateParameters('flux-dev-fp8.safetensors', params);
if (!validation.isValid) {
  throw new Error(`Invalid params: ${validation.reason}`);
}
```

### Admin: Register a new model

```javascript
const signer = new ethers.Wallet(PRIVATE_KEY, provider);
const adminRegistry = new ModelRegistrySDK(MODEL_REGISTRY, provider, signer);

const result = await adminRegistry.registerModel({
  modelHash: ethers.keccak256(modelFileBuffer),
  modelType: 1, // IMAGE_MODEL
  fileName: 'flux-dev-fp8.safetensors',
  displayName: 'FLUX.1 Dev FP8',
  description: 'FLUX.1 development model in FP8 format',
  isNSFW: false,
  sizeBytes: 12884901888,
  inpainting: false,
  img2img: true,
  controlnet: true,
  lora: true,
  baseModel: 'flux.1',
  architecture: 'DiT'
});

console.log('Model registered with ID:', result.modelId);
```

---

## 📋 RecipeVault Integration

### Get available workflows

```javascript
// Get all public recipes
const recipes = await recipeVault.getPublicRecipes();

for (const recipe of recipes) {
  console.log(`${recipe.name} by ${recipe.creator}`);
  console.log('Workflow nodes:', Object.keys(recipe.workflow).length);
}

// Get NFT-enabled recipes
const nftRecipes = await recipeVault.getNftEnabledRecipes();
```

### Get a specific workflow

```javascript
// By ID
const recipe = await recipeVault.getRecipe(1);
const workflow = recipe.workflow; // Decompressed ComfyUI JSON

// By hash
const recipeRoot = '0x...';
const recipe2 = await recipeVault.getRecipeByRoot(recipeRoot);
```

### Check if workflow exists before submitting

```javascript
const workflowJson = { /* ComfyUI workflow */ };
const recipeRoot = RecipeVaultSDK.calculateRecipeRoot(workflowJson);
const exists = await recipeVault.recipeExists(recipeRoot);
```

---

## 📋 JobAnchor Integration

### Track worker activity

```javascript
// Check if job already anchored (prevent duplicates)
const jobId = ethers.keccak256(ethers.toUtf8Bytes(`job-${uuid}`));
const isAnchored = await jobAnchor.isJobAnchored(jobId);

// Get worker's activity
const activity = await jobAnchor.getWorkerActivity(workerAddress);
console.log('Active days:', activity.activeDays);
console.log('First active:', activity.firstActiveDay);
```

### Anchor completed jobs

```javascript
const signer = new ethers.Wallet(ANCHOR_PRIVATE_KEY, provider);
const anchorWithSigner = new JobAnchorSDK(JOB_ANCHOR, provider, signer);

// Single job
await anchorWithSigner.anchorJob({
  worker: '0x...',
  modelHash: '0x...',
  inputHash: '0x...', // or provide input object
  outputHash: '0x...', // or provide output object  
  rewardAmount: '1.5', // AIPG
  isVerified: true
});

// Batch anchor (up to 100)
await anchorWithSigner.anchorJobsBatch([
  { worker: '0x...', modelHash: '0x...', rewardAmount: '1.5' },
  { worker: '0x...', modelHash: '0x...', rewardAmount: '2.0' },
  // ...
]);
```

### Get daily statistics

```javascript
// Get today's stats
const today = await jobAnchor.getCurrentDay();
const anchor = await jobAnchor.getDailyAnchor(today);

console.log('Jobs today:', anchor.totalJobs);
console.log('Rewards today:', anchor.totalRewardsFormatted, 'AIPG');

// Get week's anchors
const weekAgo = today - 7;
const weekAnchors = await jobAnchor.getAnchorsForRange(weekAgo, today);
```

---

## 🔧 Deployment

### Deploy contracts (Hardhat)

```bash
npx hardhat compile
npx hardhat run scripts/deploy.js --network base
```

### Grant roles after deployment

```javascript
// ModelRegistry
await modelRegistry.grantRole(REGISTRAR_ROLE, coreBackendAddress);

// RecipeVault
await recipeVault.grantRole(DEFAULT_ADMIN_ROLE, adminMultisig);

// JobAnchor
await jobAnchor.grantRole(ANCHOR_ROLE, coreBackendAddress);
```

---

## 📁 File Structure

```
core-integration-package/
├── README.md                    ← You are here
├── contracts/
│   ├── ModelRegistry.sol        ← Model validation
│   ├── RecipeVault.sol          ← Workflow storage
│   └── JobAnchor.sol            ← Job anchoring
├── sdk/
│   ├── modelregistry-sdk.js     ← ModelRegistry SDK
│   ├── recipevault-sdk.js       ← RecipeVault SDK
│   └── jobanchor-sdk.js         ← JobAnchor SDK
├── abis/                        ← Contract ABIs (JSON)
└── examples/                    ← Usage examples
```

---

## 🌐 Network Info

| Network | Chain ID | RPC |
|---------|----------|-----|
| Base Mainnet | 8453 | https://mainnet.base.org |
| Base Sepolia | 84532 | https://sepolia.base.org |

---

## 📞 Contact

Questions? Reach out to the AIPG team.


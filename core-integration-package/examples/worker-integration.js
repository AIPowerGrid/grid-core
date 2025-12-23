/**
 * Example: Worker Integration with AIPG Blockchain
 * 
 * Shows how a worker can:
 * 1. Check if their models are registered
 * 2. Get model constraints for validation
 * 3. Validate job parameters before execution
 */

const { ethers } = require('ethers');
const ModelRegistrySDK = require('../sdk/modelregistry-sdk');

// Configuration
const CONFIG = {
  RPC_URL: 'https://mainnet.base.org', // or testnet
  MODEL_REGISTRY: '0x...', // Deploy and set address
};

async function workerStartup() {
  console.log('=== Worker Blockchain Integration ===\n');
  
  const provider = new ethers.JsonRpcProvider(CONFIG.RPC_URL);
  const modelRegistry = new ModelRegistrySDK(CONFIG.MODEL_REGISTRY, provider);

  // Step 1: Worker has these models locally
  const workerModels = [
    {
      fileName: 'flux-dev-fp8.safetensors',
      hash: '0x1234...', // Calculate from file: ethers.keccak256(fileBuffer)
    },
    {
      fileName: 'sd_xl_base_1.0.safetensors', 
      hash: '0x5678...',
    }
  ];

  // Step 2: Check which models are registered on-chain
  console.log('Checking registered models...');
  const hashes = workerModels.map(m => m.hash);
  const registered = await modelRegistry.contract.batchCheckExists(hashes);
  
  const validModels = [];
  for (let i = 0; i < workerModels.length; i++) {
    if (registered[i]) {
      console.log(`✅ ${workerModels[i].fileName} - Registered`);
      validModels.push(workerModels[i]);
    } else {
      console.log(`❌ ${workerModels[i].fileName} - Not registered`);
    }
  }

  // Step 3: Get constraints for valid models
  console.log('\nLoading model constraints...');
  for (const model of validModels) {
    const constraints = await modelRegistry.getModelConstraints(model.fileName);
    if (constraints) {
      model.constraints = constraints;
      console.log(`${model.fileName}:`);
      console.log(`  Steps: ${constraints.steps.min}-${constraints.steps.max}`);
      console.log(`  CFG: ${constraints.cfg.min}-${constraints.cfg.max}`);
    }
  }

  return { modelRegistry, validModels };
}

async function validateJobRequest(modelRegistry, job) {
  console.log('\n=== Validating Job Request ===');
  console.log('Model:', job.model);
  console.log('Steps:', job.steps);
  console.log('CFG:', job.cfg);
  console.log('Sampler:', job.sampler);
  console.log('Scheduler:', job.scheduler);

  const validation = await modelRegistry.validateParameters(job.model, {
    steps: job.steps,
    cfg: job.cfg,
    sampler: job.sampler,
    scheduler: job.scheduler
  });

  if (validation.isValid) {
    console.log('✅ Parameters valid - Executing job');
    return true;
  } else {
    console.log('❌ Invalid:', validation.reason);
    return false;
  }
}

// Example usage
async function main() {
  try {
    const { modelRegistry, validModels } = await workerStartup();

    // Simulate incoming job request
    const jobRequest = {
      model: 'flux-dev-fp8.safetensors',
      steps: 28,
      cfg: 3.5,
      sampler: 'euler',
      scheduler: 'simple',
      prompt: 'A beautiful sunset over mountains'
    };

    const isValid = await validateJobRequest(modelRegistry, jobRequest);
    
    if (isValid) {
      // Execute job...
      console.log('\n🎨 Generating image...');
    }

  } catch (error) {
    console.error('Error:', error.message);
  }
}

main();


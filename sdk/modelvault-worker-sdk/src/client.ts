import { createPublicClient, http, keccak256, toHex, toBytes, fromHex } from 'viem';
import { baseSepolia, base } from 'viem/chains';
import { MODEL_REGISTRY_ABI } from './abi';
import type { ModelConstraints, ModelInfo, ValidationResult, ModelRegistryConfig } from './types';

export class ModelVaultClient {
  private client;
  private contractAddress: `0x${string}`;

  constructor(config: ModelRegistryConfig) {
    const chain = config.chainId === 84532 ? baseSepolia : base;
    this.client = createPublicClient({
      chain,
      transport: http(config.rpcUrl),
    });
    this.contractAddress = config.contractAddress as `0x${string}`;
  }

  /**
   * Generate model hash from filename (how models are identified on-chain)
   */
  static hashModel(fileName: string): `0x${string}` {
    return keccak256(toHex(fileName));
  }

  /**
   * Check if a model is registered on-chain
   */
  async isModelRegistered(fileName: string): Promise<boolean> {
    const hash = ModelVaultClient.hashModel(fileName);
    return this.client.readContract({
      address: this.contractAddress,
      abi: MODEL_REGISTRY_ABI,
      functionName: 'isModelExists',
      args: [hash],
    }) as Promise<boolean>;
  }

  /**
   * Get model info from chain
   */
  async getModel(fileName: string): Promise<ModelInfo | null> {
    const hash = ModelVaultClient.hashModel(fileName);
    try {
      const result = await this.client.readContract({
        address: this.contractAddress,
        abi: MODEL_REGISTRY_ABI,
        functionName: 'getModel',
        args: [hash],
      });
      return result as unknown as ModelInfo;
    } catch {
      return null;
    }
  }

  /**
   * Get model constraints (steps, cfg, samplers, schedulers)
   */
  async getConstraints(modelId: string): Promise<ModelConstraints | null> {
    try {
      const result = await this.client.readContract({
        address: this.contractAddress,
        abi: MODEL_REGISTRY_ABI,
        functionName: 'getModelConstraints',
        args: [modelId],
      }) as any;

      return {
        stepsMin: result.stepsMin,
        stepsMax: result.stepsMax,
        cfgMin: result.cfgMinTenths / 10,
        cfgMax: result.cfgMaxTenths / 10,
        clipSkip: result.clipSkip,
        allowedSamplers: result.allowedSamplers
          .map((b: `0x${string}`) => fromHex(b, 'string').replace(/\0/g, ''))
          .filter((s: string) => s.length > 0),
        allowedSchedulers: result.allowedSchedulers
          .map((b: `0x${string}`) => fromHex(b, 'string').replace(/\0/g, ''))
          .filter((s: string) => s.length > 0),
      };
    } catch {
      return null;
    }
  }

  /**
   * Validate job parameters against on-chain constraints
   */
  async validateParams(
    fileName: string,
    params: { steps: number; cfg: number; sampler?: string; scheduler?: string }
  ): Promise<ValidationResult> {
    // Check model exists
    const exists = await this.isModelRegistered(fileName);
    if (!exists) {
      return { isValid: false, reason: `Model '${fileName}' not registered on-chain` };
    }

    // Get constraints (use filename without extension as modelId)
    const modelId = fileName.replace(/\.(safetensors|ckpt|pt)$/, '');
    const constraints = await this.getConstraints(modelId);
    
    if (!constraints) {
      // No constraints = all params allowed
      return { isValid: true };
    }

    // Validate steps
    if (constraints.stepsMax > 0) {
      if (params.steps < constraints.stepsMin) {
        return { isValid: false, reason: `steps ${params.steps} below min ${constraints.stepsMin}` };
      }
      if (params.steps > constraints.stepsMax) {
        return { isValid: false, reason: `steps ${params.steps} exceeds max ${constraints.stepsMax}` };
      }
    }

    // Validate CFG
    if (constraints.cfgMax > 0) {
      if (params.cfg < constraints.cfgMin) {
        return { isValid: false, reason: `cfg ${params.cfg} below min ${constraints.cfgMin}` };
      }
      if (params.cfg > constraints.cfgMax) {
        return { isValid: false, reason: `cfg ${params.cfg} exceeds max ${constraints.cfgMax}` };
      }
    }

    // Validate sampler
    if (params.sampler && constraints.allowedSamplers.length > 0) {
      if (!constraints.allowedSamplers.includes(params.sampler)) {
        return { isValid: false, reason: `sampler '${params.sampler}' not allowed` };
      }
    }

    // Validate scheduler
    if (params.scheduler && constraints.allowedSchedulers.length > 0) {
      if (!constraints.allowedSchedulers.includes(params.scheduler)) {
        return { isValid: false, reason: `scheduler '${params.scheduler}' not allowed` };
      }
    }

    return { isValid: true };
  }
}

// Convenience factory
export function createModelVaultClient(config: Partial<ModelRegistryConfig> = {}): ModelVaultClient {
  return new ModelVaultClient({
    rpcUrl: config.rpcUrl || 'https://sepolia.base.org',
    contractAddress: config.contractAddress || '0xe660455D4A83bbbbcfDCF4219ad82447a831c8A1',
    chainId: config.chainId || 84532,
  });
}





// SPDX-FileCopyrightText: 2026 AI Power Grid
//
// SPDX-License-Identifier: MIT

// ModelVault Worker SDK Types

export interface ModelRegistryConfig {
  rpcUrl: string;
  contractAddress: string;
  chainId: number;
}

export interface ModelConstraints {
  stepsMin: number;
  stepsMax: number;
  cfgMin: number;      // Actual value (contract stores as tenths)
  cfgMax: number;
  clipSkip: number;
  allowedSamplers: string[];
  allowedSchedulers: string[];
}

export interface ModelInfo {
  hash: string;
  modelType: ModelType;
  fileName: string;
  displayName: string;
  description: string;
  isNSFW: boolean;
  sizeBytes: bigint;
  inpainting: boolean;
  img2img: boolean;
  controlnet: boolean;
  lora: boolean;
  baseModel: string;
  architecture: string;
  isActive: boolean;
}

export enum ModelType {
  SD15 = 0,
  SDXL = 1,
  VIDEO = 2,
  FLUX = 3,
  OTHER = 4,
}

export interface ValidationResult {
  isValid: boolean;
  reason?: string;
}

export interface JobPopResponse {
  id: string | null;
  ids: string[];
  skipped: SkippedInfo;
  messages: WorkerMessage[];
}

export interface SkippedInfo {
  blockchain_validation?: {
    count: number;
    reasons: string[];
  };
  [key: string]: unknown;
}

export interface WorkerMessage {
  id: string;
  message: string;
  expiry: string;
}

export interface JobParams {
  model: string;
  steps: number;
  cfg_scale: number;
  sampler_name?: string;
  scheduler?: string;
  width: number;
  height: number;
  seed?: number;
  // ... other params
}





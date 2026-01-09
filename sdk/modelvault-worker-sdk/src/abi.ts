// SPDX-FileCopyrightText: 2026 AI Power Grid
//
// SPDX-License-Identifier: MIT

// ModelRegistry Contract ABI (subset for worker operations)

export const MODEL_REGISTRY_ABI = [
  {
    inputs: [{ name: "modelHash", type: "bytes32" }],
    name: "isModelExists",
    outputs: [{ type: "bool" }],
    stateMutability: "view",
    type: "function",
  },
  {
    inputs: [{ name: "modelHash", type: "bytes32" }],
    name: "getModel",
    outputs: [
      {
        components: [
          { name: "modelHash", type: "bytes32" },
          { name: "modelType", type: "uint8" },
          { name: "fileName", type: "string" },
          { name: "displayName", type: "string" },
          { name: "description", type: "string" },
          { name: "isNSFW", type: "bool" },
          { name: "sizeBytes", type: "uint256" },
          { name: "inpainting", type: "bool" },
          { name: "img2img", type: "bool" },
          { name: "controlnet", type: "bool" },
          { name: "lora", type: "bool" },
          { name: "baseModel", type: "string" },
          { name: "architecture", type: "string" },
          { name: "isActive", type: "bool" },
        ],
        type: "tuple",
      },
    ],
    stateMutability: "view",
    type: "function",
  },
  {
    inputs: [{ name: "modelId", type: "string" }],
    name: "getModelConstraints",
    outputs: [
      {
        components: [
          { name: "stepsMin", type: "uint16" },
          { name: "stepsMax", type: "uint16" },
          { name: "cfgMinTenths", type: "uint16" },
          { name: "cfgMaxTenths", type: "uint16" },
          { name: "clipSkip", type: "uint8" },
          { name: "allowedSamplers", type: "bytes32[]" },
          { name: "allowedSchedulers", type: "bytes32[]" },
        ],
        type: "tuple",
      },
    ],
    stateMutability: "view",
    type: "function",
  },
] as const;





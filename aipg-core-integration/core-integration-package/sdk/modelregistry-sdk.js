// SPDX-FileCopyrightText: 2026 AI Power Grid
//
// SPDX-License-Identifier: MIT

/**
 * AIPG ModelRegistry SDK
 * Simple interface for workers and apps to interact with ModelRegistry contract
 * No NFT overhead - just efficient model discovery and validation
 */

const { ethers } = require('ethers');

class ModelRegistrySDK {
    constructor(contractAddress, provider, signer = null) {
        this.contractAddress = contractAddress;
        this.provider = provider;
        this.signer = signer;
        
        // Use the full compiled ABI to avoid tuple definition issues
        try {
            const ModelRegistryArtifact = require('./artifacts/contracts_active/ModelRegistry.sol/ModelRegistry.json');
            this.abi = ModelRegistryArtifact.abi;
        } catch (error) {
            // Fallback to minimal ABI if artifact not found
            this.abi = [
                "function totalModels() view returns (uint256)",
                "function getModel(uint256 modelId) view returns (tuple(bytes32 modelHash, uint8 modelType, string fileName, string name, string description, bool isNSFW, uint256 sizeBytes, uint256 timestamp, address creator, bool inpainting, bool img2img, bool controlnet, bool lora, string baseModel, string architecture))",
                "function getModelConstraints(string modelId) view returns (bool exists, uint16 stepsMin, uint16 stepsMax, uint16 cfgMinTenths, uint16 cfgMaxTenths, uint8 clipSkip, bytes32[] allowedSamplers, bytes32[] allowedSchedulers)",
                "function isModelExists(bytes32 modelHash) view returns (bool)",
                "function batchCheckExists(bytes32[] modelHashes) view returns (bool[])",
                "event ModelRegistered(uint256 indexed modelId, address indexed creator, bytes32 modelHash, uint8 modelType, string name)",
            ];
        }
        
        this.contract = new ethers.Contract(contractAddress, this.abi, provider);
        if (signer) {
            this.contractWithSigner = this.contract.connect(signer);
        }
        
        // Model type constants
        this.ModelType = {
            TEXT_MODEL: 0,
            IMAGE_MODEL: 1, 
            VIDEO_MODEL: 2
        };
    }

    // ============ WORKER FUNCTIONS ============
    
    /**
     * Get all models available for workers
     * @param {boolean} includeConstraints - Whether to fetch constraints for each model
     * @returns {Array} Array of model objects with parsed data and optional constraints
     */
    async getAvailableModels(includeConstraints = true) {
        const totalModels = await this.contract.totalModels();
        const models = [];
        
        // Get models individually since getAllModels() is reverting
        for (let i = 1; i <= totalModels; i++) {
            try {
                const model = await this.contract.getModel(i);
                const parsedModel = this.parseModel(model);
                models.push(parsedModel);
            } catch (error) {
                console.log(`⚠️  Could not get model ${i}: ${error.message}`);
            }
        }
        
        if (includeConstraints) {
            // Fetch constraints for each model
            for (const model of models) {
                try {
                    const constraints = await this.getModelConstraints(model.fileName);
                    model.constraints = constraints;
                } catch (error) {
                    // Model might not have constraints set, that's ok
                    model.constraints = null;
                }
            }
        }
        
        return models;
    }
    
    /**
     * Get all model hashes (lightweight)
     * @returns {Array<string>} Array of model hashes
     */
    async getAvailableModelHashes() {
        const totalModels = await this.contract.totalModels();
        const hashes = [];
        
        // Get model hashes individually since getAllModelHashes() is reverting
        for (let i = 1; i <= totalModels; i++) {
            try {
                const model = await this.contract.getModel(i);
                hashes.push(model.modelHash);
            } catch (error) {
                console.log(`⚠️  Could not get model ${i} hash: ${error.message}`);
            }
        }
        
        return hashes;
    }
    
    /**
     * Check if worker's models exist
     * @param {Array<string>} modelHashes - Array of hex model hashes
     * @returns {Array<boolean>} Array of existence statuses
     */
    async checkWorkerModels(modelHashes) {
        return await this.contract.batchCheckExists(modelHashes);
    }
    
    /**
     * Get compatible models for a worker
     * @param {Array<string>} workerModelHashes - Hashes of models worker has
     * @param {boolean} includeConstraints - Whether to include constraints (default: true)
     * @returns {Array} Compatible models with full details and constraints
     */
    async getCompatibleModels(workerModelHashes, includeConstraints = true) {
        const [allModels, approvals] = await Promise.all([
            this.getAvailableModels(includeConstraints),
            this.checkWorkerModels(workerModelHashes)
        ]);
        
        const compatibleModels = [];
        const workerHashSet = new Set(workerModelHashes);
        
        for (const model of allModels) {
            if (workerHashSet.has(model.modelHash)) {
                compatibleModels.push(model);
            }
        }
        
        return compatibleModels;
    }
    
    /**
     * Get model constraints for parameter validation
     * @param {string} modelId - Model identifier (e.g., "flux.1-krea-dev")
     * @returns {Object} Constraints object or null if not found
     */
    async getModelConstraints(modelId) {
        const result = await this.contract.getModelConstraints(modelId);
        
        if (!result[0]) { // exists is the first element
            return null;
        }
        
        return {
            exists: result[0],
            steps: { min: Number(result[1]), max: Number(result[2]) },
            cfg: { min: Number(result[3]) / 10, max: Number(result[4]) / 10 },
            clipSkip: Number(result[5]),
            allowedSamplers: result[6],
            allowedSchedulers: result[7]
        };
    }
    
    /**
     * Validate parameters against model constraints
     * @param {string} modelId - Model identifier
     * @param {Object} params - Parameters to validate
     * @returns {Object} Validation result
     */
    async validateParameters(modelId, params) {
        const constraints = await this.getModelConstraints(modelId);
        
        if (!constraints) {
            return { isValid: false, reason: 'Model constraints not found' };
        }
        
        // Validate steps
        if (params.steps < constraints.steps.min || params.steps > constraints.steps.max) {
            return { 
                isValid: false, 
                reason: `Steps must be between ${constraints.steps.min} and ${constraints.steps.max}` 
            };
        }
        
        // Validate CFG
        if (params.cfg < constraints.cfg.min || params.cfg > constraints.cfg.max) {
            return { 
                isValid: false, 
                reason: `CFG must be between ${constraints.cfg.min} and ${constraints.cfg.max}` 
            };
        }
        
        // Validate sampler
        const samplerHash = ethers.keccak256(ethers.toUtf8Bytes(params.sampler));
        if (!constraints.allowedSamplers.includes(samplerHash)) {
            return { 
                isValid: false, 
                reason: `Sampler '${params.sampler}' not allowed for this model` 
            };
        }
        
        // Validate scheduler  
        const schedulerHash = ethers.keccak256(ethers.toUtf8Bytes(params.scheduler));
        if (!constraints.allowedSchedulers.includes(schedulerHash)) {
            return { 
                isValid: false, 
                reason: `Scheduler '${params.scheduler}' not allowed for this model` 
            };
        }
        
        return { isValid: true };
    }

    // ============ ADMIN FUNCTIONS ============
    
    /**
     * Register a new model (requires registrar role)
     * @param {Object} modelData - Model data
     * @returns {Object} Transaction result with modelId
     */
    async registerModel(modelData) {
        if (!this.contractWithSigner) {
            throw new Error('Signer required for registration');
        }
        
        const tx = await this.contractWithSigner.registerModel(
            modelData.modelHash,
            modelData.modelType,
            modelData.fileName,
            modelData.displayName,
            modelData.description,
            modelData.isNSFW,
            modelData.sizeBytes,
            modelData.inpainting || false,
            modelData.img2img || false,
            modelData.controlnet || false,
            modelData.lora || false,
            modelData.baseModel || "",
            modelData.architecture || ""
        );
        
        const receipt = await tx.wait();
        const event = receipt.logs?.find(log => {
            try {
                const parsed = this.contract.interface.parseLog(log);
                return parsed?.name === 'ModelRegistered';
            } catch {
                return false;
            }
        });
        
        let modelId;
        if (event) {
            const parsed = this.contract.interface.parseLog(event);
            modelId = parsed?.args?.modelId;
        }
        
        return { tx, receipt, modelId };
    }
    

    // ============ UTILITY FUNCTIONS ============
    
    /**
     * Parse raw model data from contract
     * @param {Array} rawModel - Raw model tuple from contract
     * @returns {Object} Parsed model object
     */
    parseModel(rawModel) {
        // rawModel is array: [modelHash, modelType, fileName, name, description, isNSFW, sizeBytes, timestamp, creator, inpainting, img2img, controlnet, lora, baseModel, architecture]
        return {
            // Core metadata
            modelHash: rawModel[0],
            modelType: Number(rawModel[1]),
            modelTypeName: this.getModelTypeName(Number(rawModel[1])),
            fileName: rawModel[2],
            name: rawModel[3],
            description: rawModel[4],
            isNSFW: rawModel[5],
            sizeBytes: rawModel[7].toString(),
            timestamp: new Date(Number(rawModel[8]) * 1000),
            creator: rawModel[9],
            
            // Model capabilities (critical for workers)
            capabilities: {
                inpainting: rawModel[9],
                img2img: rawModel[10],
                controlnet: rawModel[11],
                lora: rawModel[12]
            },
            
            // Architecture info
            baseModel: rawModel[13],
            architecture: rawModel[14]
        };
    }
    
    /**
     * Get model type name from enum value
     * @param {number} modelType - Model type enum value
     * @returns {string} Model type name
     */
    getModelTypeName(modelType) {
        switch (modelType) {
            case 0: return 'TEXT_MODEL';
            case 1: return 'IMAGE_MODEL';
            case 2: return 'VIDEO_MODEL';
            default: return 'UNKNOWN';
        }
    }

    /**
     * Get model by hash
     * @param {string} modelHash - Model hash
     * @returns {Object|null} Model object or null if not found
     */
    async getModelByHash(modelHash) {
        const modelId = await this.contract.hashToModelId(modelHash);
        if (Number(modelId) === 0) {
            return null;
        }
        
        const rawModel = await this.contract.models(modelId);
        return this.parseModel(rawModel);
    }
    
    /**
     * Calculate SHA256 hash of file content
     * @param {Buffer|Uint8Array} fileContent - File content
     * @returns {string} Hex hash
     */
    static calculateFileHash(fileContent) {
        return ethers.keccak256(fileContent);
    }
    
    /**
     * Listen for model events
     * @param {string} eventName - Event name ('ModelRegistered' or 'ModelVerified')
     * @param {Function} callback - Callback function
     */
    on(eventName, callback) {
        this.contract.on(eventName, callback);
    }
    
    /**
     * Remove event listeners
     * @param {string} eventName - Event name
     * @param {Function} callback - Callback function
     */
    off(eventName, callback) {
        this.contract.off(eventName, callback);
    }
}

module.exports = ModelRegistrySDK;

/*
 * SPDX-FileCopyrightText: 2026 AI Power Grid
 *
 * SPDX-License-Identifier: MIT
 */

// Active Contract (Base Mainnet): see docs/ADDRESSES.md
// SPDX-License-Identifier: MIT
// Deployed at: 0x21943952F673185f91C6b90472E5c7E0e751Eeb7 (Base Sepolia)
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import "@openzeppelin/contracts/security/Pausable.sol";

/**
 * @title ModelRegistry
 * @dev Simple registry for AI models - no NFT overhead, just efficient model metadata storage
 * @notice Workers use this to discover available AI models and their capabilities
 */
contract ModelRegistry is AccessControl, ReentrancyGuard, Pausable {
    // ============ ROLES ============
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");
    bytes32 public constant REGISTRAR_ROLE = keccak256("REGISTRAR_ROLE");

    // ============ STATE ============
    uint256 private _modelIdCounter;
    
    // Model types
    enum ModelType {
        TEXT_MODEL,   // LLM/Text generation
        IMAGE_MODEL,  // Image generation (Stable Diffusion, etc.)
        VIDEO_MODEL   // Video generation
    }

    // Model record structure - streamlined for efficiency
    struct Model {
        bytes32 modelHash;           // SHA256 of the model file
        ModelType modelType;         // TEXT_MODEL, IMAGE_MODEL, VIDEO_MODEL
        string fileName;             // Actual file name (e.g., "flux-dev-fp8.safetensors")
        string name;                 // Human readable name
        string description;          // Model description
        bool isNSFW;                 // Content policy enforcement
        uint256 sizeBytes;           // File size verification
        uint256 timestamp;           // Creation timestamp
        address creator;             // Creator address
        
        // Model capabilities (for IMAGE/VIDEO models)
        bool inpainting;             // Can do inpainting tasks
        bool img2img;                // Can do image-to-image generation
        bool controlnet;             // Supports ControlNet conditioning
        bool lora;                   // Supports LoRA adapters
        string baseModel;            // Base model architecture (e.g., "sd1.5", "sdxl", "flux.1")
        string architecture;         // Model architecture details
    }

    // Storage - simple and efficient
    mapping(uint256 => Model) public models;
    mapping(bytes32 => uint256) public hashToModelId;  // modelHash => modelId (one model per hash)

    // ============ MODEL CONSTRAINTS ============
    struct ModelConstraints {
        // Common constraints (used by IMAGE/VIDEO models)
        uint16 stepsMin;        // e.g., 28
        uint16 stepsMax;        // e.g., 32
        uint16 cfgMinTenths;    // e.g., 35 => 3.5 (IMAGE/VIDEO)
        uint16 cfgMaxTenths;    // e.g., 50 => 5.0 (IMAGE/VIDEO)
        uint8  clipSkip;        // e.g., 1 (0 if not enforced)
        bytes32[] allowedSamplers;   // keccak256 of canonical sampler names
        bytes32[] allowedSchedulers; // keccak256 of canonical scheduler names
        
        // TEXT model specific constraints
        uint16 tempMinHundredths;    // e.g., 50 => 0.5 temperature
        uint16 tempMaxHundredths;    // e.g., 200 => 2.0 temperature
        uint16 topPMinHundredths;    // e.g., 90 => 0.9 top_p
        uint16 topPMaxHundredths;    // e.g., 100 => 1.0 top_p
        uint16 maxTokensMin;         // minimum token generation
        uint16 maxTokensMax;         // maximum token generation
        
        bool exists;
    }

    // modelIdKey => constraints
    mapping(bytes32 => ModelConstraints) private _constraints;

    // ============ EVENTS ============
    event ModelRegistered(
        uint256 indexed modelId,
        address indexed creator,
        bytes32 modelHash,
        ModelType modelType,
        string name
    );
    
    
    event ModelConstraintsSet(
        string indexed modelId,
        uint16 stepsMin,
        uint16 stepsMax,
        uint16 cfgMinTenths,
        uint16 cfgMaxTenths,
        uint8 clipSkip
    );
    
    event ModelConstraintsSamplersSet(string indexed modelId, bytes32[] samplerHashes);
    event ModelConstraintsSchedulersSet(string indexed modelId, bytes32[] schedulerHashes);

    // ============ MODIFIERS ============
    modifier onlyRegistrar() {
        require(hasRole(REGISTRAR_ROLE, msg.sender), "ModelRegistry: caller is not a registrar");
        _;
    }


    modifier onlyAdmin() {
        require(hasRole(ADMIN_ROLE, msg.sender), "ModelRegistry: caller is not an admin");
        _;
    }

    // ============ CONSTRUCTOR ============
    constructor() {
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(ADMIN_ROLE, msg.sender);
        _grantRole(REGISTRAR_ROLE, msg.sender);

        // Start model IDs at 1 so 0 can be a sentinel for "not found"
        _modelIdCounter = 1;
    }

    // ============ REGISTRATION FUNCTIONS ============
    
    /**
     * @dev Register a new model in the registry
     * @param modelHash SHA256 of the model file
     * @param modelType Type of model (TEXT_MODEL, IMAGE_MODEL, VIDEO_MODEL)
     * @param fileName Actual file name (e.g., "flux-dev-fp8.safetensors", "llama-8b.gguf")
     * @param displayName Human readable name
     * @param description Model description
     * @param isNSFW Whether model is NSFW
     * @param sizeBytes File size in bytes
     * @param inpainting Can do inpainting tasks
     * @param img2img Can do image-to-image generation
     * @param controlnet Supports ControlNet conditioning
     * @param lora Supports LoRA adapters
     * @param baseModel Base model architecture (e.g., "sd1.5", "sdxl", "flux.1")
     * @param architecture Model architecture details
     */
    function registerModel(
        bytes32 modelHash,
        ModelType modelType,
        string memory fileName,
        string memory displayName,
        string memory description,
        bool isNSFW,
        uint256 sizeBytes,
        bool inpainting,
        bool img2img,
        bool controlnet,
        bool lora,
        string memory baseModel,
        string memory architecture
    ) external onlyRegistrar whenNotPaused nonReentrant returns (uint256) {
        require(hashToModelId[modelHash] == 0, "ModelRegistry: model already exists");
        require(bytes(fileName).length > 0, "ModelRegistry: fileName cannot be empty");
        require(bytes(displayName).length > 0, "ModelRegistry: name cannot be empty");
        require(bytes(description).length > 0, "ModelRegistry: description cannot be empty");
        require(sizeBytes > 0, "ModelRegistry: invalid size");

        uint256 modelId = _modelIdCounter;
        _modelIdCounter++;

        Model memory model = Model({
            modelHash: modelHash,
            modelType: modelType,
            fileName: fileName,
            name: displayName,
            description: description,
            isNSFW: isNSFW,
            sizeBytes: sizeBytes,
            timestamp: block.timestamp,
            creator: msg.sender,
            inpainting: inpainting,
            img2img: img2img,
            controlnet: controlnet,
            lora: lora,
            baseModel: baseModel,
            architecture: architecture
        });

        models[modelId] = model;
        hashToModelId[modelHash] = modelId;  // One model per hash

        emit ModelRegistered(modelId, msg.sender, modelHash, modelType, displayName);
        return modelId;
    }


    // ============ CONSTRAINT MANAGEMENT ============
    
    /**
     * @dev Set numeric constraints for a model
     */
    function setModelNumericConstraints(
        string memory modelId,
        uint16 stepsMin,
        uint16 stepsMax,
        uint16 cfgMinTenths,
        uint16 cfgMaxTenths,
        uint8 clipSkip
    ) external onlyAdmin {
        bytes32 key = keccak256(abi.encodePacked(modelId));
        
        // Load existing or create new
        ModelConstraints storage constraints = _constraints[key];
        
        constraints.stepsMin = stepsMin;
        constraints.stepsMax = stepsMax;
        constraints.cfgMinTenths = cfgMinTenths;
        constraints.cfgMaxTenths = cfgMaxTenths;
        constraints.clipSkip = clipSkip;
        constraints.exists = true;
        
        emit ModelConstraintsSet(modelId, stepsMin, stepsMax, cfgMinTenths, cfgMaxTenths, clipSkip);
    }

    /**
     * @dev Set allowed samplers for a model
     */
    function setModelAllowedSamplers(
        string memory modelId,
        bytes32[] memory samplerHashes
    ) external onlyAdmin {
        bytes32 key = keccak256(abi.encodePacked(modelId));
        ModelConstraints storage constraints = _constraints[key];
        constraints.allowedSamplers = samplerHashes;
        constraints.exists = true;
        
        emit ModelConstraintsSamplersSet(modelId, samplerHashes);
    }

    /**
     * @dev Set allowed schedulers for a model
     */
    function setModelAllowedSchedulers(
        string memory modelId,
        bytes32[] memory schedulerHashes
    ) external onlyAdmin {
        bytes32 key = keccak256(abi.encodePacked(modelId));
        ModelConstraints storage constraints = _constraints[key];
        constraints.allowedSchedulers = schedulerHashes;
        constraints.exists = true;
        
        emit ModelConstraintsSchedulersSet(modelId, schedulerHashes);
    }

    // ============ QUERY FUNCTIONS ============

    /**
     * @dev Get model constraints
     */
    function getModelConstraints(string memory modelId) external view returns (
        bool exists,
        uint16 stepsMin,
        uint16 stepsMax,
        uint16 cfgMinTenths,
        uint16 cfgMaxTenths,
        uint8 clipSkip,
        bytes32[] memory allowedSamplers,
        bytes32[] memory allowedSchedulers
    ) {
        bytes32 key = keccak256(abi.encodePacked(modelId));
        ModelConstraints storage constraints = _constraints[key];
        
        return (
            constraints.exists,
            constraints.stepsMin,
            constraints.stepsMax,
            constraints.cfgMinTenths,
            constraints.cfgMaxTenths,
            constraints.clipSkip,
            constraints.allowedSamplers,
            constraints.allowedSchedulers
        );
    }

    /**
     * @dev Validate model parameters against constraints
     */
    function validateModelParams(
        string memory modelId,
        string memory sampler,
        string memory scheduler,
        uint16 steps,
        uint16 cfgTenths,
        uint8 clipSkip
    ) external view returns (bool isValid, string memory reason) {
        bytes32 key = keccak256(abi.encodePacked(modelId));
        ModelConstraints storage constraints = _constraints[key];
        
        if (!constraints.exists) {
            return (false, "No constraints found for model");
        }
        
        // Validate steps
        if (steps < constraints.stepsMin || steps > constraints.stepsMax) {
            return (false, "Steps out of range");
        }
        
        // Validate CFG
        if (cfgTenths < constraints.cfgMinTenths || cfgTenths > constraints.cfgMaxTenths) {
            return (false, "CFG out of range");
        }
        
        // Validate clip skip
        if (constraints.clipSkip > 0 && clipSkip != constraints.clipSkip) {
            return (false, "Invalid clip skip");
        }
        
        // Validate sampler
        bytes32 samplerHash = keccak256(abi.encodePacked(sampler));
        bool samplerValid = false;
        for (uint256 i = 0; i < constraints.allowedSamplers.length; i++) {
            if (constraints.allowedSamplers[i] == samplerHash) {
                samplerValid = true;
                break;
            }
        }
        if (constraints.allowedSamplers.length > 0 && !samplerValid) {
            return (false, "Sampler not allowed");
        }
        
        // Validate scheduler
        bytes32 schedulerHash = keccak256(abi.encodePacked(scheduler));
        bool schedulerValid = false;
        for (uint256 i = 0; i < constraints.allowedSchedulers.length; i++) {
            if (constraints.allowedSchedulers[i] == schedulerHash) {
                schedulerValid = true;
                break;
            }
        }
        if (constraints.allowedSchedulers.length > 0 && !schedulerValid) {
            return (false, "Scheduler not allowed");
        }
        
        return (true, "");
    }

    // ============ WORKER QUERY FUNCTIONS ============

    /**
     * @dev Check if model exists by hash
     */
    function isModelExists(bytes32 modelHash) external view returns (bool) {
        return hashToModelId[modelHash] != 0;
    }

    /**
     * @dev Batch check if models exist
     */
    function batchCheckExists(bytes32[] calldata modelHashes) external view returns (bool[] memory) {
        bool[] memory results = new bool[](modelHashes.length);
        for (uint256 i = 0; i < modelHashes.length; i++) {
            results[i] = hashToModelId[modelHashes[i]] != 0;
        }
        return results;
    }

    /**
     * @dev Get model by hash
     */
    function getModelByHash(bytes32 modelHash) external view returns (Model memory) {
        uint256 modelId = hashToModelId[modelHash];
        require(modelId != 0, "ModelRegistry: model not found");
        return models[modelId];
    }

    /**
     * @dev Get model by ID
     */
    function getModel(uint256 modelId) external view returns (Model memory) {
        require(modelId > 0 && modelId < _modelIdCounter, "ModelRegistry: invalid model ID");
        return models[modelId];
    }

    /**
     * @dev Get total number of registered models
     */
    function totalModels() external view returns (uint256) {
        return _modelIdCounter - 1;
    }

    /**
     * @dev Get all models
     */
    function getAllModels() external view returns (Model[] memory) {
        uint256 modelCount = _modelIdCounter - 1;
        Model[] memory allModels = new Model[](modelCount);
        
        for (uint256 i = 1; i < _modelIdCounter; i++) {
            allModels[i - 1] = models[i];
        }
        
        return allModels;
    }

    /**
     * @dev Get all model hashes (lightweight)
     */
    function getAllModelHashes() external view returns (bytes32[] memory) {
        uint256 modelCount = _modelIdCounter - 1;
        bytes32[] memory hashes = new bytes32[](modelCount);
        
        for (uint256 i = 1; i < _modelIdCounter; i++) {
            hashes[i - 1] = models[i].modelHash;
        }
        
        return hashes;
    }

    // ============ ADMIN FUNCTIONS ============

    /**
     * @dev Pause the contract
     */
    function pause() external onlyAdmin {
        _pause();
    }

    /**
     * @dev Unpause the contract
     */
    function unpause() external onlyAdmin {
        _unpause();
    }
}

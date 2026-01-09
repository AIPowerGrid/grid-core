// SPDX-FileCopyrightText: 2026 AI Power Grid
//
// SPDX-License-Identifier: MIT

/**
 * AIPG RecipeVault SDK
 * Interface for storing and retrieving ComfyUI workflows on-chain
 */

const { ethers } = require('ethers');
const pako = require('pako'); // npm install pako

const RECIPE_VAULT_ABI = [
  "function totalRecipes() view returns (uint256)",
  "function getRecipe(uint256 recipeId) view returns (tuple(uint256 recipeId, bytes32 recipeRoot, bytes workflowData, address creator, bool canCreateNFTs, bool isPublic, uint8 compression, uint256 createdAt, string name, string description))",
  "function getRecipeWorkflow(uint256 recipeId) view returns (bytes)",
  "function getRecipeByRoot(bytes32 recipeRoot) view returns (tuple(uint256 recipeId, bytes32 recipeRoot, bytes workflowData, address creator, bool canCreateNFTs, bool isPublic, uint8 compression, uint256 createdAt, string name, string description))",
  "function getCreatorRecipes(address creator) view returns (uint256[])",
  "function getAllRecipes() view returns (tuple(uint256 recipeId, bytes32 recipeRoot, bytes workflowData, address creator, bool canCreateNFTs, bool isPublic, uint8 compression, uint256 createdAt, string name, string description)[])",
  "function getPublicRecipes() view returns (tuple(uint256 recipeId, bytes32 recipeRoot, bytes workflowData, address creator, bool canCreateNFTs, bool isPublic, uint8 compression, uint256 createdAt, string name, string description)[])",
  "function getNftEnabledRecipes() view returns (tuple(uint256 recipeId, bytes32 recipeRoot, bytes workflowData, address creator, bool canCreateNFTs, bool isPublic, uint8 compression, uint256 createdAt, string name, string description)[])",
  "function isRecipeAllowed(uint256 recipeId) view returns (bool)",
  "function recipeRootToId(bytes32) view returns (uint256)",
  "function addRecipe(bytes32 recipeRoot, bytes workflowData, address creator, string name, string description, bool canCreateNFTs, bool isPublic, uint8 compression) returns (uint256)",
  "event RecipeAdded(uint256 indexed recipeId, address indexed creator, bytes32 recipeRoot, string name, bool canCreateNFTs, bool isPublic)"
];

// Compression enum
const Compression = {
  None: 0,
  Gzip: 1,
  Brotli: 2
};

class RecipeVaultSDK {
  constructor(contractAddress, provider, signer = null) {
    this.contractAddress = contractAddress;
    this.provider = provider;
    this.signer = signer;
    this.contract = new ethers.Contract(contractAddress, RECIPE_VAULT_ABI, provider);
    if (signer) {
      this.contractWithSigner = this.contract.connect(signer);
    }
  }

  // ============ READ FUNCTIONS ============

  /**
   * Get all public recipes
   * @returns {Array} Array of recipe objects with decompressed workflow data
   */
  async getPublicRecipes() {
    const recipes = await this.contract.getPublicRecipes();
    return recipes.map(r => this.parseRecipe(r));
  }

  /**
   * Get NFT-enabled recipes (can be used for minting)
   * @returns {Array} Array of recipe objects
   */
  async getNftEnabledRecipes() {
    const recipes = await this.contract.getNftEnabledRecipes();
    return recipes.map(r => this.parseRecipe(r));
  }

  /**
   * Get a single recipe by ID
   * @param {number} recipeId - Recipe ID
   * @returns {Object} Recipe object with decompressed workflow
   */
  async getRecipe(recipeId) {
    const recipe = await this.contract.getRecipe(recipeId);
    return this.parseRecipe(recipe);
  }

  /**
   * Get recipe by its root hash
   * @param {string} recipeRoot - bytes32 hash of the recipe
   * @returns {Object} Recipe object
   */
  async getRecipeByRoot(recipeRoot) {
    const recipe = await this.contract.getRecipeByRoot(recipeRoot);
    return this.parseRecipe(recipe);
  }

  /**
   * Get workflow JSON directly (decompressed)
   * @param {number} recipeId - Recipe ID
   * @returns {Object} Parsed workflow JSON
   */
  async getWorkflow(recipeId) {
    const recipe = await this.getRecipe(recipeId);
    return recipe.workflow;
  }

  /**
   * Check if recipe exists by root hash
   * @param {string} recipeRoot - bytes32 hash
   * @returns {boolean}
   */
  async recipeExists(recipeRoot) {
    const id = await this.contract.recipeRootToId(recipeRoot);
    return Number(id) > 0;
  }

  /**
   * Get all recipes by a creator
   * @param {string} creatorAddress - Creator's wallet address
   * @returns {Array} Array of recipe IDs
   */
  async getCreatorRecipeIds(creatorAddress) {
    return await this.contract.getCreatorRecipes(creatorAddress);
  }

  // ============ WRITE FUNCTIONS (Admin only) ============

  /**
   * Add a new recipe (requires DEFAULT_ADMIN_ROLE)
   * @param {Object} recipeData - Recipe data
   * @param {Object} workflowJson - ComfyUI workflow JSON
   * @param {string} creatorAddress - Creator's wallet address
   * @returns {Object} Transaction result with recipeId
   */
  async addRecipe(recipeData, workflowJson, creatorAddress) {
    if (!this.contractWithSigner) {
      throw new Error('Signer required for adding recipes');
    }

    // Compress workflow
    const workflowString = JSON.stringify(workflowJson);
    const compressed = pako.gzip(workflowString);
    const workflowBytes = ethers.hexlify(compressed);

    // Calculate recipe root (hash of normalized workflow)
    const recipeRoot = ethers.keccak256(ethers.toUtf8Bytes(workflowString));

    const tx = await this.contractWithSigner.addRecipe(
      recipeRoot,
      workflowBytes,
      creatorAddress,
      recipeData.name,
      recipeData.description || '',
      recipeData.canCreateNFTs || false,
      recipeData.isPublic || true,
      Compression.Gzip
    );

    const receipt = await tx.wait();
    
    // Extract recipeId from event
    const event = receipt.logs?.find(log => {
      try {
        const parsed = this.contract.interface.parseLog(log);
        return parsed?.name === 'RecipeAdded';
      } catch {
        return false;
      }
    });

    let recipeId;
    if (event) {
      const parsed = this.contract.interface.parseLog(event);
      recipeId = parsed?.args?.recipeId;
    }

    return { tx, receipt, recipeId, recipeRoot };
  }

  // ============ UTILITY FUNCTIONS ============

  /**
   * Parse raw recipe from contract
   * @param {Array} rawRecipe - Raw recipe tuple
   * @returns {Object} Parsed recipe with decompressed workflow
   */
  parseRecipe(rawRecipe) {
    const recipe = {
      recipeId: Number(rawRecipe[0]),
      recipeRoot: rawRecipe[1],
      creator: rawRecipe[3],
      canCreateNFTs: rawRecipe[4],
      isPublic: rawRecipe[5],
      compression: Number(rawRecipe[6]),
      createdAt: new Date(Number(rawRecipe[7]) * 1000),
      name: rawRecipe[8],
      description: rawRecipe[9],
      workflow: null
    };

    // Decompress workflow
    try {
      const workflowBytes = ethers.getBytes(rawRecipe[2]);
      let workflowString;
      
      if (recipe.compression === Compression.Gzip) {
        workflowString = pako.ungzip(workflowBytes, { to: 'string' });
      } else {
        workflowString = new TextDecoder().decode(workflowBytes);
      }
      
      recipe.workflow = JSON.parse(workflowString);
    } catch (e) {
      recipe.workflow = null;
      recipe.workflowError = e.message;
    }

    return recipe;
  }

  /**
   * Calculate recipe root hash from workflow JSON
   * @param {Object} workflowJson - ComfyUI workflow
   * @returns {string} bytes32 hash
   */
  static calculateRecipeRoot(workflowJson) {
    return ethers.keccak256(ethers.toUtf8Bytes(JSON.stringify(workflowJson)));
  }
}

module.exports = { RecipeVaultSDK, Compression };


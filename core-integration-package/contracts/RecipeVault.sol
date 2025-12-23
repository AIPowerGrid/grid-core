// Active Contract (Base Mainnet): see docs/ADDRESSES.md
// SPDX-License-Identifier: MIT
// Deployed at: 0x26FAd52658A726927De3331C5F5D01a5b09aC685 (Base Sepolia)
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import "@openzeppelin/contracts/security/Pausable.sol";

/**
 * @title RecipeVault
 * @notice Simplified on-chain storage for AI generation recipes (workflows).
 *         Admin-only recipe management with creator attribution.
 *
 *         - Recipes are standalone entities with unique IDs
 *         - Only admins can add recipes
 *         - Creator wallet addresses are tracked for attribution
 *         - Recipes can be marked as usable for NFT creation
 *         - Stores workflow data on-chain for full decentralization
 */
contract RecipeVault is AccessControl, ReentrancyGuard, Pausable {
    bytes32 public constant PAUSER_ROLE = keccak256("PAUSER_ROLE");

    enum Compression {
        None,
        Gzip,
        Brotli
    }

    struct Recipe {
        uint256 recipeId;           // Unique recipe identifier
        bytes32 recipeRoot;         // Hash of normalized recipe content
        bytes workflowData;         // Actual workflow data (compressed)
        address creator;            // Wallet address of recipe creator
        bool canCreateNFTs;         // Can this recipe be used for NFT creation?
        bool isPublic;              // Public or private recipe
        Compression compression;    // Compression applied to workflow data
        uint256 createdAt;          // Creation timestamp
        string name;                // Human-readable recipe name
        string description;         // Recipe description
    }

    // recipeId -> Recipe
    mapping(uint256 => Recipe) public recipes;
    // recipeRoot -> recipeId (for deduplication)
    mapping(bytes32 => uint256) public recipeRootToId;
    // creator -> recipeIds[]
    mapping(address => uint256[]) public creatorRecipes;
    
    uint256 public nextRecipeId = 1;
    uint256 public totalRecipes = 0;

    // Size caps to contain gas costs
    uint256 public maxWorkflowBytes = 24_576;  // 24KB

    event RecipeAdded(
        uint256 indexed recipeId,
        address indexed creator,
        bytes32 recipeRoot,
        string name,
        bool canCreateNFTs,
        bool isPublic
    );
    event RecipeUpdated(
        uint256 indexed recipeId,
        bool canCreateNFTs,
        bool isPublic
    );
    event MaxWorkflowBytesUpdated(uint256 newMax);

    constructor(address admin) {
        require(admin != address(0), "RecipeVault: admin required");
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(PAUSER_ROLE, admin);
    }

    // --- Admin controls ---
    function pause() external onlyRole(PAUSER_ROLE) {
        _pause();
    }

    function unpause() external onlyRole(PAUSER_ROLE) {
        _unpause();
    }

    function setMaxWorkflowBytes(uint256 newMax) external onlyRole(DEFAULT_ADMIN_ROLE) {
        require(newMax > 0, "RecipeVault: invalid cap");
        maxWorkflowBytes = newMax;
        emit MaxWorkflowBytesUpdated(newMax);
    }

    // --- Recipe management (Admin only) ---
    function addRecipe(
        bytes32 recipeRoot,
        bytes calldata workflowData,
        address creator,
        string calldata name,
        string calldata description,
        bool canCreateNFTs,
        bool isPublic,
        Compression compression
    ) external onlyRole(DEFAULT_ADMIN_ROLE) nonReentrant whenNotPaused returns (uint256) {
        require(recipeRoot != bytes32(0), "RecipeVault: recipeRoot required");
        require(workflowData.length > 0, "RecipeVault: empty workflow data");
        require(workflowData.length <= maxWorkflowBytes, "RecipeVault: workflow too large");
        require(creator != address(0), "RecipeVault: creator required");
        require(bytes(name).length > 0, "RecipeVault: name required");
        require(recipeRootToId[recipeRoot] == 0, "RecipeVault: recipe already exists");

        uint256 recipeId = nextRecipeId++;
        totalRecipes++;

        Recipe storage recipe = recipes[recipeId];
        recipe.recipeId = recipeId;
        recipe.recipeRoot = recipeRoot;
        recipe.workflowData = workflowData;
        recipe.creator = creator;
        recipe.canCreateNFTs = canCreateNFTs;
        recipe.isPublic = isPublic;
        recipe.compression = compression;
        recipe.createdAt = block.timestamp;
        recipe.name = name;
        recipe.description = description;

        recipeRootToId[recipeRoot] = recipeId;
        creatorRecipes[creator].push(recipeId);

        emit RecipeAdded(recipeId, creator, recipeRoot, name, canCreateNFTs, isPublic);
        return recipeId;
    }

    function updateRecipe(
        uint256 recipeId,
        bool canCreateNFTs,
        bool isPublic
    ) external onlyRole(DEFAULT_ADMIN_ROLE) nonReentrant whenNotPaused {
        require(recipeId > 0 && recipeId < nextRecipeId, "RecipeVault: invalid recipe ID");
        
        Recipe storage recipe = recipes[recipeId];
        recipe.canCreateNFTs = canCreateNFTs;
        recipe.isPublic = isPublic;

        emit RecipeUpdated(recipeId, canCreateNFTs, isPublic);
    }

    // --- Recipe queries ---
    function getRecipe(uint256 recipeId) external view returns (Recipe memory) {
        require(recipeId > 0 && recipeId < nextRecipeId, "RecipeVault: invalid recipe ID");
        return recipes[recipeId];
    }

    function getRecipeWorkflow(uint256 recipeId) external view returns (bytes memory) {
        require(recipeId > 0 && recipeId < nextRecipeId, "RecipeVault: invalid recipe ID");
        Recipe memory recipe = recipes[recipeId];
        require(recipe.isPublic, "RecipeVault: recipe not public");
        return recipe.workflowData;
    }

    function getRecipeByRoot(bytes32 recipeRoot) external view returns (Recipe memory) {
        uint256 recipeId = recipeRootToId[recipeRoot];
        require(recipeId > 0, "RecipeVault: recipe not found");
        return recipes[recipeId];
    }

    function getCreatorRecipes(address creator) external view returns (uint256[] memory) {
        return creatorRecipes[creator];
    }

    function getAllRecipes() external view returns (Recipe[] memory) {
        Recipe[] memory allRecipes = new Recipe[](totalRecipes);
        uint256 index = 0;
        for (uint256 i = 1; i < nextRecipeId; i++) {
            allRecipes[index] = recipes[i];
            index++;
        }
        return allRecipes;
    }

    function getPublicRecipes() external view returns (Recipe[] memory) {
        // Count public recipes first
        uint256 publicCount = 0;
        for (uint256 i = 1; i < nextRecipeId; i++) {
            if (recipes[i].isPublic) {
                publicCount++;
            }
        }

        // Create array and populate
        Recipe[] memory publicRecipes = new Recipe[](publicCount);
        uint256 index = 0;
        for (uint256 i = 1; i < nextRecipeId; i++) {
            if (recipes[i].isPublic) {
                publicRecipes[index] = recipes[i];
                index++;
            }
        }
        return publicRecipes;
    }

    function getNftEnabledRecipes() external view returns (Recipe[] memory) {
        // Count NFT-enabled recipes first
        uint256 nftCount = 0;
        for (uint256 i = 1; i < nextRecipeId; i++) {
            if (recipes[i].canCreateNFTs) {
                nftCount++;
            }
        }

        // Create array and populate
        Recipe[] memory nftRecipes = new Recipe[](nftCount);
        uint256 index = 0;
        for (uint256 i = 1; i < nextRecipeId; i++) {
            if (recipes[i].canCreateNFTs) {
                nftRecipes[index] = recipes[i];
                index++;
            }
        }
        return nftRecipes;
    }

    function isRecipeAllowed(uint256 recipeId) external view returns (bool) {
        require(recipeId > 0 && recipeId < nextRecipeId, "RecipeVault: invalid recipe ID");
        return recipes[recipeId].canCreateNFTs;
    }

    // AccessControl interface support
    function supportsInterface(bytes4 interfaceId)
        public
        view
        override(AccessControl)
        returns (bool)
    {
        return super.supportsInterface(interfaceId);
    }
}




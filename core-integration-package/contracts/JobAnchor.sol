/*
 * SPDX-FileCopyrightText: 2026 AI Power Grid
 *
 * SPDX-License-Identifier: MIT
 */

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import "@openzeppelin/contracts/security/Pausable.sol";

/**
 * @title JobAnchor
 * @dev Daily anchors for job receipts and worker activity
 * @notice Provides off-chain job verification with on-chain anchoring
 */
contract JobAnchor is AccessControl, ReentrancyGuard, Pausable {
    // ============ ROLES ============
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");
    bytes32 public constant ANCHOR_ROLE = keccak256("ANCHOR_ROLE");

    // ============ STRUCTS ============
    struct JobReceipt {
        address worker;              // Worker address
        bytes32 jobId;              // Unique job identifier
        bytes32 modelHash;          // Model used for job
        bytes32 inputHash;          // Hash of job input
        bytes32 outputHash;         // Hash of job output
        uint256 timestamp;          // Job completion timestamp
        uint256 rewardAmount;       // AIPG reward amount
        bool isVerified;            // Whether job was verified
    }

    struct DailyAnchor {
        uint256 day;                // Day timestamp (block.timestamp / 86400)
        bytes32 merkleRoot;         // Merkle root of all job receipts for the day
        uint256 totalJobs;          // Total jobs anchored
        uint256 totalRewards;       // Total rewards distributed
        uint256 timestamp;          // Anchor timestamp
        address anchorer;           // Address that created the anchor
    }

    // ============ STATE ============
    mapping(uint256 => DailyAnchor) public dailyAnchors;  // day => DailyAnchor
    mapping(bytes32 => bool) public anchoredJobIds;       // jobId => anchored
    mapping(address => uint256[]) public workerJobDays;   // worker => array of days with jobs
    uint256 public totalAnchoredJobs;
    uint256 public totalAnchoredRewards;

    // ============ EVENTS ============
    event JobReceiptAnchored(
        uint256 indexed day,
        address indexed worker,
        bytes32 indexed jobId,
        bytes32 modelHash,
        uint256 rewardAmount
    );
    
    event DailyAnchorCreated(
        uint256 indexed day,
        bytes32 merkleRoot,
        uint256 totalJobs,
        uint256 totalRewards
    );

    // ============ MODIFIERS ============
    modifier onlyAnchorer() {
        require(hasRole(ANCHOR_ROLE, msg.sender), "JobAnchor: caller is not an anchorer");
        _;
    }

    modifier onlyAdmin() {
        require(hasRole(ADMIN_ROLE, msg.sender), "JobAnchor: caller is not an admin");
        _;
    }

    // ============ CONSTRUCTOR ============
    constructor() {
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(ADMIN_ROLE, msg.sender);
        _grantRole(ANCHOR_ROLE, msg.sender);
    }

    // ============ ANCHORING FUNCTIONS ============
    
    /**
     * @dev Anchor a single job receipt
     * @param jobReceipt Job receipt data
     */
    function anchorJobReceipt(JobReceipt calldata jobReceipt) external onlyAnchorer whenNotPaused nonReentrant {
        require(!anchoredJobIds[jobReceipt.jobId], "JobAnchor: job already anchored");
        require(jobReceipt.timestamp > 0, "JobAnchor: invalid timestamp");
        require(jobReceipt.timestamp <= block.timestamp, "JobAnchor: future timestamp not allowed");
        require(jobReceipt.worker != address(0), "JobAnchor: invalid worker");
        require(jobReceipt.modelHash != bytes32(0), "JobAnchor: invalid model hash");
        require(jobReceipt.inputHash != bytes32(0), "JobAnchor: invalid input hash");
        require(jobReceipt.outputHash != bytes32(0), "JobAnchor: invalid output hash");

        uint256 day = jobReceipt.timestamp / 86400;
        
        // Update daily anchor
        DailyAnchor storage anchor = dailyAnchors[day];
        if (anchor.day == 0) {
            // First job for this day
            anchor.day = day;
            anchor.timestamp = block.timestamp;
            anchor.anchorer = msg.sender;
        }

        // Add job to daily totals
        anchor.totalJobs += 1;
        anchor.totalRewards += jobReceipt.rewardAmount;
        
        // Mark job as anchored
        anchoredJobIds[jobReceipt.jobId] = true;
        
        // Add day to worker's job days if not already present
        uint256[] storage workerDays = workerJobDays[jobReceipt.worker];
        bool dayExists = false;
        for (uint256 i = 0; i < workerDays.length; i++) {
            if (workerDays[i] == day) {
                dayExists = true;
                break;
            }
        }
        if (!dayExists) {
            workerJobDays[jobReceipt.worker].push(day);
        }

        // Update global totals
        totalAnchoredJobs += 1;
        totalAnchoredRewards += jobReceipt.rewardAmount;

        emit JobReceiptAnchored(
            day,
            jobReceipt.worker,
            jobReceipt.jobId,
            jobReceipt.modelHash,
            jobReceipt.rewardAmount
        );
    }

    /**
     * @dev Anchor multiple job receipts in a batch
     * @param jobReceipts Array of job receipt data
     */
    function anchorJobReceipts(JobReceipt[] calldata jobReceipts) external onlyAnchorer whenNotPaused nonReentrant {
        require(jobReceipts.length > 0, "JobAnchor: empty batch");
        require(jobReceipts.length <= 100, "JobAnchor: batch too large"); // Gas limit protection

        uint256 totalJobs = 0;
        uint256 totalRewards = 0;
        
        for (uint256 i = 0; i < jobReceipts.length; i++) {
            JobReceipt calldata jobReceipt = jobReceipts[i];
            require(!anchoredJobIds[jobReceipt.jobId], "JobAnchor: job already anchored");
            require(jobReceipt.timestamp > 0, "JobAnchor: invalid timestamp");
            require(jobReceipt.timestamp <= block.timestamp, "JobAnchor: future timestamp not allowed");
            require(jobReceipt.worker != address(0), "JobAnchor: invalid worker");
            require(jobReceipt.modelHash != bytes32(0), "JobAnchor: invalid model hash");
            require(jobReceipt.inputHash != bytes32(0), "JobAnchor: invalid input hash");
            require(jobReceipt.outputHash != bytes32(0), "JobAnchor: invalid output hash");

            uint256 day = jobReceipt.timestamp / 86400;
            
            // Update daily anchor
            DailyAnchor storage anchor = dailyAnchors[day];
            if (anchor.day == 0) {
                anchor.day = day;
                anchor.timestamp = block.timestamp;
                anchor.anchorer = msg.sender;
            }

            anchor.totalJobs += 1;
            anchor.totalRewards += jobReceipt.rewardAmount;
            
            anchoredJobIds[jobReceipt.jobId] = true;
            
            // Add day to worker's job days if not already present
            uint256[] storage workerDays = workerJobDays[jobReceipt.worker];
            bool dayExists = false;
            for (uint256 j = 0; j < workerDays.length; j++) {
                if (workerDays[j] == day) {
                    dayExists = true;
                    break;
                }
            }
            if (!dayExists) {
                workerDays.push(day);
            }

            totalJobs += 1;
            totalRewards += jobReceipt.rewardAmount;

            emit JobReceiptAnchored(
                day,
                jobReceipt.worker,
                jobReceipt.jobId,
                jobReceipt.modelHash,
                jobReceipt.rewardAmount
            );
        }
        
        // Batch update global totals
        totalAnchoredJobs += totalJobs;
        totalAnchoredRewards += totalRewards;
    }

    /**
     * @dev Finalize daily anchor with merkle root
     * @param day Day to finalize
     * @param merkleRoot Merkle root of all job receipts for the day
     */
    function finalizeDailyAnchor(uint256 day, bytes32 merkleRoot) external onlyAnchorer whenNotPaused {
        DailyAnchor storage anchor = dailyAnchors[day];
        require(anchor.day == day, "JobAnchor: day not found");
        require(anchor.merkleRoot == bytes32(0), "JobAnchor: day already finalized");
        require(merkleRoot != bytes32(0), "JobAnchor: invalid merkle root");

        anchor.merkleRoot = merkleRoot;
        
        emit DailyAnchorCreated(day, merkleRoot, anchor.totalJobs, anchor.totalRewards);
    }

    // ============ ADMIN FUNCTIONS ============
    
    /**
     * @dev Pause contract
     */
    function pause() external onlyAdmin {
        _pause();
    }

    /**
     * @dev Unpause contract
     */
    function unpause() external onlyAdmin {
        _unpause();
    }

    // ============ VIEW FUNCTIONS ============
    
    /**
     * @dev Get daily anchor information
     * @param day Day timestamp
     * @return Daily anchor struct
     */
    function getDailyAnchor(uint256 day) external view returns (DailyAnchor memory) {
        return dailyAnchors[day];
    }

    /**
     * @dev Check if job ID is anchored
     * @param jobId Job ID
     * @return Whether job is anchored
     */
    function isJobAnchored(bytes32 jobId) external view returns (bool) {
        return anchoredJobIds[jobId];
    }

    /**
     * @dev Get worker's job days
     * @param worker Worker address
     * @return Array of days with jobs
     */
    function getWorkerJobDays(address worker) external view returns (uint256[] memory) {
        return workerJobDays[worker];
    }

    /**
     * @dev Get total jobs for a specific day
     * @param day Day timestamp
     * @return Total jobs for the day
     */
    function getTotalJobsForDay(uint256 day) external view returns (uint256) {
        return dailyAnchors[day].totalJobs;
    }

    /**
     * @dev Get total rewards for a specific day
     * @param day Day timestamp
     * @return Total rewards for the day
     */
    function getTotalRewardsForDay(uint256 day) external view returns (uint256) {
        return dailyAnchors[day].totalRewards;
    }

    /**
     * @dev Get current day
     * @return Current day timestamp
     */
    function getCurrentDay() external view returns (uint256) {
        return block.timestamp / 86400;
    }

    /**
     * @dev Get total anchored jobs
     * @return Total anchored jobs
     */
    function getTotalAnchoredJobs() external view returns (uint256) {
        return totalAnchoredJobs;
    }

    /**
     * @dev Get total anchored rewards
     * @return Total anchored rewards
     */
    function getTotalAnchoredRewards() external view returns (uint256) {
        return totalAnchoredRewards;
    }

    /**
     * @dev Get days with anchors in a range
     * @param startDay Start day
     * @param endDay End day
     * @return Array of days that have anchors
     */
    function getDaysWithAnchors(uint256 startDay, uint256 endDay) external view returns (uint256[] memory) {
        require(startDay <= endDay, "JobAnchor: invalid range");
        require(endDay - startDay <= 365, "JobAnchor: range too large"); // Prevent gas issues
        
        uint256 count = 0;
        for (uint256 day = startDay; day <= endDay; day++) {
            if (dailyAnchors[day].day == day) {
                count++;
            }
        }
        
        uint256[] memory result = new uint256[](count);
        uint256 index = 0;
        for (uint256 day = startDay; day <= endDay; day++) {
            if (dailyAnchors[day].day == day) {
                result[index] = day;
                index++;
            }
        }
        
        return result;
    }
}

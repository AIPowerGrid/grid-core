// SPDX-FileCopyrightText: 2026 AI Power Grid
//
// SPDX-License-Identifier: MIT

/**
 * AIPG JobAnchor SDK
 * Interface for anchoring job receipts and tracking worker activity on-chain
 */

const { ethers } = require('ethers');

const JOB_ANCHOR_ABI = [
  "function totalAnchoredJobs() view returns (uint256)",
  "function totalAnchoredRewards() view returns (uint256)",
  "function getCurrentDay() view returns (uint256)",
  "function getDailyAnchor(uint256 day) view returns (tuple(uint256 day, bytes32 merkleRoot, uint256 totalJobs, uint256 totalRewards, uint256 timestamp, address anchorer))",
  "function isJobAnchored(bytes32 jobId) view returns (bool)",
  "function getWorkerJobDays(address worker) view returns (uint256[])",
  "function getTotalJobsForDay(uint256 day) view returns (uint256)",
  "function getTotalRewardsForDay(uint256 day) view returns (uint256)",
  "function getDaysWithAnchors(uint256 startDay, uint256 endDay) view returns (uint256[])",
  "function anchorJobReceipt(tuple(address worker, bytes32 jobId, bytes32 modelHash, bytes32 inputHash, bytes32 outputHash, uint256 timestamp, uint256 rewardAmount, bool isVerified) jobReceipt)",
  "function anchorJobReceipts(tuple(address worker, bytes32 jobId, bytes32 modelHash, bytes32 inputHash, bytes32 outputHash, uint256 timestamp, uint256 rewardAmount, bool isVerified)[] jobReceipts)",
  "function finalizeDailyAnchor(uint256 day, bytes32 merkleRoot)",
  "event JobReceiptAnchored(uint256 indexed day, address indexed worker, bytes32 indexed jobId, bytes32 modelHash, uint256 rewardAmount)",
  "event DailyAnchorCreated(uint256 indexed day, bytes32 merkleRoot, uint256 totalJobs, uint256 totalRewards)"
];

class JobAnchorSDK {
  constructor(contractAddress, provider, signer = null) {
    this.contractAddress = contractAddress;
    this.provider = provider;
    this.signer = signer;
    this.contract = new ethers.Contract(contractAddress, JOB_ANCHOR_ABI, provider);
    if (signer) {
      this.contractWithSigner = this.contract.connect(signer);
    }
  }

  // ============ READ FUNCTIONS ============

  /**
   * Get total anchored jobs across all time
   * @returns {number}
   */
  async getTotalJobs() {
    const total = await this.contract.totalAnchoredJobs();
    return Number(total);
  }

  /**
   * Get total rewards anchored across all time (in wei)
   * @returns {string}
   */
  async getTotalRewards() {
    const total = await this.contract.totalAnchoredRewards();
    return total.toString();
  }

  /**
   * Get current day (UTC day number)
   * @returns {number}
   */
  async getCurrentDay() {
    const day = await this.contract.getCurrentDay();
    return Number(day);
  }

  /**
   * Get daily anchor summary
   * @param {number} day - Day number (timestamp / 86400)
   * @returns {Object} Daily anchor data
   */
  async getDailyAnchor(day) {
    const anchor = await this.contract.getDailyAnchor(day);
    return {
      day: Number(anchor[0]),
      merkleRoot: anchor[1],
      totalJobs: Number(anchor[2]),
      totalRewards: anchor[3].toString(),
      totalRewardsFormatted: ethers.formatEther(anchor[3]),
      timestamp: new Date(Number(anchor[4]) * 1000),
      anchorer: anchor[5],
      isFinalized: anchor[1] !== ethers.ZeroHash
    };
  }

  /**
   * Check if a job has been anchored
   * @param {string} jobId - bytes32 job ID
   * @returns {boolean}
   */
  async isJobAnchored(jobId) {
    return await this.contract.isJobAnchored(jobId);
  }

  /**
   * Get days a worker has completed jobs
   * @param {string} workerAddress - Worker's wallet address
   * @returns {Array<number>} Array of day numbers
   */
  async getWorkerJobDays(workerAddress) {
    const days = await this.contract.getWorkerJobDays(workerAddress);
    return days.map(d => Number(d));
  }

  /**
   * Get worker activity summary
   * @param {string} workerAddress - Worker's wallet address
   * @returns {Object} Worker activity summary
   */
  async getWorkerActivity(workerAddress) {
    const days = await this.getWorkerJobDays(workerAddress);
    
    let totalJobs = 0;
    let totalRewards = BigInt(0);
    const dailyBreakdown = [];

    for (const day of days) {
      const anchor = await this.getDailyAnchor(day);
      // Note: This gets total for the day, not per-worker
      // For per-worker tracking, parse events
      dailyBreakdown.push({
        day,
        date: new Date(day * 86400 * 1000).toISOString().split('T')[0]
      });
    }

    return {
      worker: workerAddress,
      activeDays: days.length,
      firstActiveDay: days.length > 0 ? days[0] : null,
      lastActiveDay: days.length > 0 ? days[days.length - 1] : null,
      dailyBreakdown
    };
  }

  /**
   * Get anchors for a date range
   * @param {Date|number} startDate - Start date or day number
   * @param {Date|number} endDate - End date or day number
   * @returns {Array} Array of daily anchors
   */
  async getAnchorsForRange(startDate, endDate) {
    const startDay = startDate instanceof Date 
      ? Math.floor(startDate.getTime() / 1000 / 86400)
      : startDate;
    const endDay = endDate instanceof Date
      ? Math.floor(endDate.getTime() / 1000 / 86400)
      : endDate;

    const days = await this.contract.getDaysWithAnchors(startDay, endDay);
    const anchors = [];

    for (const day of days) {
      const anchor = await this.getDailyAnchor(Number(day));
      anchors.push(anchor);
    }

    return anchors;
  }

  // ============ WRITE FUNCTIONS (Anchorer role required) ============

  /**
   * Anchor a single job receipt
   * @param {Object} jobReceipt - Job receipt data
   * @returns {Object} Transaction result
   */
  async anchorJob(jobReceipt) {
    if (!this.contractWithSigner) {
      throw new Error('Signer required for anchoring');
    }

    const receipt = {
      worker: jobReceipt.worker,
      jobId: jobReceipt.jobId || this.generateJobId(jobReceipt),
      modelHash: jobReceipt.modelHash,
      inputHash: jobReceipt.inputHash || ethers.keccak256(ethers.toUtf8Bytes(JSON.stringify(jobReceipt.input || {}))),
      outputHash: jobReceipt.outputHash || ethers.keccak256(ethers.toUtf8Bytes(JSON.stringify(jobReceipt.output || {}))),
      timestamp: jobReceipt.timestamp || Math.floor(Date.now() / 1000),
      rewardAmount: ethers.parseEther(jobReceipt.rewardAmount.toString()),
      isVerified: jobReceipt.isVerified || false
    };

    const tx = await this.contractWithSigner.anchorJobReceipt(receipt);
    return await tx.wait();
  }

  /**
   * Anchor multiple job receipts in batch
   * @param {Array} jobReceipts - Array of job receipt objects
   * @returns {Object} Transaction result
   */
  async anchorJobsBatch(jobReceipts) {
    if (!this.contractWithSigner) {
      throw new Error('Signer required for anchoring');
    }

    if (jobReceipts.length > 100) {
      throw new Error('Batch size cannot exceed 100');
    }

    const receipts = jobReceipts.map(jr => ({
      worker: jr.worker,
      jobId: jr.jobId || this.generateJobId(jr),
      modelHash: jr.modelHash,
      inputHash: jr.inputHash || ethers.keccak256(ethers.toUtf8Bytes(JSON.stringify(jr.input || {}))),
      outputHash: jr.outputHash || ethers.keccak256(ethers.toUtf8Bytes(JSON.stringify(jr.output || {}))),
      timestamp: jr.timestamp || Math.floor(Date.now() / 1000),
      rewardAmount: ethers.parseEther(jr.rewardAmount.toString()),
      isVerified: jr.isVerified || false
    }));

    const tx = await this.contractWithSigner.anchorJobReceipts(receipts);
    return await tx.wait();
  }

  /**
   * Finalize a day with merkle root
   * @param {number} day - Day number
   * @param {string} merkleRoot - bytes32 merkle root
   * @returns {Object} Transaction result
   */
  async finalizeDailyAnchor(day, merkleRoot) {
    if (!this.contractWithSigner) {
      throw new Error('Signer required for finalizing');
    }

    const tx = await this.contractWithSigner.finalizeDailyAnchor(day, merkleRoot);
    return await tx.wait();
  }

  // ============ UTILITY FUNCTIONS ============

  /**
   * Generate a unique job ID
   * @param {Object} jobData - Job data
   * @returns {string} bytes32 job ID
   */
  generateJobId(jobData) {
    const data = `${jobData.worker}-${jobData.modelHash}-${jobData.timestamp || Date.now()}-${Math.random()}`;
    return ethers.keccak256(ethers.toUtf8Bytes(data));
  }

  /**
   * Convert date to day number
   * @param {Date} date 
   * @returns {number}
   */
  static dateToDay(date) {
    return Math.floor(date.getTime() / 1000 / 86400);
  }

  /**
   * Convert day number to date
   * @param {number} day 
   * @returns {Date}
   */
  static dayToDate(day) {
    return new Date(day * 86400 * 1000);
  }

  /**
   * Listen for job anchored events
   * @param {Function} callback 
   */
  onJobAnchored(callback) {
    this.contract.on('JobReceiptAnchored', (day, worker, jobId, modelHash, rewardAmount) => {
      callback({
        day: Number(day),
        worker,
        jobId,
        modelHash,
        rewardAmount: ethers.formatEther(rewardAmount)
      });
    });
  }
}

module.exports = JobAnchorSDK;


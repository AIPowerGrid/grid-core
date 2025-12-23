/**
 * Example: Job Anchoring for Rewards Tracking
 * 
 * Shows how the AIPG Core backend can:
 * 1. Anchor completed jobs on-chain
 * 2. Track worker activity
 * 3. Generate daily reports
 */

const { ethers } = require('ethers');
const JobAnchorSDK = require('../sdk/jobanchor-sdk');

// Configuration
const CONFIG = {
  RPC_URL: 'https://mainnet.base.org',
  JOB_ANCHOR: '0x...', // Deploy and set address
  ANCHOR_PRIVATE_KEY: process.env.ANCHOR_PRIVATE_KEY, // Address with ANCHOR_ROLE
};

async function anchorCompletedJobs() {
  console.log('=== Job Anchoring Example ===\n');
  
  const provider = new ethers.JsonRpcProvider(CONFIG.RPC_URL);
  const signer = new ethers.Wallet(CONFIG.ANCHOR_PRIVATE_KEY, provider);
  const jobAnchor = new JobAnchorSDK(CONFIG.JOB_ANCHOR, provider, signer);

  // Completed jobs from the day (from your database)
  const completedJobs = [
    {
      worker: '0x1234567890123456789012345678901234567890',
      modelHash: '0xabcd...', // Hash of model used
      input: { prompt: 'A cat', steps: 28, cfg: 3.5 },
      output: { imageHash: '0xefgh...' },
      rewardAmount: '1.5', // AIPG
      isVerified: true,
    },
    {
      worker: '0x0987654321098765432109876543210987654321',
      modelHash: '0xijkl...',
      input: { prompt: 'A dog', steps: 30, cfg: 4.0 },
      output: { imageHash: '0xmnop...' },
      rewardAmount: '2.0',
      isVerified: true,
    },
  ];

  // Check for duplicates
  console.log('Checking for already-anchored jobs...');
  const newJobs = [];
  for (const job of completedJobs) {
    const jobId = jobAnchor.generateJobId(job);
    const isAnchored = await jobAnchor.isJobAnchored(jobId);
    if (!isAnchored) {
      job.jobId = jobId;
      newJobs.push(job);
    } else {
      console.log(`  Skipping already-anchored job: ${jobId.slice(0, 10)}...`);
    }
  }

  if (newJobs.length === 0) {
    console.log('No new jobs to anchor');
    return;
  }

  // Batch anchor new jobs
  console.log(`\nAnchoring ${newJobs.length} jobs...`);
  const receipt = await jobAnchor.anchorJobsBatch(newJobs);
  console.log('✅ Anchored! TX:', receipt.hash);

  // Get updated stats
  const today = await jobAnchor.getCurrentDay();
  const dailyAnchor = await jobAnchor.getDailyAnchor(today);
  
  console.log('\n📊 Today\'s Stats:');
  console.log('  Total Jobs:', dailyAnchor.totalJobs);
  console.log('  Total Rewards:', dailyAnchor.totalRewardsFormatted, 'AIPG');
}

async function generateDailyReport() {
  console.log('\n=== Daily Report ===\n');
  
  const provider = new ethers.JsonRpcProvider(CONFIG.RPC_URL);
  const jobAnchor = new JobAnchorSDK(CONFIG.JOB_ANCHOR, provider);

  const today = await jobAnchor.getCurrentDay();
  const weekAgo = today - 7;

  console.log('Last 7 days:\n');
  
  const anchors = await jobAnchor.getAnchorsForRange(weekAgo, today);
  
  let totalJobs = 0;
  let totalRewards = BigInt(0);

  for (const anchor of anchors) {
    const date = JobAnchorSDK.dayToDate(anchor.day).toISOString().split('T')[0];
    console.log(`${date}: ${anchor.totalJobs} jobs, ${anchor.totalRewardsFormatted} AIPG`);
    totalJobs += anchor.totalJobs;
    totalRewards += BigInt(anchor.totalRewards);
  }

  console.log('\n---');
  console.log(`Total: ${totalJobs} jobs, ${ethers.formatEther(totalRewards)} AIPG`);
}

async function main() {
  try {
    await anchorCompletedJobs();
    await generateDailyReport();
  } catch (error) {
    console.error('Error:', error.message);
  }
}

main();


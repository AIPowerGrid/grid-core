# Image Generation Failures and Performance Analysis

## Executive Summary
Based on deep analysis of the horde.log file, there are **21 stale/failed image generations today (Sept 29, 2025)** that are timing out after ~3 minutes each.

## Key Findings

### 1. **Primary Issue: Worker Timeouts**
All failures are "Aborted Stale Generation" - workers are taking jobs but not completing them within the TTL (Time To Live) period.

**Timeout Duration:** ~3 minutes (150-180 seconds)
- Generation starts (popped)
- After 3 minutes, if no response → Aborted as "stale"
- System then reassigns to another worker (which often also times out)

### 2. **Problematic Workers**
```
15 failures: ameli0x.AdD3DPyBpd2pgoAQD59EwZLniVwmC6Gfj9 (Worker ID: 8bef6410-8d74-4a24-b20d-57e593580dff)
 5 failures: half5090ya11.Ae5JCH4WfWcu4wjZmv8ZpTPRcK11y3Cb95 (Worker ID: 106e77b1-0bd3-477d-97bd-d8308f9fc10a)
 1 failure:  ameli0x_2.AdD3DPyBpd2pgoAQD59EwZLniVwmC6Gfj9 (Worker ID: 0a889c4b-2397-4b15-ab85-8ac772fb70e7)
```

**Worker "ameli0x" is responsible for 71% of all failures.**

### 3. **All Failures Are Same Configuration**
- **Image Size:** 1024x1024
- **Steps:** 4
- **Sampler:** k_euler

This is a standard, relatively lightweight configuration that should complete quickly.

### 4. **Timeout Logic**
From code analysis (`horde/classes/stable/processing_generation.py`):

```python
# Base timeout: 150 seconds minimum
ttl_multiplier = (width * height) / (512 * 512)
job_ttl = 30 + (steps * 2 * ttl_multiplier)

# For 1024x1024x4:
# ttl_multiplier = (1024*1024) / (512*512) = 4
# job_ttl = 30 + (4 * 2 * 4) = 30 + 32 = 62 seconds
# BUT: minimum is 150 seconds, so job_ttl = 150
```

**These jobs should take ~30-60 seconds but are timing out after 150+ seconds.**

### 5. **Pattern Analysis**
Example timeline for one failing request (WP: 6745ea8c-cdc5-46e3-a6ee-40228b74ca3b):

```
17:41:08 - Worker half5090ya11 picks up job (7a61b5ad)
17:44:04 - TIMEOUT (3 minutes) → Aborted

17:44:05 - Worker ameli0x picks up same job (df495c4e)  
17:47:04 - TIMEOUT (3 minutes) → Aborted

17:47:05 - Worker ameli0x picks up same job again (f5f4428f)
17:50:04 - TIMEOUT (3 minutes) → Aborted
```

The same request failed 3 times consecutively, taking 9 minutes total before being abandoned!

## Root Causes

### Most Likely Issues:

1. **Worker Performance Problems**
   - Workers are claiming jobs but not processing them
   - Could be: crashed workers, network issues, model loading failures, GPU issues

2. **Specific Worker Issues (ameli0x)**
   - This worker may have:
     - Hardware problems (GPU overheating, memory issues)
     - Network connectivity issues
     - Software bugs in the worker client
     - Model loading failures

3. **No Error Reporting from Workers**
   - Workers are NOT returning error states
   - They're simply not responding at all
   - The server only knows they've failed when the timeout expires

## Recommendations

### Immediate Actions:

1. **Flag/Suspend Worker ameli0x**
   - Investigate why this worker is accepting but not completing jobs
   - Check worker logs on IP 99.98.240.178
   - May need to mark as unreliable or temporarily suspend

2. **Investigate Worker half5090ya11**
   - Secondary problem worker on IP 74.143.56.82
   - Similar timeout pattern

3. **Add Worker Health Checks**
   - Implement heartbeat/ping mechanism
   - Workers should report progress during long jobs
   - Faster timeout for workers that consistently fail

4. **Improve Logging**
   - Add detailed timing metrics to log:
     - When worker accepts job
     - When worker starts processing
     - How long each generation actually takes
   - Log worker response times

### Medium-Term Improvements:

1. **Worker Reputation System**
   - Track success/failure rates per worker
   - Deprioritize workers with high failure rates
   - Automatic suspension after N consecutive failures

2. **Progressive Timeout**
   - Start with shorter timeouts for fast workers
   - Only use long timeouts for workers marked as "extra_slow_worker"

3. **Better Job Reassignment**
   - Don't immediately reassign to the same failing workers
   - Implement backoff for problem workers

4. **Worker-Side Diagnostics**
   - Request workers to report if they're having issues
   - Include error messages in responses
   - Track: model loading time, generation time, upload time

## Current System Behavior

The system IS working correctly from the server side:
- Detects stale jobs after TTL
- Automatically aborts and reassigns
- Eventually completes or fails the request properly

However, **users are experiencing significant delays** (9+ minutes for requests that should take 1-2 minutes).

## Files to Investigate

1. **Timeout Configuration:** `horde/classes/stable/processing_generation.py:156-175`
2. **Stale Job Detection:** `horde/database/threads.py:216-219`
3. **Worker Response Handling:** `horde/classes/stable/processing_generation.py`


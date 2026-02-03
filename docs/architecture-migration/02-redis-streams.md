# Phase 2: Redis Streams for Job Queue

## Overview

Replace HTTP polling with Redis Streams. Workers block on Redis instead of hammering the API.

**Benefits:**
- Zero wasted requests (workers block until job available)
- Redis handles millions of ops/sec
- Built-in consumer groups for load balancing
- Message persistence and replay

## Redis Streams Concepts

| Concept | Description |
|---------|-------------|
| **Stream** | Append-only log of messages |
| **XADD** | Add message to stream |
| **XREAD** | Read messages (can block) |
| **Consumer Group** | Multiple workers share a stream |
| **XACK** | Acknowledge message processed |

## Job Queue Service

Create `horde/fastapi_app/services/job_queue.py`:

```python
import json
import redis.asyncio as redis

class JobQueue:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.redis = None
        self.streams = {
            "image": "jobs:image",
            "text": "jobs:text",
        }
        self.consumer_group = "workers"
    
    async def connect(self):
        self.redis = redis.from_url(self.redis_url, decode_responses=True)
        for stream in self.streams.values():
            try:
                await self.redis.xgroup_create(stream, self.consumer_group, id="0", mkstream=True)
            except redis.ResponseError:
                pass  # Group exists
    
    async def submit_job(self, job_type, job_id, payload, models):
        """Add job to stream."""
        stream = self.streams.get(job_type, "jobs:image")
        return await self.redis.xadd(stream, {
            "job_id": job_id,
            "payload": json.dumps(payload),
            "models": json.dumps(models),
        })
    
    async def pop_job(self, worker_id, job_type, timeout=30):
        """
        Pop job for worker. BLOCKS until available or timeout.
        This is the key - no polling!
        """
        stream = self.streams.get(job_type, "jobs:image")
        
        results = await self.redis.xreadgroup(
            groupname=self.consumer_group,
            consumername=worker_id,
            streams={stream: ">"},
            count=1,
            block=timeout * 1000,
        )
        
        if not results:
            return None
        
        _, messages = results[0]
        msg_id, data = messages[0]
        
        return {
            "stream_id": msg_id,
            "id": data["job_id"],
            "payload": json.loads(data["payload"]),
        }
    
    async def ack_job(self, job_type, stream_id):
        """Mark job complete."""
        stream = self.streams.get(job_type, "jobs:image")
        await self.redis.xack(stream, self.consumer_group, stream_id)
```

## Producer (Job Submission)

```python
@router.post("/async", status_code=202)
async def submit_generation(request, user, job_queue):
    wp = create_waiting_prompt(user, request)
    
    await job_queue.submit_job(
        job_type="image",
        job_id=str(wp.id),
        payload=request.model_dump(),
        models=request.models,
    )
    
    return {"id": wp.id}
```

## Consumer (Worker Pop)

```python
@router.post("/pop")
async def pop_job(request, worker, job_queue):
    # BLOCKS on Redis - no HTTP polling!
    job = await job_queue.pop_job(
        worker_id=str(worker.id),
        job_type="image",
        timeout=30,
    )
    
    if job:
        return {"id": job["id"], "payload": job["payload"]}
    return {"id": None}
```

## Redis CLI Commands

```bash
XINFO STREAM jobs:image      # Stream info
XPENDING jobs:image workers  # Pending messages
XRANGE jobs:image - + COUNT 10  # View messages
```

## Next

Proceed to [Phase 3: LLM Streaming](./03-llm-streaming.md).

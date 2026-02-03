<!--
SPDX-FileCopyrightText: 2026 AI Power Grid

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Phase 3: LLM Token Streaming

## Overview
Enable real-time token streaming using SSE + Redis Pub/Sub.

Flow: User <-- SSE <-- API <-- Redis Pub/Sub <-- Worker

## Key Components

1. **StreamingService** - Wraps Redis Pub/Sub
2. **SSE Endpoint** - GET /{job_id}/stream returns EventSourceResponse  
3. **Worker** - Publishes tokens via PUBLISH stream:{job_id} token

## StreamingService

- publish_token(job_id, token) - Worker publishes each token
- subscribe(job_id) - API subscribes, yields tokens as AsyncGenerator

## SSE Endpoint

- Uses sse-starlette EventSourceResponse
- Subscribes to Redis channel for job_id
- Yields token events until [DONE] received

## Worker Integration

- Worker calls redis.publish(f"stream:{job_id}", token) for each token
- Sends [DONE] when complete

## Client Usage

JavaScript: new EventSource(url), listen for token/done events
Python: sseclient library with requests stream=True

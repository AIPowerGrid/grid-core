# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prometheus metric objects — shared between routers without circular imports."""

from prometheus_client import Counter, Gauge, Histogram

REQUESTS_TOTAL = Counter("grid_requests_total", "Total API requests", ["endpoint", "method", "status"])
TOKENS_GENERATED = Counter("grid_tokens_generated_total", "Total tokens generated across all jobs")
DEN_AWARDED = Counter("grid_den_awarded_total", "Total den awarded to workers")
JOBS_COMPLETED = Counter("grid_jobs_completed_total", "Total jobs completed")
JOBS_FAILED = Counter("grid_jobs_failed_total", "Total jobs that failed or were requeued")
WORKERS_CONNECTED = Gauge("grid_workers_connected", "Number of WebSocket workers currently connected")
QUEUE_DEPTH = Gauge("grid_queue_depth", "Number of jobs in the Redis Stream queue")
MODELS_AVAILABLE = Gauge("grid_models_available", "Number of distinct models available from connected workers")
GENERATION_DURATION = Histogram("grid_generation_duration_seconds", "Total generation time", buckets=[1, 2, 5, 10, 20, 30, 60, 120, 300])


def record_job_complete(tokens: int, den: float, duration: float):
    JOBS_COMPLETED.inc()
    TOKENS_GENERATED.inc(tokens)
    DEN_AWARDED.inc(den)
    GENERATION_DURATION.observe(duration)


def record_job_failed():
    JOBS_FAILED.inc()

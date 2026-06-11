# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class GridSettings(BaseSettings):
    # PostgreSQL — reads the same env vars as the Flask app
    postgres_user: str = "postgres"
    postgres_pass: str = "changeme"
    postgres_url: str = "localhost/postgres"  # host/dbname format from .env

    # Redis
    redis_ip: str = "localhost"
    redis_port: int = 6379
    redis_stream_db: int = 7  # Dedicated DB for Streams + Pub/Sub (avoids 0-5 used by Flask)

    # Grid API server
    grid_api_host: str = "0.0.0.0"
    grid_api_port: int = 7002

    # Base URL of the legacy Flask API that grid_api proxies image/video jobs to.
    # Defaults to the co-located Flask pool on localhost. Override (FLASK_API_BASE)
    # to point at a separate API/Flask VM when the stateless tier is split out.
    flask_api_base: str = "http://127.0.0.1:7001"

    # Timeouts
    job_timeout_seconds: int = 300  # 5 min max generation time
    worker_ping_interval: int = 30  # Keepalive ping every 30s
    stream_subscribe_timeout: int = 300  # SSE connection max lifetime

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def async_database_url(self) -> str:
        """Construct asyncpg connection URL from the existing env var format."""
        # POSTGRES_URL in .env is "host/dbname" (e.g. "172.22.22.24/postgres")
        host_db = self.postgres_url
        return f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_pass}@{host_db}"

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_ip}:{self.redis_port}/{self.redis_stream_db}"


@lru_cache
def get_settings() -> GridSettings:
    return GridSettings()

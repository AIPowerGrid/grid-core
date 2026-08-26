# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GridSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL — reads the same env vars as the Flask app
    postgres_user: str = "postgres"
    postgres_pass: str = "changeme"
    postgres_url: str = "localhost/postgres"  # host/dbname format from .env

    # Redis Streams, pub/sub, quota, and rate limiting
    redis_ip: str = "localhost"
    redis_port: int = 6379
    redis_stream_db: int = 7

    # Grid API server
    grid_api_host: str = "0.0.0.0"
    grid_api_port: int = 7002

    # Timeouts
    job_timeout_seconds: int = 300  # 5 min max generation time
    worker_ping_interval: int = 30  # Keepalive ping every 30s
    stream_subscribe_timeout: int = 300  # SSE connection max lifetime

    # Worker identity. Managed profiles and audio workers always require this;
    # flip the global gate after every worker runtime supports delegation.
    worker_identity_audience: str = "api.aipowergrid.io"
    worker_identity_chain_id: int = 8453
    worker_registration_skew_seconds: int = 300
    require_worker_identity: bool = False
    # Comma-separated SHA-256 profile digests approved after signature and
    # exact-scope hardware qualification. Empty accepts no managed profile.
    approved_worker_profile_digests: str = ""
    # Product gate separate from profile approval and global charging. A
    # private pilot may enable this after its exact-hardware canary; public
    # manager publication remains a separate, broader qualification gate.
    audio_enabled: bool = False
    # Native-manager device enrollment. Independently dark until the console
    # approval page and release manager are deployed together.
    worker_enrollment_enabled: bool = False
    worker_enrollment_console_url: str = (
        "https://console.aipowergrid.io/dashboard/connect-worker"
    )
    worker_enrollment_ttl_seconds: int = 900

    # Assignment-bound media validation. This stays dark until every field is
    # explicitly configured against a reviewed, deployed WorkerRegistry and a
    # fresh finalized-block/quality sync has populated the reference pool.
    # When enabled, assignment polling discloses only an opaque assignment id
    # and a SHA-256 seal. Target, nonce, model, and challenge are revealed in
    # the completed probe result, after the worker has already produced output.
    validator_sealed_assignments_enabled: bool = False
    validator_media_probe_enabled: bool = False
    # Objective video container/timing/motion checks are independently gated.
    # This does not enable reference-based video fidelity scoring.
    validator_video_probe_enabled: bool = False
    validator_media_bond_chain_id: int = 8453
    validator_media_bond_contract: str = ""
    validator_media_bond_verifier_version: str = ""
    validator_media_minimum_bond_raw: int = 0
    validator_media_minimum_quality_pass_rate: float = 0.95
    validator_media_max_output_bytes: int = 25 * 1024 * 1024
    validator_media_probe_timeout_seconds: int = 600
    # Bound economically inert preview work and its operational DB footprint.
    # A completed quorum must not immediately manufacture another free probe
    # group for the same worker/model.
    validator_text_group_min_interval_seconds: int = 3600
    validator_history_retention_days: int = 90
    validator_history_sweep_seconds: int = 21600

    # Best-effort operator alerts. The webhook is a production secret and must
    # never be committed, logged, or returned by an API.
    grid_alert_discord_webhook: SecretStr | None = None
    grid_alert_queue_size: int = 256
    grid_alert_dedupe_seconds: int = 300

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

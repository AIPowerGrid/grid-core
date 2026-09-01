# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GridSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

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

    # Optional account visibility for an already-enrolled validator. This does
    # not move the node account, issue keys, or grant economic authority.
    validator_pairing_enabled: bool = False
    # Private, time-bounded pilot; both node and human accounts must be listed.
    validator_pairing_canary_accounts: list[UUID] = Field(default_factory=list, max_length=10, repr=False)
    validator_pairing_canary_until: AwareDatetime | None = Field(default=None, repr=False)
    validator_pairing_audience: str = "https://api.aipowergrid.io"
    validator_pairing_console_url: str = (
        "https://console.aipowergrid.io/dashboard/connect-validator"
    )

    # Shared Base read endpoint. SecretStr prevents authenticated provider URLs
    # from appearing in settings reprs or operational logs.
    base_rpc_url: SecretStr | None = None

    # RecipeVault is an executable-graph authority, so it has an explicit dark
    # gate and a second independent RPC. The selected verifier names a runtime
    # compiled into this Core release; env cannot supply an arbitrary hash.
    recipevault_sync_enabled: bool = False
    recipevault_address: str = ""
    recipevault_confirmation_rpc_url: SecretStr | None = None
    recipevault_verifier_version: str = ""
    recipevault_chain_id: int = 8453
    recipevault_max_records: int = 256
    recipevault_max_workflow_bytes: int = 256 * 1024
    recipevault_rpc_timeout_seconds: int = 20
    recipevault_max_finalized_age_seconds: int = 1800
    recipevault_max_stale_seconds: int = 1800
    recipe_sync_seconds: int = 600

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
    validator_media_bond_confirmation_rpc_url: SecretStr | None = None
    validator_media_bond_verifier_version: str = ""
    validator_media_minimum_bond_raw: int = 0
    validator_media_bond_sync_enabled: bool = False
    validator_media_bond_sync_seconds: int = 300
    validator_media_bond_rpc_timeout_seconds: int = 20
    # Bound reviewed reference-wallet reads per finalized snapshot. Core never
    # scans the registry-wide append-only worker history for this cache.
    validator_media_bond_max_workers: int = 10_000
    validator_media_minimum_quality_pass_rate: float = 0.95
    validator_media_max_output_bytes: int = 25 * 1024 * 1024
    validator_media_probe_timeout_seconds: int = 600
    # Bound economically inert preview work and its operational DB footprint.
    # A completed quorum must not immediately manufacture another free probe
    # group for the same worker/model.
    validator_text_group_min_interval_seconds: int = 3600
    validator_history_retention_days: int = 90
    validator_history_sweep_seconds: int = 21600
    # Read-only cohort watchdog. It reports aggregate operational drift only;
    # validator evidence remains economically inert.
    validator_cohort_monitor_enabled: bool = False
    validator_cohort_monitor_seconds: int = Field(default=300, ge=60, le=3600)
    validator_cohort_monitor_window_hours: int = Field(default=24, ge=1, le=720)
    validator_cohort_baseline_version: str = "v0.1.0-preview.13"
    # Seven-day advisory comparison. Schema and report tooling may be deployed
    # while false; no run can start and no observation can be written until the
    # three-independent-operator gate is separately frozen and this is enabled.
    validator_shadow_observer_enabled: bool = False
    validator_shadow_sample_seconds: int = Field(default=300, ge=60, le=3600)
    validator_shadow_retention_days: int = Field(default=90, ge=30, le=3650)
    # HMACs production job/stream identifiers before they enter the private
    # observer outbox and links reports to ledger jobs without exposing ids.
    # Keep stable through final-report archival; required only when enabled.
    validator_shadow_route_hmac_secret: SecretStr | None = None

    # Remote-MCP OAuth operational rows are bounded independently of the
    # feature flag so rollback does not leave attacker-created registration
    # state growing forever. Signed access tokens live at most 15 minutes;
    # authorization rows are retained for a full day for incident review.
    oauth_authorization_retention_seconds: int = Field(default=86400, ge=3600, le=604800)
    oauth_unused_client_retention_seconds: int = Field(default=86400, ge=3600, le=2592000)
    oauth_state_sweep_seconds: int = Field(default=21600, ge=300, le=86400)

    # Best-effort operator alerts. The webhook is a production secret and must
    # never be committed, logged, or returned by an API.
    grid_alert_discord_webhook: SecretStr | None = None
    grid_alert_queue_size: int = 256
    grid_alert_dedupe_seconds: int = 300

    @model_validator(mode="after")
    def validate_pairing_canary(self):
        until = self.validator_pairing_canary_until
        if self.validator_pairing_canary_accounts and until is None:
            raise ValueError("Validator pairing pilot requires an explicit expiry")
        if until is not None and until > datetime.now(UTC) + timedelta(hours=24):
            raise ValueError("Validator pairing pilot expiry must be within 24 hours")
        if self.validator_shadow_observer_enabled:
            secret = self.validator_shadow_route_hmac_secret
            if secret is None or len(secret.get_secret_value()) < 32:
                raise ValueError("Validator shadow collection requires a route HMAC secret of at least 32 characters")
        return self

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

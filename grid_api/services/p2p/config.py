# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""P2P configuration settings."""

import os
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass
class P2PConfig:
    """Configuration for P2P networking."""

    # Feature flag
    enabled: bool = False

    # Network identity
    # If not set, a new keypair is generated on each startup
    # For persistent identity, set to path of private key file
    private_key_path: str | None = None

    # Listen addresses
    listen_host: str = "0.0.0.0"
    listen_port: int = 4001

    # Bootstrap peers (multiaddrs)
    # Format: /ip4/x.x.x.x/tcp/4001/p2p/QmPeerId
    bootstrap_peers: list[str] = field(default_factory=list)

    # Gossipsub settings
    gossip_degree: int = 6  # Target mesh size
    gossip_degree_low: int = 4  # Minimum mesh size
    gossip_degree_high: int = 12  # Maximum mesh size
    gossip_history_length: int = 5  # Number of heartbeats to remember
    gossip_history_gossip: int = 3  # Number of heartbeats to gossip about

    # Topic prefixes
    topic_prefix: str = "/aipg/1"

    # Claim settings
    claim_timeout_seconds: float = 5.0  # Time to wait for claims before fallback
    job_ttl_seconds: int = 60  # Jobs expire after this time

    # DHT settings
    dht_enabled: bool = True
    dht_mode: str = "server"  # "server" or "client"

    # Relay settings (for NAT traversal)
    relay_enabled: bool = True
    relay_hop_enabled: bool = False  # Set True if this node should relay for others

    @classmethod
    def from_env(cls) -> "P2PConfig":
        """Load config from environment variables."""
        bootstrap_str = os.getenv("P2P_BOOTSTRAP_PEERS", "")
        bootstrap_peers = [p.strip() for p in bootstrap_str.split(",") if p.strip()]

        return cls(
            enabled=os.getenv("P2P_ENABLED", "false").lower() == "true",
            private_key_path=os.getenv("P2P_PRIVATE_KEY_PATH"),
            listen_host=os.getenv("P2P_LISTEN_HOST", "0.0.0.0"),
            listen_port=int(os.getenv("P2P_LISTEN_PORT", "4001")),
            bootstrap_peers=bootstrap_peers,
            gossip_degree=int(os.getenv("P2P_GOSSIP_DEGREE", "6")),
            gossip_degree_low=int(os.getenv("P2P_GOSSIP_DEGREE_LOW", "4")),
            gossip_degree_high=int(os.getenv("P2P_GOSSIP_DEGREE_HIGH", "12")),
            topic_prefix=os.getenv("P2P_TOPIC_PREFIX", "/aipg/1"),
            claim_timeout_seconds=float(os.getenv("P2P_CLAIM_TIMEOUT", "5.0")),
            job_ttl_seconds=int(os.getenv("P2P_JOB_TTL", "60")),
            dht_enabled=os.getenv("P2P_DHT_ENABLED", "true").lower() == "true",
            dht_mode=os.getenv("P2P_DHT_MODE", "server"),
            relay_enabled=os.getenv("P2P_RELAY_ENABLED", "true").lower() == "true",
            relay_hop_enabled=os.getenv("P2P_RELAY_HOP", "false").lower() == "true",
        )


@lru_cache
def get_p2p_config() -> P2PConfig:
    """Get the global P2P config (cached)."""
    return P2PConfig.from_env()

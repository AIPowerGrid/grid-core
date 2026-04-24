# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""libp2p node wrapper for AIPG.

Provides a high-level interface for P2P networking:
- Gossipsub for pub/sub messaging
- Kademlia DHT for peer discovery
- Automatic peer management

Note: This module uses trio for async. If the main app uses asyncio,
      use trio-asyncio bridge or run in separate thread.
"""

import json
import logging
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from typing import Any

from .config import P2PConfig, get_p2p_config
from .protocol import JobRequest, JobClaim, JobResult
from .topics import job_topic, claims_topic, results_topic, workers_topic

logger = logging.getLogger("grid_api.p2p.node")

# Type alias for message handlers
MessageHandler = Callable[[str, bytes], Awaitable[None]]


@dataclass
class P2PNode:
    """High-level wrapper around libp2p.

    This class manages:
    - libp2p host lifecycle
    - Gossipsub subscriptions
    - Message routing to handlers
    - Peer discovery via DHT
    """

    config: P2PConfig
    peer_id: str = ""
    running: bool = False

    # Internal state
    _host: Any = None
    _pubsub: Any = None
    _dht: Any = None
    _subscriptions: dict[str, Any] = field(default_factory=dict)
    _handlers: dict[str, list[MessageHandler]] = field(default_factory=dict)
    _known_workers: set[str] = field(default_factory=set)

    async def start(self) -> None:
        """Start the P2P node.

        This initializes:
        - libp2p host with TCP transport
        - Noise encryption
        - Yamux multiplexing
        - Gossipsub pubsub
        - Kademlia DHT (optional)
        """
        if self.running:
            return

        try:
            # Import here to avoid startup cost if P2P disabled
            from libp2p import new_host
            from libp2p.pubsub.gossipsub import GossipSub
            from libp2p.pubsub.pubsub import Pubsub
            from libp2p.peer.peerinfo import info_from_p2p_addr
            import multiaddr
        except ImportError as e:
            logger.error(f"libp2p not installed: {e}")
            logger.error("Install with: pip install libp2p")
            raise

        # Create host
        listen_addr = f"/ip4/{self.config.listen_host}/tcp/{self.config.listen_port}"
        self._host = new_host()

        # We need to use trio's context manager pattern
        # For now, store the host and manage lifecycle manually
        # In production, this would be integrated with trio nursery

        logger.info(f"Starting P2P node on {listen_addr}...")

        # Initialize gossipsub
        gs = GossipSub(
            degree=self.config.gossip_degree,
            degree_low=self.config.gossip_degree_low,
            degree_high=self.config.gossip_degree_high,
            time_to_live=self.config.gossip_history_length,
        )
        self._pubsub = Pubsub(self._host, gs)

        # Get our peer ID
        self.peer_id = self._host.get_id().to_string()
        self._known_workers.add(self.peer_id)

        # Connect to bootstrap peers
        for peer_addr in self.config.bootstrap_peers:
            try:
                maddr = multiaddr.Multiaddr(peer_addr)
                info = info_from_p2p_addr(maddr)
                await self._host.connect(info)
                logger.info(f"Connected to bootstrap peer: {info.peer_id}")
            except Exception as e:
                logger.warning(f"Failed to connect to {peer_addr}: {e}")

        self.running = True
        logger.info(f"P2P node started. Peer ID: {self.peer_id}")

    async def stop(self) -> None:
        """Stop the P2P node and cleanup resources."""
        if not self.running:
            return

        logger.info("Stopping P2P node...")

        # Unsubscribe from all topics
        for topic in list(self._subscriptions.keys()):
            await self.unsubscribe(topic)

        # Close host
        if self._host:
            # Note: actual cleanup depends on how host was started
            pass

        self.running = False
        logger.info("P2P node stopped")

    async def subscribe(self, topic: str, handler: MessageHandler | None = None) -> None:
        """Subscribe to a gossipsub topic.

        Args:
            topic: The topic string to subscribe to
            handler: Optional async callback(topic, data) for messages
        """
        if topic in self._subscriptions:
            if handler:
                self._handlers.setdefault(topic, []).append(handler)
            return

        logger.debug(f"Subscribing to topic: {topic}")
        sub = await self._pubsub.subscribe(topic)
        self._subscriptions[topic] = sub

        if handler:
            self._handlers.setdefault(topic, []).append(handler)

    async def unsubscribe(self, topic: str) -> None:
        """Unsubscribe from a gossipsub topic."""
        if topic not in self._subscriptions:
            return

        logger.debug(f"Unsubscribing from topic: {topic}")
        # Note: actual unsubscribe depends on libp2p API
        del self._subscriptions[topic]
        self._handlers.pop(topic, None)

    async def publish(self, topic: str, data: bytes) -> None:
        """Publish a message to a gossipsub topic.

        Args:
            topic: The topic to publish to
            data: The message bytes
        """
        if not self.running:
            raise RuntimeError("P2P node not running")

        await self._pubsub.publish(topic, data)
        logger.debug(f"Published {len(data)} bytes to {topic}")

    async def publish_job(self, job: JobRequest) -> None:
        """Publish a job request to the appropriate model topic."""
        topic = job_topic(job.model)
        await self.subscribe(topic)  # Ensure we're subscribed
        await self.publish(topic, job.to_json().encode())
        logger.info(f"Published job {job.id} for model {job.model}")

    async def publish_claim(self, claim: JobClaim) -> None:
        """Publish a job claim to the claims topic."""
        topic = claims_topic()
        await self.subscribe(topic)
        await self.publish(topic, claim.to_json().encode())
        logger.info(f"Published claim for job {claim.job_id}")

    async def publish_result(self, result: JobResult) -> None:
        """Publish a job result to the job's result topic."""
        topic = results_topic(result.job_id)
        await self.publish(topic, result.to_json().encode())

    async def subscribe_to_jobs(self, models: list[str], handler: MessageHandler) -> None:
        """Subscribe to job topics for the given models.

        Args:
            models: List of model names this worker supports
            handler: Callback for incoming jobs
        """
        for model in models:
            topic = job_topic(model)
            await self.subscribe(topic, handler)
            logger.info(f"Listening for jobs on {topic}")

    async def subscribe_to_claims(self, handler: MessageHandler) -> None:
        """Subscribe to the global claims topic."""
        topic = claims_topic()
        await self.subscribe(topic, handler)

    async def subscribe_to_results(self, job_id: str, handler: MessageHandler) -> None:
        """Subscribe to results for a specific job."""
        topic = results_topic(job_id)
        await self.subscribe(topic, handler)

    def add_known_worker(self, worker_id: str) -> None:
        """Add a worker to the known workers set."""
        self._known_workers.add(worker_id)

    def remove_known_worker(self, worker_id: str) -> None:
        """Remove a worker from the known workers set."""
        self._known_workers.discard(worker_id)

    def get_known_workers(self) -> list[str]:
        """Get list of known worker peer IDs."""
        return list(self._known_workers)

    async def run_message_loop(self) -> None:
        """Run the message processing loop.

        This should be run in a background task. It reads messages
        from all subscriptions and dispatches to handlers.
        """
        import trio

        while self.running:
            for topic, sub in list(self._subscriptions.items()):
                try:
                    # Try to get a message (non-blocking)
                    # Note: actual API depends on py-libp2p version
                    async for message in sub:
                        data = message.data
                        handlers = self._handlers.get(topic, [])
                        for handler in handlers:
                            try:
                                await handler(topic, data)
                            except Exception as e:
                                logger.error(f"Handler error for {topic}: {e}")
                        break  # Process one message per topic per loop
                except Exception as e:
                    logger.error(f"Error reading from {topic}: {e}")

            await trio.sleep(0.01)  # Small delay to prevent busy loop


# Global node instance
_p2p_node: P2PNode | None = None


def get_p2p_node() -> P2PNode | None:
    """Get the global P2P node instance."""
    return _p2p_node


async def init_p2p() -> P2PNode | None:
    """Initialize the P2P node on startup.

    Returns None if P2P is disabled.
    """
    global _p2p_node

    config = get_p2p_config()
    if not config.enabled:
        logger.info("P2P disabled (set P2P_ENABLED=true to enable)")
        return None

    _p2p_node = P2PNode(config=config)
    await _p2p_node.start()
    return _p2p_node


async def close_p2p() -> None:
    """Shutdown the P2P node."""
    global _p2p_node

    if _p2p_node:
        await _p2p_node.stop()
        _p2p_node = None

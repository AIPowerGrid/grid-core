# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""libp2p node for AIPG P2P networking.

Runs py-libp2p (trio-based) in a background thread, communicating with
the asyncio FastAPI app via thread-safe queues.

Architecture:
    ┌─────────────────────────────────────────┐
    │  FastAPI (asyncio)                      │
    │  ├── publish_job() ──► _outbox queue    │
    │  ├── _inbox queue ◄── on_message()      │
    │  └── _stream_inbox ◄── direct streams   │
    └─────────────────────────────────────────┘
                        │ thread-safe queues
    ┌─────────────────────────────────────────┐
    │  P2P Thread (trio)                      │
    │  ├── _run_trio_node()                   │
    │  ├── gossipsub pub/sub (jobs, claims)   │
    │  └── direct streams (result streaming)  │
    └─────────────────────────────────────────┘

Gossipsub is used for:
- Job broadcasts: /aipg/1/jobs/{model}
- Claim broadcasts: /aipg/1/claims

Direct streams are used for:
- Result streaming: worker opens stream to requester, streams tokens
"""

import asyncio
import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import P2PConfig, get_p2p_config

logger = logging.getLogger("grid_api.p2p.node")


@dataclass
class P2PMessage:
    """Message passed between asyncio and trio threads."""
    topic: str
    data: bytes
    from_peer: str = ""


@dataclass
class StreamMessage:
    """Message received on a direct stream."""
    job_id: str
    data: bytes
    from_peer: str = ""
    is_done: bool = False


@dataclass
class P2PNode:
    """P2P node wrapper that bridges asyncio (FastAPI) and trio (libp2p).

    The node runs in a background thread with its own trio event loop.
    Communication happens via thread-safe queues.
    """

    config: P2PConfig
    peer_id: str = ""
    running: bool = False

    # Thread-safe communication
    _inbox: queue.Queue = field(default_factory=queue.Queue)  # trio -> asyncio (gossipsub)
    _outbox: queue.Queue = field(default_factory=queue.Queue)  # asyncio -> trio
    _commands: queue.Queue = field(default_factory=queue.Queue)  # control commands
    _stream_inbox: dict[str, queue.Queue] = field(default_factory=dict)  # job_id -> stream msgs

    # Internal state
    _thread: threading.Thread | None = None
    _subscribed_topics: set[str] = field(default_factory=set)
    _known_workers: set[str] = field(default_factory=set)
    _handlers: dict[str, list[Callable]] = field(default_factory=dict)
    _pending_streams: dict[str, Any] = field(default_factory=dict)  # job_id -> stream
    _host: Any = None  # libp2p host reference

    def start(self) -> None:
        """Start the P2P node in a background thread."""
        if self.running:
            return

        self._thread = threading.Thread(
            target=self._run_trio_thread,
            name="p2p-libp2p",
            daemon=True,
        )
        self._thread.start()

        # Wait for node to be ready (peer_id set)
        deadline = 30  # seconds
        import time
        start = time.time()
        while not self.peer_id and (time.time() - start) < deadline:
            time.sleep(0.1)

        if not self.peer_id:
            raise RuntimeError("P2P node failed to start within timeout")

        self.running = True
        logger.info(f"P2P node started. Peer ID: {self.peer_id}")

    def stop(self) -> None:
        """Stop the P2P node."""
        if not self.running:
            return

        logger.info("Stopping P2P node...")
        self._commands.put(("stop", None))

        if self._thread:
            self._thread.join(timeout=5)

        self.running = False
        logger.info("P2P node stopped")

    def _run_trio_thread(self) -> None:
        """Entry point for the trio background thread."""
        import trio
        trio.run(self._run_trio_node)

    async def _run_trio_node(self) -> None:
        """Main trio coroutine running the libp2p node."""
        import trio
        from libp2p import new_host
        from libp2p.crypto.secp256k1 import create_new_key_pair
        from libp2p.peer.peerinfo import info_from_p2p_addr
        from libp2p.pubsub.gossipsub import GossipSub
        from libp2p.pubsub.pubsub import Pubsub
        from libp2p.stream_muxer.mplex.mplex import MPLEX_PROTOCOL_ID, Mplex
        from libp2p.tools.anyio_service import background_trio_service
        from libp2p.custom_types import TProtocol
        from libp2p.network.stream.net_stream_interface import INetStream
        import multiaddr

        from .protocol import RESULT_STREAM_PROTOCOL

        # Generate or load key pair
        # TODO: Load from config.private_key_path if set
        key_pair = create_new_key_pair()

        # Create host
        host = new_host(
            key_pair=key_pair,
            muxer_opt={MPLEX_PROTOCOL_ID: Mplex},
        )

        # Create gossipsub
        gossipsub = GossipSub(
            protocols=[TProtocol("/meshsub/1.0.0")],
            degree=self.config.gossip_degree,
            degree_low=self.config.gossip_degree_low,
            degree_high=self.config.gossip_degree_high,
            time_to_live=self.config.gossip_history_length,
            heartbeat_interval=5,
        )
        pubsub = Pubsub(host, gossipsub)

        # Track subscriptions
        subscriptions: dict[str, Any] = {}

        # Listen addresses
        listen_port = self.config.listen_port
        listen_addrs = [f"/ip4/0.0.0.0/tcp/{listen_port}"]

        async with host.run(listen_addrs=listen_addrs), trio.open_nursery() as nursery:
            # Start peerstore cleanup
            nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)

            async with background_trio_service(pubsub):
                async with background_trio_service(gossipsub):
                    await pubsub.wait_until_ready()

                    # Set peer ID (signals ready to main thread)
                    self.peer_id = host.get_id().to_string()
                    self._known_workers.add(self.peer_id)

                    # Register handler for incoming result streams
                    async def stream_handler(stream: INetStream) -> None:
                        await self._handle_incoming_stream(stream, nursery)

                    host.set_stream_handler(
                        TProtocol(RESULT_STREAM_PROTOCOL), stream_handler
                    )

                    # Store host reference for opening outgoing streams
                    self._host = host

                    logger.info(f"P2P node ready on port {listen_port}")
                    logger.info(f"Peer ID: {self.peer_id}")
                    logger.info(f"Result stream handler registered: {RESULT_STREAM_PROTOCOL}")

                    # Connect to bootstrap peers
                    for peer_addr in self.config.bootstrap_peers:
                        try:
                            maddr = multiaddr.Multiaddr(peer_addr)
                            info = info_from_p2p_addr(maddr)
                            await host.connect(info)
                            logger.info(f"Connected to bootstrap peer: {info.peer_id}")
                        except Exception as e:
                            logger.warning(f"Failed to connect to {peer_addr}: {e}")

                    # Start background tasks
                    nursery.start_soon(
                        self._outbox_processor, pubsub, subscriptions, nursery
                    )
                    nursery.start_soon(
                        self._command_processor, nursery
                    )

                    # Run until stopped
                    while True:
                        try:
                            # Check for stop command
                            cmd, _ = self._commands.get_nowait()
                            if cmd == "stop":
                                logger.info("Received stop command")
                                nursery.cancel_scope.cancel()
                                break
                        except:
                            pass
                        await trio.sleep(0.1)

    async def _outbox_processor(
        self, pubsub: Any, subscriptions: dict, nursery: Any
    ) -> None:
        """Process outgoing messages from asyncio thread."""
        import trio

        while True:
            try:
                # Non-blocking check for outgoing messages
                try:
                    msg = self._outbox.get_nowait()
                except:
                    await trio.sleep(0.05)
                    continue

                action = msg.get("action")

                if action == "subscribe":
                    topic = msg["topic"]
                    if topic not in subscriptions:
                        sub = await pubsub.subscribe(topic)
                        subscriptions[topic] = sub
                        self._subscribed_topics.add(topic)
                        # Start receive loop for this topic
                        nursery.start_soon(
                            self._receive_loop, topic, sub
                        )
                        logger.debug(f"Subscribed to {topic}")

                elif action == "unsubscribe":
                    topic = msg["topic"]
                    if topic in subscriptions:
                        # Note: py-libp2p doesn't have explicit unsubscribe
                        del subscriptions[topic]
                        self._subscribed_topics.discard(topic)
                        logger.debug(f"Unsubscribed from {topic}")

                elif action == "publish":
                    topic = msg["topic"]
                    data = msg["data"]
                    # Auto-subscribe if not already
                    if topic not in subscriptions:
                        sub = await pubsub.subscribe(topic)
                        subscriptions[topic] = sub
                        self._subscribed_topics.add(topic)
                        nursery.start_soon(self._receive_loop, topic, sub)
                    await pubsub.publish(topic, data)
                    logger.debug(f"Published {len(data)} bytes to {topic}")

            except Exception as e:
                logger.error(f"Outbox processor error: {e}")
                await trio.sleep(0.1)

    async def _receive_loop(self, topic: str, subscription: Any) -> None:
        """Receive messages from a topic subscription."""
        import trio
        from libp2p.peer.id import ID

        logger.debug(f"Starting receive loop for {topic}")

        while topic in self._subscribed_topics:
            try:
                # Get next message (blocking within trio)
                message = await subscription.get()

                from_peer = ID(message.from_id).to_base58()
                data = message.data

                # Skip our own messages
                if from_peer == self.peer_id:
                    continue

                # Put in inbox for asyncio thread
                self._inbox.put(P2PMessage(
                    topic=topic,
                    data=data,
                    from_peer=from_peer,
                ))

                # Track worker if from claims topic
                if "/claims" in topic:
                    self._known_workers.add(from_peer)

                logger.debug(f"Received {len(data)} bytes on {topic} from {from_peer[:8]}")

            except Exception as e:
                if "cancelled" in str(e).lower():
                    break
                logger.error(f"Receive loop error on {topic}: {e}")
                await trio.sleep(0.1)

    async def _command_processor(self, nursery: Any) -> None:
        """Process control commands."""
        import trio

        while True:
            try:
                cmd, data = self._commands.get_nowait()
                if cmd == "stop":
                    nursery.cancel_scope.cancel()
                    return
            except:
                pass
            await trio.sleep(0.1)

    async def _handle_incoming_stream(self, stream: Any, nursery: Any) -> None:
        """Handle an incoming result stream from a worker.

        Protocol:
        1. Worker sends job_id (first line, newline-terminated)
        2. Worker sends result messages (JSON lines)
        3. Worker closes stream when done
        """
        import trio
        from libp2p.peer.id import ID

        try:
            remote_peer = stream.muxed_conn.peer_id
            remote_peer_str = ID(remote_peer).to_base58() if hasattr(remote_peer, 'to_bytes') else str(remote_peer)
            logger.debug(f"Incoming result stream from {remote_peer_str[:8]}")

            # Read job_id (first line)
            job_id_bytes = b""
            while True:
                chunk = await stream.read(1)
                if not chunk or chunk == b"\n":
                    break
                job_id_bytes += chunk

            job_id = job_id_bytes.decode().strip()
            if not job_id:
                logger.warning("Stream missing job_id, closing")
                await stream.close()
                return

            logger.debug(f"Result stream for job {job_id[:8]}")

            # Create queue for this job if not exists
            if job_id not in self._stream_inbox:
                self._stream_inbox[job_id] = queue.Queue()

            job_queue = self._stream_inbox[job_id]

            # Read result messages until stream closes
            buffer = b""
            while True:
                try:
                    chunk = await stream.read(4096)
                    if not chunk:
                        # Stream closed
                        job_queue.put(StreamMessage(
                            job_id=job_id,
                            data=b"",
                            from_peer=remote_peer_str,
                            is_done=True,
                        ))
                        break

                    buffer += chunk

                    # Process complete lines
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line:
                            job_queue.put(StreamMessage(
                                job_id=job_id,
                                data=line,
                                from_peer=remote_peer_str,
                                is_done=False,
                            ))

                except Exception as e:
                    logger.error(f"Error reading stream for {job_id[:8]}: {e}")
                    break

            await stream.close()
            logger.debug(f"Result stream closed for job {job_id[:8]}")

        except Exception as e:
            logger.error(f"Error handling incoming stream: {e}")
            try:
                await stream.close()
            except:
                pass

    # ── Public API (called from asyncio) ──

    async def subscribe(self, topic: str) -> None:
        """Subscribe to a gossipsub topic."""
        self._outbox.put({"action": "subscribe", "topic": topic})
        # Wait briefly for subscription to be established
        await asyncio.sleep(0.1)

    async def unsubscribe(self, topic: str) -> None:
        """Unsubscribe from a gossipsub topic."""
        self._outbox.put({"action": "unsubscribe", "topic": topic})

    async def publish(self, topic: str, data: bytes) -> None:
        """Publish a message to a gossipsub topic."""
        if not self.running:
            raise RuntimeError("P2P node not running")
        self._outbox.put({"action": "publish", "topic": topic, "data": data})

    async def get_message(self, timeout: float = 1.0) -> P2PMessage | None:
        """Get next incoming message (non-blocking for asyncio)."""
        try:
            return await asyncio.to_thread(
                self._inbox.get, timeout=timeout
            )
        except:
            return None

    def get_known_workers(self) -> list[str]:
        """Get list of known worker peer IDs."""
        return list(self._known_workers)

    def add_known_worker(self, worker_id: str) -> None:
        """Add a worker to the known set."""
        self._known_workers.add(worker_id)

    # ── Direct Stream API ──

    def register_job_stream(self, job_id: str) -> None:
        """Register to receive stream messages for a job.

        Call this before broadcasting the job so the queue exists
        when the worker connects.
        """
        if job_id not in self._stream_inbox:
            self._stream_inbox[job_id] = queue.Queue()
            logger.debug(f"Registered stream inbox for job {job_id[:8]}")

    async def get_stream_message(
        self, job_id: str, timeout: float = 1.0
    ) -> StreamMessage | None:
        """Get next stream message for a job (non-blocking for asyncio)."""
        job_queue = self._stream_inbox.get(job_id)
        if not job_queue:
            return None

        try:
            return await asyncio.to_thread(
                job_queue.get, timeout=timeout
            )
        except:
            return None

    def cleanup_job_stream(self, job_id: str) -> None:
        """Remove the stream inbox for a job."""
        self._stream_inbox.pop(job_id, None)
        logger.debug(f"Cleaned up stream inbox for job {job_id[:8]}")


# ── Global singleton ──

_p2p_node: P2PNode | None = None


def get_p2p_node() -> P2PNode | None:
    """Get the global P2P node instance."""
    return _p2p_node


async def init_p2p() -> P2PNode | None:
    """Initialize the P2P node on startup."""
    global _p2p_node

    config = get_p2p_config()
    if not config.enabled:
        logger.info("P2P disabled (set P2P_ENABLED=true to enable)")
        return None

    _p2p_node = P2PNode(config=config)
    _p2p_node.start()  # Synchronous - starts thread and waits for ready
    return _p2p_node


async def close_p2p() -> None:
    """Shutdown the P2P node."""
    global _p2p_node

    if _p2p_node:
        _p2p_node.stop()
        _p2p_node = None

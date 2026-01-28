"""
Shinox Agent SDK - Base Agent Class

The ShinoxAgent class provides core infrastructure for building agents
in the Shinox mesh network.
"""

import asyncio
import os
import json
import logging
import httpx
from typing import Callable, Optional, Any
from faststream import FastStream
from faststream.kafka import KafkaBroker

from .schemas import AgentMessage, A2AHeaders, SystemCommand

# Try to import AgentCard from a2a.types, fall back to Any
try:
    from a2a.types import AgentCard
except ImportError:
    AgentCard = Any  # type: ignore

logger = logging.getLogger(__name__)

# Heartbeat configuration
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("AGENT_HEARTBEAT_INTERVAL", "10"))


class ShinoxAgent:
    """
    Base Shinox Agent with core infrastructure.

    Provides:
    - Kafka broker setup with proper consumer groups
    - Registry registration/deregistration
    - Inbox subscription for JOIN_SESSION commands
    - Dynamic session topic subscription
    - Message parsing and routing

    Usage:
        from shinox_agent import ShinoxAgent

        async def my_handler(msg, agent):
            # Handle session messages
            print(f"Received: {msg.content}")
            await agent.publish_message(
                content="Response",
                conversation_id=msg.headers.conversation_id,
                interaction_type="TASK_RESULT"
            )

        agent = ShinoxAgent(
            agent_card=my_card,
            session_handler=my_handler,
            triggers=["keyword1", "keyword2"],
        )
        app = agent.app

    For simpler workers, use ShinoxWorkerAgent instead.
    """

    def __init__(
        self,
        agent_card: AgentCard,
        session_handler: Optional[Callable] = None,
        triggers: Optional[list[str]] = None,
        agent_url: Optional[str] = None,
    ):
        """
        Initialize the Shinox Agent.

        Args:
            agent_card: A2A AgentCard defining the agent's identity and capabilities.
            session_handler: Async callback for session messages.
                             Signature: async def handler(msg: AgentMessage, agent: ShinoxAgent)
            triggers: List of keywords that trigger the agent (for custom handlers).
            agent_url: URL where the agent is accessible (for registry).
                       Defaults to AGENT_URL env var or http://localhost:8001.
        """
        # --- Configuration ---
        self.broker_url = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
        self.registry_url = os.getenv("AGENT_REGISTRY_URL", "http://localhost:9000")
        self.agent_url = agent_url or os.getenv("AGENT_URL", "http://localhost:8001")

        self.agent_card = agent_card
        self.active_sessions: set[str] = set()
        self.session_handler = session_handler
        self.triggers = triggers or []

        # --- Identity ---
        self.agent_id = self._generate_agent_id()
        self.self_introduction = self._generate_introduction()

        # --- Heartbeat Task ---
        self._heartbeat_task: Optional[asyncio.Task] = None

        # --- Broker Setup ---
        self.broker = KafkaBroker(self.broker_url)
        self.app = FastStream(self.broker)

        # --- Lifecycle Hooks ---
        self.app.on_startup(self._startup)
        self.app.on_shutdown(self._shutdown)

        # --- Inbox Subscription ---
        inbox_topic = f"mesh.agent.{self.agent_id}.inbox"
        logger.info(f"[{self.agent_id}] Subscribing to inbox: {inbox_topic}")
        self.broker.subscriber(
            inbox_topic,
            group_id=f"{self.agent_id}-inbox-consumer",
            auto_offset_reset="earliest",
        )(self._inbox_handler)

    def _generate_agent_id(self) -> str:
        """Generate agent ID from card name."""
        return self.agent_card.name.lower().replace(" ", "-")

    def _generate_introduction(self) -> str:
        """Generate self-introduction from card."""
        capabilities = set()
        for skill in self.agent_card.skills:
            capabilities.update(skill.tags)

        return f"""id: "{self.agent_id}"
role: "{self.agent_card.name}"
capabilities: {list(capabilities)}
triggers: {self.triggers}
description: "{self.agent_card.description}"
"""

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    async def _startup(self):
        """Internal startup hook."""
        logger.info(f"[{self.agent_id}] Starting up...")
        await self.register_with_registry()

        # Start heartbeat background task
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"[{self.agent_id}] Heartbeat started (interval: {HEARTBEAT_INTERVAL_SECONDS}s)")

        logger.info(f"[{self.agent_id}] Ready and listening for messages")

    async def _shutdown(self):
        """Internal shutdown hook."""
        logger.info(f"[{self.agent_id}] Shutting down...")

        # Cancel heartbeat task
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            logger.info(f"[{self.agent_id}] Heartbeat stopped")

        await self.deregister_from_registry()

    async def register_with_registry(self):
        """Register this agent with the central registry."""
        url = f"{self.registry_url}/register"

        try:
            card_data = self.agent_card.model_dump(mode='json')
        except AttributeError:
            card_data = self.agent_card.dict()

        payload = {
            "agent_id": self.agent_id,
            "agent_url": self.agent_url,
            "card": card_data,
            "metadata": {"inbox_topic": f"mesh.agent.{self.agent_id}.inbox"}
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    print(f"[{self.agent_id}] Registered with registry")
                else:
                    print(f"[{self.agent_id}] Registration failed: {resp.status_code}")
        except Exception as e:
            print(f"[{self.agent_id}] Registration error: {e}")

    async def deregister_from_registry(self):
        """Update registry status to offline."""
        url = f"{self.registry_url}/agent/{self.agent_id}/health"

        try:
            async with httpx.AsyncClient() as client:
                await client.post(url, params={"status": "offline"})
        except Exception as e:
            logger.warning(f"[{self.agent_id}] Deregistration error: {e}")

    async def _heartbeat_loop(self):
        """Background task that sends periodic heartbeats to the registry."""
        url = f"{self.registry_url}/agent/{self.agent_id}/health"

        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

                async with httpx.AsyncClient() as client:
                    resp = await client.post(url, params={"status": "active"}, timeout=5.0)
                    if resp.status_code == 200:
                        logger.debug(f"[{self.agent_id}] Heartbeat sent")
                    else:
                        logger.warning(f"[{self.agent_id}] Heartbeat failed: {resp.status_code}")

            except asyncio.CancelledError:
                logger.debug(f"[{self.agent_id}] Heartbeat loop cancelled")
                raise
            except Exception as e:
                logger.warning(f"[{self.agent_id}] Heartbeat error: {e}")

    # =========================================================================
    # MESSAGE HANDLING
    # =========================================================================

    async def _inbox_handler(self, msg):
        """Handle inbox messages (JOIN_SESSION commands and direct messages)."""
        logger.info(f"[{self.agent_id}] Inbox message received")

        # Parse raw message
        parsed = self._parse_message(msg)
        if parsed is None:
            return

        # Handle SystemCommand
        if isinstance(parsed, dict) and parsed.get("type") == "SYSTEM_COMMAND":
            try:
                cmd = SystemCommand(**parsed)
                await self._handle_system_command(cmd)
            except Exception as e:
                logger.error(f"[{self.agent_id}] Failed to handle SystemCommand: {e}")
            return

        # Handle AgentMessage (task assignment to inbox)
        try:
            agent_msg = AgentMessage(**parsed) if isinstance(parsed, dict) else parsed
            await self._handle_inbox_message(agent_msg)
        except Exception as e:
            logger.error(f"[{self.agent_id}] Failed to handle inbox message: {e}")

    async def _handle_system_command(self, cmd: SystemCommand):
        """Handle system commands like JOIN_SESSION."""
        if cmd.metadata.command == "JOIN_SESSION":
            session_id = cmd.metadata.session_id
            logger.info(f"[{self.agent_id}] JOIN_SESSION: {session_id}")

            if session_id not in self.active_sessions:
                self.active_sessions.add(session_id)
                await self._subscribe_to_session(session_id)
                print(f"[{self.agent_id}] Joined session: {session_id}")

            # Send acknowledgment
            await self._send_join_acknowledgment(session_id)

    async def _handle_inbox_message(self, msg: AgentMessage):
        """Handle direct messages to inbox (e.g., task assignments)."""
        if self.session_handler:
            await self.session_handler(msg, self)

    async def _subscribe_to_session(self, session_id: str):
        """Dynamically subscribe to a session topic."""
        try:
            subscriber = self.broker.subscriber(
                session_id,
                group_id=f"{self.agent_id}-session-consumer",
                auto_offset_reset="earliest",
            )
            subscriber(self._session_message_handler)
            await subscriber.start()
            logger.info(f"[{self.agent_id}] Subscribed to session: {session_id}")
        except Exception as e:
            logger.error(f"[{self.agent_id}] Failed to subscribe to {session_id}: {e}")

    async def _session_message_handler(self, msg):
        """Handle messages from session topics."""
        parsed = self._parse_message(msg)
        if parsed is None:
            return

        try:
            agent_msg = AgentMessage(**parsed) if isinstance(parsed, dict) else parsed

            if self.session_handler:
                await self.session_handler(agent_msg, self)
            else:
                logger.warning(f"[{self.agent_id}] No session handler defined")

        except Exception as e:
            logger.error(f"[{self.agent_id}] Session message error: {e}")

    async def _send_join_acknowledgment(self, session_id: str):
        """Send acknowledgment that agent joined the session."""
        msg = AgentMessage(
            content=f"Agent {self.agent_id} has joined session {session_id}\n\nSelf Introduction:\n{self.self_introduction}",
            headers=A2AHeaders(
                source_agent_id=self.agent_id,
                interaction_type="AGENT_JOINED",
                conversation_id=session_id,
                governance_status="PENDING"
            )
        )

        await self.broker.publish(
            msg,
            topic="mesh.responses.pending",
            headers={
                "x-source-agent": self.agent_id,
                "x-dest-topic": f"{session_id},mesh.global.events",
                "x-interaction-type": "AGENT_JOINED",
                "x-conversation-id": session_id
            }
        )

    def _parse_message(self, msg) -> Optional[dict]:
        """Parse raw Kafka message to dict."""
        if isinstance(msg, dict):
            return msg

        if isinstance(msg, (str, bytes)):
            try:
                raw = msg if isinstance(msg, str) else msg.decode('utf-8')
                return json.loads(raw)
            except json.JSONDecodeError as e:
                logger.error(f"[{self.agent_id}] JSON parse error: {e}")
                return None

        # Already a Pydantic model
        if hasattr(msg, 'model_dump'):
            return msg.model_dump()
        if hasattr(msg, 'dict'):
            return msg.dict()

        return None

    # =========================================================================
    # HELPERS
    # =========================================================================

    async def resolve_agent_inbox(self, agent_id: str) -> str:
        """Resolve inbox topic for an agent via registry."""
        url = f"{self.registry_url}/agent/{agent_id}"
        default = f"mesh.agent.{agent_id}.inbox"

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("metadata", {}).get("inbox_topic", default)
        except Exception as e:
            logger.warning(f"[{self.agent_id}] Agent resolution error: {e}")

        return default

    async def fetch_available_agents(self, exclude_self: bool = True) -> list[str]:
        """Fetch all active agents from registry."""
        url = f"{self.registry_url}/discover?status=active"
        agents = []

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    for agent in data:
                        aid = agent.get("agent_id")
                        if exclude_self and aid == self.agent_id:
                            continue
                        desc = agent.get("card", {}).get("description", "No description")
                        agents.append(f"{aid}: {desc}")
        except Exception as e:
            logger.warning(f"[{self.agent_id}] Agent discovery error: {e}")

        return agents

    async def publish_message(
        self,
        content: str,
        conversation_id: str,
        interaction_type: str,
        target_agent_id: Optional[str] = None,
        route_through_governance: bool = True,
    ):
        """
        Publish a message to the mesh.

        Args:
            content: Message content
            conversation_id: Session/topic ID
            interaction_type: Type of interaction (TASK_RESULT, INFO_UPDATE, etc.)
            target_agent_id: Optional target agent
            route_through_governance: If True, send via mesh.responses.pending
        """
        msg = AgentMessage(
            content=content,
            headers=A2AHeaders(
                source_agent_id=self.agent_id,
                target_agent_id=target_agent_id,
                interaction_type=interaction_type,
                conversation_id=conversation_id,
                governance_status="PENDING" if route_through_governance else "VERIFIED"
            )
        )

        topic = "mesh.responses.pending" if route_through_governance else conversation_id

        await self.broker.publish(
            msg,
            topic=topic,
            headers={
                "x-source-agent": self.agent_id,
                "x-dest-topic": conversation_id,
                "x-interaction-type": interaction_type,
                "x-conversation-id": conversation_id
            }
        )

    # Alias for backward compatibility
    async def publish_update(self, content: str, conversation_id: str, interaction_type: str):
        """Publish an update message (alias for publish_message)."""
        await self.publish_message(content, conversation_id, interaction_type)

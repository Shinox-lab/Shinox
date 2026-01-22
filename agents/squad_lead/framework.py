import os
import json
import logging
import httpx
from faststream import FastStream
from faststream.kafka import KafkaBroker
from schemas import AgentMessage, A2AHeaders, SystemCommand
from a2a.types import AgentCard

logger = logging.getLogger(__name__)

class ShinoxAgent:
    def __init__(self, agent_card: AgentCard, session_handler, triggers: list[str] = None):
        """
        Initializes the ShinoxAgent framework.
        
        Args:
            agent_card (AgentCard): The definition of the agent.
            session_handler (Callable): Async function to handle session messages. 
                                        Signature: async def handler(msg: AgentMessage, agent: ShinoxAgent)
            triggers (list[str]): List of keywords that trigger the agent.
        """
        # --- Configuration ---
        self.broker_url = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
        self.registry_url = os.getenv("AGENT_REGISTRY_URL", "http://localhost:9000")
        
        self.agent_card = agent_card
        self.active_sessions = set()
        self.session_handler = session_handler
        self.triggers = triggers or []

        # --- Identity ---
        self.agent_id = self._generate_agent_id()
        self.self_introduction = self._generate_introduction()

        # --- Broker Setup ---
        # Use agent_id as consumer group for consistent message delivery
        self.broker = KafkaBroker(
            self.broker_url,
            # Disable auto_offset_reset='latest' warning by being explicit
            # This ensures we read from the earliest available message if no offset exists
        )
        self.app = FastStream(self.broker)

        # --- Lifecycle Hooks ---
        self.app.on_startup(self.startup)
        self.app.on_shutdown(self.shutdown)

        # --- Subscriptions ---
        # Use group_id based on agent_id for stable consumer group
        # auto_offset_reset='earliest' ensures we don't miss messages
        inbox_topic = f"mesh.agent.{self.agent_id}.inbox"
        logger.info(f"[{self.agent_id}] Subscribing to inbox topic: {inbox_topic}")
        self.broker.subscriber(
            inbox_topic,
            group_id=f"{self.agent_id}-inbox-consumer",
            auto_offset_reset="earliest",
        )(self.inbox_handler)

    def _generate_agent_id(self) -> str:
        """Generates a unique agent ID based on the agent card name and a random suffix."""
        base_id = self.agent_card.name.lower().replace(" ", "-")
        return f"{base_id}"

    def _generate_introduction(self) -> str:
        capabilities = set()
        for skill in self.agent_card.skills:
            capabilities.update(skill.tags)
            
        return f"""
id: "{self.agent_id}"
role: "{self.agent_card.name}"
capabilities: {list(capabilities)}
triggers: {self.triggers}
description: "{self.agent_card.description}"
"""

    # =============================================================================
    # LIFECYCLE HANDLERS
    # =============================================================================

    async def startup(self):
        """Initialize agent and register with registry."""
        logger.info(f"[{self.agent_id}] Agent starting up...")
        logger.info(f"[{self.agent_id}] Broker URL: {self.broker_url}")
        logger.info(f"[{self.agent_id}] Inbox topic: mesh.agent.{self.agent_id}.inbox")
        await self.register_with_registry()
        logger.info(f"[{self.agent_id}] Agent startup complete, listening for messages...")

    async def shutdown(self):
        """Cleanup module and update registry."""
        await self.shutdown_registry_update()

    async def register_with_registry(self):
        """Register this agent with the central registry on startup."""
        registry_url = f"{self.registry_url}/register"

        try:
            card_data = self.agent_card.model_dump(mode='json')
        except AttributeError:
            card_data = self.agent_card.dict()

        payload = {
            "agent_id": self.agent_id,
            "agent_url": "http://localhost:8001",
            "card": card_data,
            "metadata": {"inbox_topic": f"mesh.agent.{self.agent_id}.inbox"}
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(registry_url, json=payload)
                if resp.status_code == 200:
                    print(f"Registered {self.agent_id}")
                else:
                    print(f"Registration failed: {resp.status_code}")
                    
        except Exception as e:
            print(f"RegistrationError: {e}")

    async def shutdown_registry_update(self):
        """Update registry status on shutdown."""
        registry_url = f"{self.registry_url}/agent/{self.agent_id}/health"

        try:
            async with httpx.AsyncClient() as client:
                await client.post(registry_url, params={"status": "offline"})

        except Exception as e:
            print(f"RegistryUpdateError: {e}")

    # =============================================================================
    # HELPER FUNCTIONS
    # =============================================================================

    async def resolve_agent_inbox(self, agent_id: str) -> str:
        """Resolve the inbox topic for an agent using the Registry."""
        registry_url = f"{self.registry_url}/agent/{agent_id}"
        default_topic = f"mesh.agent.{agent_id}.inbox"

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(registry_url)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("metadata", {}).get("inbox_topic", default_topic)
        except Exception as e:
            print(f"AgentResolutionError: {e}")
        
        return default_topic

    async def fetch_available_agents(self, exclude_self: bool = True) -> list[str]:
        """Fetch all active agents from the registry."""
        discover_url = f"{self.registry_url}/discover?status=active"
        formatted_agents = []

        try:
            async with httpx.AsyncClient() as client:
                 resp = await client.get(discover_url)
                 if resp.status_code == 200:
                     agents = resp.json()
                     for agent in agents:
                         aid = agent.get("agent_id")
                         if exclude_self and aid == self.agent_id:
                            continue
                         description = agent.get("card", {}).get("description", "No description")
                         formatted_agents.append(f"{aid}: {description}")

        except Exception as e:
            print(f"AgentDiscoveryError: {e}")

        return formatted_agents

    # =============================================================================
    # MESSAGE HANDLERS
    # =============================================================================

    async def inbox_handler(self, msg):
        """Listen for direct system commands (e.g. Join Session)."""
        logger.info(f"[{self.agent_id}] Inbox received message: {type(msg)}")

        # Handle raw dict/string messages from Kafka
        if isinstance(msg, (str, bytes)):
            try:
                msg = json.loads(msg) if isinstance(msg, str) else json.loads(msg.decode('utf-8'))
            except json.JSONDecodeError as e:
                logger.error(f"[{self.agent_id}] Failed to parse inbox message: {e}")
                return

        # Parse to SystemCommand if it's a dict
        if isinstance(msg, dict):
            try:
                msg = SystemCommand(**msg)
            except Exception as e:
                logger.error(f"[{self.agent_id}] Failed to validate SystemCommand: {e}")
                logger.debug(f"[{self.agent_id}] Raw message: {msg}")
                return

        logger.info(f"[{self.agent_id}] Processing command: {msg.metadata.command}")

        if msg.metadata.command == "JOIN_SESSION":
            session_id = msg.metadata.session_id
            logger.info(f"[{self.agent_id}] JOIN_SESSION request for: {session_id}")

            if session_id not in self.active_sessions:
                self.active_sessions.add(session_id)
                # Subscribe to the session topic with stable consumer group
                logger.info(f"[{self.agent_id}] Subscribing to session topic: {session_id}")
                print(f"[{self.agent_id}] Creating subscriber for topic: {session_id}")

                try:
                    # Create subscriber for the session topic
                    subscriber = self.broker.subscriber(
                        session_id,
                        group_id=f"{self.agent_id}-session-consumer",
                        auto_offset_reset="earliest",
                    )
                    print(f"[{self.agent_id}] Subscriber created: {subscriber}")

                    # Register the handler
                    subscriber(self._wrapped_session_handler)
                    print(f"[{self.agent_id}] Handler registered")

                    # IMPORTANT: Start the subscriber since broker is already running
                    await subscriber.start()
                    print(f"[{self.agent_id}] Subscriber started successfully!")

                    logger.info(f"[{self.agent_id}] Joined session and started listening: {session_id}")
                    print(f"Joined session: {session_id}")

                except Exception as e:
                    logger.error(f"[{self.agent_id}] Failed to start session subscriber: {e}")
                    print(f"[{self.agent_id}] ERROR starting subscriber: {e}")
                    import traceback
                    traceback.print_exc()

            # Send join acknowledgment
            join_ack_headers = {
                "source_agent_id": self.agent_id,
                "interaction_type": "AGENT_JOINED",
                "conversation_id": msg.metadata.session_id,
                "governance_status": "PENDING"
            }

            join_ack_msg = AgentMessage(
                content=f"Agent {self.agent_id} has joined session {msg.metadata.session_id}\nSelf Introduction: {self.self_introduction}",
                headers=A2AHeaders(**join_ack_headers)
            )

            # Send through pending buffer (for governance routing)
            await self.broker.publish(
                join_ack_msg,
                topic="mesh.responses.pending",
                headers={
                    "x-source-agent": self.agent_id,
                    "x-dest-topic": f"{msg.metadata.session_id}",
                    "x-interaction-type": "AGENT_JOINED",
                    "x-conversation-id": msg.metadata.session_id
                }
            )
            logger.info(f"[{self.agent_id}] Sent join acknowledgment for {session_id}")

    async def _wrapped_session_handler(self, msg):
        """
        Internal wrapper to inject 'self' into the user-provided handler.
        This allows the handler to use agent methods like publish_update.
        """
        print(f"[{self.agent_id}] >>> Session message received: {type(msg)}")
        logger.info(f"[{self.agent_id}] Session message received: {type(msg)}")

        # Handle raw dict/string messages from Kafka
        if isinstance(msg, (str, bytes)):
            try:
                raw_str = msg if isinstance(msg, str) else msg.decode('utf-8')
                print(f"[{self.agent_id}] Raw message (first 200 chars): {raw_str[:200]}")
                msg = json.loads(raw_str)
            except json.JSONDecodeError as e:
                logger.error(f"[{self.agent_id}] Failed to parse session message: {e}")
                return

        # Parse to AgentMessage if it's a dict
        if isinstance(msg, dict):
            print(f"[{self.agent_id}] Parsing dict with keys: {list(msg.keys())}")
            try:
                msg = AgentMessage(**msg)
            except Exception as e:
                logger.error(f"[{self.agent_id}] Failed to validate AgentMessage: {e}")
                print(f"[{self.agent_id}] Validation error: {e}")
                print(f"[{self.agent_id}] Raw dict: {msg}")
                return

        print(f"[{self.agent_id}] Passing to session_handler: {msg.headers.interaction_type}")
        await self.session_handler(msg, self)

    async def publish_update(self, content: str, conversation_id: str, interaction_type: str):
        """
        Publish an update message to the conversation topic.
        
        Args:
            content: The text content of the message.
            conversation_id: The session/topic ID.
            interaction_type: The type of interaction (e.g., INFO_UPDATE, SQUAD_COMPLETION).
        """
        await self.broker.publish(
            AgentMessage(
                content=content,
                headers=A2AHeaders(
                    source_agent_id=self.agent_id,
                    interaction_type=interaction_type,
                    conversation_id=conversation_id
                )
            ),
            topic=conversation_id,
            headers={
                "x-source-agent": self.agent_id,
                "x-dest-topic": conversation_id,
                "x-interaction-type": interaction_type,
                "x-conversation-id": conversation_id
            }
        )

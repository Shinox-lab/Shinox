"""
Shinox Agent SDK - Worker Agent Class

The ShinoxWorkerAgent provides a simple way to create task-executing agents
with default wake-up logic and brain invocation.
"""

import logging
from typing import Optional, Any

from .base import ShinoxAgent
from .schemas import AgentMessage

# Try to import AgentCard from a2a.types, fall back to Any
try:
    from a2a.types import AgentCard
except ImportError:
    AgentCard = Any  # type: ignore

logger = logging.getLogger(__name__)


class ShinoxWorkerAgent(ShinoxAgent):
    """
    Worker agent with default wake-up logic and simple brain invocation.

    This is the simplest way to create a task-executing agent:

        from shinox_agent import ShinoxWorkerAgent
        from brain import brain
        from agent import agent_card

        agent = ShinoxWorkerAgent(
            agent_card=agent_card,
            brain=brain,
        )
        app = agent.app

    The worker will:
    1. Join sessions when invited by Director
    2. Listen for messages where it's @mentioned or targeted
    3. Invoke the brain and publish the result

    Wake-up triggers (agent responds when):
    - target_agent_id matches this agent's ID
    - @{agent_id} is mentioned in the message content
    - Any custom trigger word is found in the message
    """

    def __init__(
        self,
        agent_card: AgentCard,
        brain: Any,
        agent_url: Optional[str] = None,
        triggers: Optional[list[str]] = None,
    ):
        """
        Initialize a Worker Agent.

        Args:
            agent_card: A2A AgentCard defining the agent's identity and capabilities.
            brain: LangGraph/LangChain brain with ainvoke(state, config) method.
                   Expected signature: brain.ainvoke({"messages": [...]}, config={"configurable": {"thread_id": ...}})
            agent_url: URL where agent is accessible (for registry).
            triggers: Additional keywords that wake up the agent (case-insensitive).
        """
        self.brain = brain
        self._custom_triggers = triggers or []

        # Initialize base with our default handler
        super().__init__(
            agent_card=agent_card,
            session_handler=self._default_session_handler,
            triggers=triggers,
            agent_url=agent_url,
        )

    async def _default_session_handler(self, msg: AgentMessage, agent: 'ShinoxWorkerAgent'):
        """Default handler with wake-up logic and brain invocation."""
        headers = msg.headers
        my_id = agent.agent_id
        interaction_type = headers.interaction_type

        # --- Filtering ---

        # Ignore messages from self (loop prevention)
        if headers.source_agent_id == my_id:
            return

        # Session filtering (only process if we're in the session)
        if headers.conversation_id not in agent.active_sessions:
            # But allow direct inbox messages (TASK_ASSIGNMENT)
            if interaction_type != "TASK_ASSIGNMENT":
                return

        # --- Wake-up Logic ---
        # Workers follow explicit assignment model:
        # - Wait for TASK_ASSIGNMENT from orchestrator (squad-lead)
        # - Or explicit targeting (@mention, target_agent_id)
        # - Do NOT self-start on SESSION_BRIEFING - let squad-lead coordinate
        should_wake = False

        # 1. ALWAYS wake if directly targeted by agent ID
        if headers.target_agent_id == my_id:
            should_wake = True

        # 2. ALWAYS wake if @mentioned in content
        elif f"@{my_id}" in msg.content:
            should_wake = True

        # 3. Wake on TASK_ASSIGNMENT (explicit work from orchestrator)
        elif interaction_type == "TASK_ASSIGNMENT":
            should_wake = True

        # NOTE: Workers do NOT wake on:
        # - SESSION_BRIEFING: Squad-lead handles briefings and creates assignments
        # - INFO_UPDATE: Broadcast status updates, not direct tasks
        # - TASK_RESULT: Other agents' results, for passive observation
        # - AGENT_JOINED: Announcements only

        if not should_wake:
            logger.debug(f"[{my_id}] Ignoring {interaction_type} from {headers.source_agent_id}")
            return

        print(f"[{my_id}] Processing message from {headers.source_agent_id}")

        # --- Brain Invocation ---
        try:
            from langchain_core.messages import HumanMessage

            # Prepare state
            lc_msg = HumanMessage(content=msg.content, name=headers.source_agent_id)
            state = {"messages": [lc_msg]}
            config = {"configurable": {"thread_id": headers.conversation_id}}

            # Invoke brain
            result = await self.brain.ainvoke(state, config=config)

            # Extract response
            response_content = result["messages"][-1].content

            # Publish result
            await agent.publish_task_result(
                content=str(response_content),
                conversation_id=headers.conversation_id,
                target_agent_id=headers.source_agent_id,
            )

        except Exception as e:
            logger.error(f"[{my_id}] Brain invocation error: {e}")
            await agent.publish_task_result(
                content=f"Error processing request: {str(e)}",
                conversation_id=headers.conversation_id,
                target_agent_id=headers.source_agent_id,
            )

    async def publish_task_result(
        self,
        content: str,
        conversation_id: str,
        target_agent_id: Optional[str] = None,
    ):
        """
        Publish a task result back to the session.

        Args:
            content: The result content
            conversation_id: The session ID
            target_agent_id: Optional agent to target (usually the requester)
        """
        await self.publish_message(
            content=content,
            conversation_id=conversation_id,
            interaction_type="TASK_RESULT",
            target_agent_id=target_agent_id,
            route_through_governance=True,
        )

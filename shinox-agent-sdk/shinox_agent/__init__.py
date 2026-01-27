"""
Shinox Agent SDK

A Python library for building agents in the Shinox decentralized mesh network.

Quick Start (Worker Agent):
    from shinox_agent import ShinoxWorkerAgent

    agent = ShinoxWorkerAgent(
        agent_card=my_card,
        brain=my_brain,
    )
    app = agent.app

Quick Start (Custom Agent):
    from shinox_agent import ShinoxAgent

    async def handler(msg, agent):
        await agent.publish_message(...)

    agent = ShinoxAgent(
        agent_card=my_card,
        session_handler=handler,
    )
    app = agent.app
"""

from .base import ShinoxAgent
from .worker import ShinoxWorkerAgent
from .schemas import (
    AgentMessage,
    A2AHeaders,
    SystemCommand,
    SystemCommandMetadata,
    InteractionType,
    GovernanceStatus,
)

__version__ = "0.1.0"

__all__ = [
    # Agent classes
    "ShinoxAgent",
    "ShinoxWorkerAgent",
    # Message schemas
    "AgentMessage",
    "A2AHeaders",
    "SystemCommand",
    "SystemCommandMetadata",
    # Type aliases
    "InteractionType",
    "GovernanceStatus",
]

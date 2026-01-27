"""
General Fast Agent - Simple Logic Worker

This agent handles simple general logic tasks quickly and efficiently.
Uses the Shinox Agent SDK for automatic session management and messaging.
"""

from shinox_agent import ShinoxWorkerAgent
from brain import brain
from agent import agent_card

# --- Create Agent ---
# The SDK handles:
# - Registry registration
# - Inbox subscription
# - Session subscription on JOIN_SESSION
# - Wake-up logic (target_agent_id, @mention)
# - Brain invocation
# - Task result publishing

agent = ShinoxWorkerAgent(
    agent_card=agent_card,
    brain=brain,
    # Optional: Add custom wake-up triggers
    # triggers=["calculate", "logic"],
)

# Export app for faststream
app = agent.app

# --- Run ---
# faststream run main:app

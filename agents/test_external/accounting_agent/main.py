"""
Accounting Specialist Agent - Currency & Financial Worker

This agent handles accounting tasks including currency conversion,
invoice processing, and financial reporting.
Uses the Shinox Agent SDK for automatic session management and messaging.
"""

from shinox_agent import ShinoxWorkerAgent
from brain import brain
from agent import agent_card

# --- Create Agent ---
agent = ShinoxWorkerAgent(
    agent_card=agent_card,
    brain=brain,
    # Custom triggers for accounting-related keywords
    triggers=["currency", "convert", "invoice", "expense", "financial"],
)

# Export app for faststream
app = agent.app

# --- Run ---
# faststream run main:app

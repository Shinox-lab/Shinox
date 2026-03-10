"""Accounting Specialist Agent - A2A Bridge Worker"""

import os

from shinox_agent import ShinoxWorkerAgent, A2ABridge

agent = ShinoxWorkerAgent(
    name="Accounting Specialist Agent",
    brain=A2ABridge(os.getenv("A2A_AGENT_URL", "http://localhost:10002")),
    triggers=["currency", "convert", "invoice", "expense", "financial"],
)

# Export app for faststream
app = agent.app

# --- Run ---
# faststream run main:app

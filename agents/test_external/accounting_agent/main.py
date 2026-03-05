"""
Accounting Specialist Agent - Currency & Financial Worker (A2A Bridge)

This agent proxies accounting tasks to an external A2A-compatible agent.
Uses the Shinox Agent SDK with A2ABridge for seamless mesh integration.
"""

import os

from shinox_agent import ShinoxWorkerAgent, A2ABridge
from agent import agent_card

A2A_AGENT_URL = os.getenv("A2A_AGENT_URL", "http://localhost:10002")

bridge = A2ABridge(A2A_AGENT_URL)

# --- Create Agent ---
agent = ShinoxWorkerAgent(
    agent_card=agent_card,
    brain=bridge,
    triggers=["currency", "convert", "invoice", "expense", "financial"],
)

# Export app for faststream
app = agent.app


@app.on_startup
async def _on_startup():
    remote_card = await bridge.fetch_agent_card()
    if remote_card:
        agent.agent_card.skills = remote_card.skills


@app.on_shutdown
async def _on_shutdown():
    await bridge.close()


# --- Run ---
# faststream run main:app

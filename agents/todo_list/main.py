"""
Todo List Agent

This agent manages a persistent todo list using an MCP server backend.
Uses the Shinox Agent SDK for automatic session management and messaging.
"""

import os

from shinox_agent import ShinoxWorkerAgent
from brain import brain, mcp_client
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
)

# Export app for faststream
app = agent.app


# --- MCP Client Lifecycle ---

@app.on_startup
async def _on_startup():
    """Connect to the MCP todo server on startup."""
    import logging
    logger = logging.getLogger(__name__)

    server_path = os.getenv(
        "MCP_SERVER_PATH",
        os.path.abspath(
            os.path.join(os.path.dirname(__file__), "mcp_tool", "server", "dist", "index.js")
        ),
    )
    try:
        await mcp_client.connect(server_path)
    except Exception as e:
        logger.error(f"Failed to start MCP server — todo tools will be unavailable: {e}")


@app.on_shutdown
async def _on_shutdown():
    """Clean up MCP client on shutdown."""
    if mcp_client.is_connected:
        await mcp_client.cleanup()


# --- Run ---
# faststream run main:app

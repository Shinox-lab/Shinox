import os
import logging
from typing import Optional
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

DEFAULT_SERVER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "server", "dist", "index.js"
)


class MCPClient:
    def __init__(self):
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self._connected = False

    async def connect(self, server_script_path: Optional[str] = None):
        """Connect to an MCP server.

        Args:
            server_script_path: Path to the server script (.py or .js).
                Defaults to the sibling server at ../server/dist/index.js.
        """
        if server_script_path is None:
            server_script_path = os.path.abspath(DEFAULT_SERVER_PATH)

        is_python = server_script_path.endswith('.py')
        is_js = server_script_path.endswith('.js')
        if not (is_python or is_js):
            raise ValueError("Server script must be a .py or .js file")

        command = "python" if is_python else "node"

        try:
            server_params = StdioServerParameters(
                command=command,
                args=[server_script_path],
                env=None
            )

            stdio_transport = await self.exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            self.stdio, self.write = stdio_transport
            self.session = await self.exit_stack.enter_async_context(
                ClientSession(self.stdio, self.write)
            )

            await self.session.initialize()
            self._connected = True

            response = await self.session.list_tools()
            tools = response.tools
            logger.info(f"Connected to MCP server with tools: {[t.name for t in tools]}")
            return tools

        except Exception as e:
            self._connected = False
            logger.error(f"Failed to connect to MCP server at {server_script_path}: {e}")
            raise

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def list_tools(self):
        """List available tools from the server."""
        if not self._connected:
            raise ConnectionError("MCP client is not connected. Call connect() first.")
        response = await self.session.list_tools()
        return response.tools

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Call a tool on the server and return the text result."""
        if not self._connected:
            raise ConnectionError("MCP client is not connected. Call connect() first.")
        result = await self.session.call_tool(name, arguments)
        texts = []
        for item in result.content:
            if item.type == "text":
                texts.append(item.text)
        return "\n".join(texts)

    async def cleanup(self):
        """Clean up resources."""
        await self.exit_stack.aclose()
        self._connected = False

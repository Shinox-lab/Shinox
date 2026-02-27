import os
import sys
import operator
import logging
from typing import Annotated, List, Optional, TypedDict
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode
from langchain_core.messages import SystemMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

# Add mcp_tool/client/ to import path so `client.py` is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "mcp_tool", "client"))
from client import MCPClient  # noqa: E402 — imported from mcp_tool/client/client.py via sys.path
load_dotenv()

logger = logging.getLogger(__name__)

memory = MemorySaver()

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_NAME = os.getenv("OPENAI_MODEL_NAME", "nvidia/nemotron-3-nano-30b-a3b:free")

# --- MCP Client (singleton, connected at startup via main.py) ---
mcp_client = MCPClient()


# --- LangChain Tools (proxy to MCP server) ---

@tool
async def create_todo(title: str, description: str) -> str:
    """Create a new todo item with a title and description (markdown supported)."""
    return await mcp_client.call_tool("create-todo", {"title": title, "description": description})


@tool
async def list_todos() -> str:
    """List all todos."""
    return await mcp_client.call_tool("list-todos", {})


@tool
async def get_todo(id: str) -> str:
    """Get a specific todo by its UUID."""
    print(f"[DEBUG] Calling get_todo with id={id}")
    return await mcp_client.call_tool("get-todo", {"id": id})


@tool
async def update_todo(id: str, title: Optional[str] = None, description: Optional[str] = None) -> str:
    """Update a todo's title or description. At least one field must be provided."""
    print(f"[DEBUG] Calling update_todo with id={id}, title={title}, description={description}")
    args = {"id": id}
    if title:
        args["title"] = title
    if description:
        args["description"] = description
    return await mcp_client.call_tool("update-todo", args)


@tool
async def complete_todo(id: str) -> str:
    """Mark a todo as completed."""
    print(f"[DEBUG] Calling complete_todo with id={id}")
    return await mcp_client.call_tool("complete-todo", {"id": id})


@tool
async def delete_todo(id: str) -> str:
    """Delete a todo permanently."""
    print(f"[DEBUG] Calling delete_todo with id={id}")
    return await mcp_client.call_tool("delete-todo", {"id": id})


@tool
async def search_todos_by_title(title: str) -> str:
    """Search todos by title (case insensitive partial match)."""
    print(f"[DEBUG] Calling search_todos_by_title with title={title}")
    return await mcp_client.call_tool("search-todos-by-title", {"title": title})


@tool
async def search_todos_by_date(date: str) -> str:
    """Search todos by creation date (format: YYYY-MM-DD)."""
    print(f"[DEBUG] Calling search_todos_by_date with date={date}")
    return await mcp_client.call_tool("search-todos-by-date", {"date": date})


@tool
async def list_active_todos() -> str:
    """List all non-completed todos."""
    print(f"[DEBUG] Calling list_active_todos")
    return await mcp_client.call_tool("list-active-todos", {})


@tool
async def summarize_active_todos() -> str:
    """Generate a summary of all active (non-completed) todos."""
    print(f"[DEBUG] Calling summarize_active_todos")
    return await mcp_client.call_tool("summarize-active-todos", {})


todo_tools = [
    create_todo, list_todos, get_todo, update_todo,
    complete_todo, delete_todo, search_todos_by_title,
    search_todos_by_date, list_active_todos, summarize_active_todos,
]

# --- LLM with tool binding ---
llm = ChatOpenAI(
    model=OPENAI_MODEL_NAME,
    base_url=OPENAI_BASE_URL,
    api_key=OPENAI_API_KEY,
).bind_tools(todo_tools)


# --- State ---
class SquadState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]


# --- System Prompt ---
SYSTEM_PROMPT = """\
You are The Todo Manager — a focused, organized, and reliable AI assistant dedicated to managing the user's todo list.

YOUR DOMAIN:
- Creating, reading, updating, completing, and deleting todo items
- Searching and filtering todos by title or date
- Summarizing active tasks and providing status overviews

NOT YOUR DOMAIN:
- General knowledge questions, philosophy, or conversation unrelated to todos
- Calendar scheduling, reminders, or time-based alerts
- Any task not related to todo list management

TOOL USAGE:
1. You have access to todo management tools. ALWAYS use the appropriate tool to fulfill requests — never fabricate todo data from memory.
2. For ambiguous requests (e.g., "delete my task"), first use search or list tools to identify the correct item, then act.
3. When creating todos, use markdown formatting in the description field for readability (headers, bullet points, checkboxes).
4. When multiple actions are needed (e.g., "create three tasks"), execute each tool call individually — do not batch into one.

RESPONSE RULES:
5. Always confirm the action taken and show the relevant result (e.g., the created todo's title and ID).
6. When listing todos, format them clearly — include title, status, and ID.
7. If a tool call fails or returns an error, tell the user what went wrong and suggest a retry or alternative.
8. If the user's request is unclear, ask for clarification rather than guessing which todo they mean.

TONE: Organized, efficient, and friendly. Like a sharp personal assistant who keeps things tidy.\
"""


# --- Nodes ---

async def node_planner(state: SquadState):
    """Process the user's message and decide on tool calls or direct response."""
    messages = [SystemMessage(content=SYSTEM_PROMPT)]
    for msg in state["messages"]:
        if isinstance(msg, tuple):
            messages.append(HumanMessage(content=msg[1]))
        else:
            messages.append(msg)

    response = await llm.ainvoke(messages)
    return {"messages": [response]}


def should_continue(state: SquadState):
    """Route to tool execution if the last message has tool calls, otherwise end."""
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return END


tool_node = ToolNode(todo_tools)


# --- Graph ---

def _build_workflow() -> StateGraph:
    wf = StateGraph(SquadState)

    wf.add_node("planner", node_planner)
    wf.add_node("tools", tool_node)

    wf.set_entry_point("planner")
    wf.add_conditional_edges("planner", should_continue)
    wf.add_edge("tools", "planner")

    return wf


brain = _build_workflow().compile(checkpointer=memory)

SUPPORTED_CONTENT_TYPES = ['text', 'text/plain']

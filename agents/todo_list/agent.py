from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)

# --- Skills ---

todo_crud = AgentSkill(
    id="todo_crud",
    name="Todo CRUD Operations",
    description="Creates, reads, updates, and deletes todo items. Supports markdown-formatted descriptions and unique IDs for precise item management.",
    tags=["todo", "create", "read", "update", "delete", "crud"],
    examples=[
        "Create a new todo to buy groceries",
        "Show me all my todos",
        "Update the description of todo abc-123",
        "Delete the todo about laundry",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

todo_completion = AgentSkill(
    id="todo_completion",
    name="Todo Completion Tracking",
    description="Marks todos as completed and lists all active (non-completed) todos for status tracking.",
    tags=["todo", "complete", "done", "active", "status", "tracking"],
    examples=[
        "Mark the grocery todo as completed",
        "Show me all my active todos",
        "Which todos are still pending?",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

todo_search = AgentSkill(
    id="todo_search",
    name="Todo Search & Filtering",
    description="Searches todos by title (case-insensitive partial match) or by creation date (YYYY-MM-DD format).",
    tags=["todo", "search", "filter", "find", "query", "date", "title"],
    examples=[
        "Search for todos about work",
        "Find all todos created on 2026-02-18",
        "Which todos mention 'meeting'?",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

todo_summarize = AgentSkill(
    id="todo_summarize",
    name="Todo Summarization",
    description="Generates a summary overview of all active (non-completed) todos, useful for daily standups or status reports.",
    tags=["todo", "summary", "overview", "report", "digest"],
    examples=[
        "Summarize my active todos",
        "Give me a daily todo digest",
        "What's on my plate right now?",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

# --- Agent Card ---

agent_card = AgentCard(
    name="Todo List Agent",
    description=(
        "A focused productivity agent that manages a persistent todo list backed by a database. "
        "Supports full CRUD operations, completion tracking, and search by title or date. "
        "Returns raw todo data — does NOT format or present results for end users."
    ),
    url="http://localhost:10002/",
    version="1.0.0",
    capabilities=AgentCapabilities(
        streaming=True,
        push_notifications=True,
    ),
    skills=[todo_crud, todo_completion, todo_search, todo_summarize],
    default_input_modes=["text"],
    default_output_modes=["text"],
)

SELF_INTRODUCTION = """
id: "agent-todo-list-01"
role: "Todo List Agent"
description: "I am a Todo List Agent. I manage a persistent todo list with full CRUD operations, completion tracking, search by title or date, and active todo summarization."
capabilities: ["create_todo", "list_todos", "update_todo", "complete_todo", "delete_todo", "search_by_title", "search_by_date", "list_active"]
triggers: ["todo", "task", "reminder", "list", "create", "complete", "delete", "search", "summarize"]
"""

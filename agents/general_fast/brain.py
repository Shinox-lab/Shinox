import os
import operator
from collections.abc import AsyncIterable
from typing import Annotated, List, TypedDict, Union, Dict, Literal
import httpx
from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, ToolMessage, BaseMessage, AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel
from dotenv import load_dotenv

# --------------

load_dotenv()

memory = MemorySaver()

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_NAME = "nvidia/nemotron-3-nano-30b-a3b:free"  # Use a smart model for orchestration

llm = ChatOpenAI(
    model=OPENAI_MODEL_NAME,
    base_url=OPENAI_BASE_URL,
    api_key=OPENAI_API_KEY,
)

# --- 1. The State ---
# This is persisted to Postgres between Kafka events
class SquadState(TypedDict):
    # The full conversation history (Passive Memory)
    messages: Annotated[List[BaseMessage], operator.add]

def node_planner(state: SquadState):
    """
    The Strategic Thinker. Looks at history and generates/updates the DAG.
    """
    system_prompt = """
You are The Generalist — a quick, reliable, and well-rounded AI assistant operating as part of a multi-agent team.

Your purpose is to handle general knowledge queries, common-sense reasoning, summarization, and simple logic tasks. You are the go-to agent when no specialist is required.

YOUR DOMAIN:
- General knowledge and trivia (history, geography, science, culture)
- Common-sense reasoning and simple logic
- Summarization and synthesis of information
- Language tasks (paraphrasing, explanation, comparison)
- Combining or interpreting results from other agents when asked by the Squad Lead

NOT YOUR DOMAIN:
- Currency conversion, financial calculations, or live market data
- Database queries, code execution, or technical debugging
- Specialized philosophical, legal, or medical analysis
- Any task requiring external tools, APIs, or real-time data

RESPONSE STYLE:
1. Respond directly in plain text. Do NOT generate JSON, action schemas, or tool calls.
2. Be concise and clear — prioritize accuracy over length.
3. Structure longer answers with short paragraphs or bullet points for readability.
4. When summarizing results from other agents, preserve key facts and present them in a user-friendly way.

TONE: Helpful, direct, and efficient. Like a knowledgeable colleague who gives you a straight answer without unnecessary preamble.
    """
    
    # Build the message list - convert tuples to proper message objects
    messages = [SystemMessage(content=system_prompt)]
    for msg in state['messages']:
        if isinstance(msg, tuple):
            # Convert tuple format ('user', 'content') to HumanMessage
            messages.append(HumanMessage(content=msg[1]))
        else:
            # Already a BaseMessage object (AIMessage, ToolMessage, etc.)
            messages.append(msg)
    
    print(f"DEBUG: node_planner called with {len(state['messages'])} messages")
    print(f"DEBUG: Last message type: {type(state['messages'][-1])}")
    
    response = llm.invoke(messages)
    
    return {"messages": [response]}
    # Pseudo-parsing the LLM response into a list
    # In prod, use structured_output (Pydantic)
    # new_plan = response.content.split("\n") 
    
    # return {"plan": new_plan, "squad_status": "EXECUTING"}

# --- 3. The Graph ---

workflow = StateGraph(SquadState)

workflow.add_node("planner", node_planner)

# Add edges to connect nodes
workflow.set_entry_point("planner")
# After tool execution, go back to planner for final response
workflow.add_edge("planner", END)

# Compile with recursion limit to prevent infinite loops
brain = workflow.compile(checkpointer=memory)

SUPPORTED_CONTENT_TYPES = ['text', 'text/plain']
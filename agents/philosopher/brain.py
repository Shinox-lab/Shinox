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
    You are a wise and thoughtful Philosopher AI.
    Your purpose is to assist users by exploring deep questions, analyzing ethical dilemmas, and providing wisdom drawn from the history of philosophy.

    YOUR DOMAIN: Philosophy, ethics, existentialism, logic, critical thinking, and finding meaning.
    NOT YOUR DOMAIN: Real-time data, trivial facts, unrelated calculations, or purely technical execution without deeper context.

    IMPORTANT INSTRUCTIONS:
    1. Respond with depth, nuance, and clarity. Avoid superficial answers.
    2. Draw upon the ideas of great philosophers (e.g., Socrates, Kant, Nietzsche, Confucius) where relevant, but explain them simply.
    3. Encourage the user to think critically. Sometimes the best answer is a thought-provoking question.
    6. You do not have access to any external tools or live data.

    HONESTY RULES:
    7. If a task is outside your domain (e.g., "What is the stock price of Apple?"), gently steer the conversation back to philosophical principles or admit you deal in wisdom, not raw data.
    8. Be humble in your wisdom. Acknowledge the complexity of truth.
    9. If you are not certain, frame your answer as a perspective or a possibility rather than absolute factormation" and specify what's missing.
    8. NEVER fabricate data, statistics, live prices, exchange rates, or any information that requires real-time lookup. If asked for such data, say "I don't have access to live data for this request."
    9. A honest "I don't know" is always better than a confident wrong answer.
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
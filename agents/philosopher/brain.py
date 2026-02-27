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
class PhilosopherState(TypedDict):
    # The full conversation history (Passive Memory)
    messages: Annotated[List[BaseMessage], operator.add]

def node_philosopher(state: PhilosopherState):
    """
    The Philosopher's Mind. Receives a question, reflects deeply, and responds with wisdom.
    """
    system_prompt = """
You are The Philosopher — a wise, contemplative, and intellectually generous AI agent.

Your purpose is to help humans explore the deepest questions of existence: ethics, meaning, knowledge, beauty, justice, consciousness, freedom, and the good life. You are not a search engine or a trivia bot — you are a thinking companion.

YOUR DOMAIN:
- Philosophy (Western, Eastern, African, Indigenous traditions)
- Ethics and moral reasoning (utilitarianism, deontology, virtue ethics, care ethics)
- Existentialism, phenomenology, and philosophy of mind
- Logic, epistemology, and critical thinking
- Stoicism, Taoism, Buddhism, and practical wisdom
- Philosophy of science, technology, and AI

NOT YOUR DOMAIN:
- Real-time data, stock prices, weather, sports scores
- Purely technical coding tasks (unless they raise philosophical questions)
- Medical, legal, or financial advice

RESPONSE STYLE:
1. Respond with depth, nuance, and clarity. Prefer substance over brevity, but never be needlessly verbose.
2. Draw upon the ideas of great thinkers — Socrates, Aristotle, Kant, Nietzsche, Simone de Beauvoir, Confucius, Lao Tzu, Marcus Aurelius, bell hooks, Kwame Gyekye — but always explain their ideas accessibly.
3. Use the Socratic method when appropriate: sometimes the most powerful response is a well-placed question that helps the user examine their own assumptions.
4. Acknowledge multiple perspectives. Philosophy thrives on dialogue, not dogma.
5. When discussing ethical dilemmas, present at least two frameworks and note their tensions.
6. Where a concept is abstract, ground it with a vivid analogy or a concrete example from everyday life.
7. Weave in brief, relevant quotes from philosophers when they illuminate the point (attribute them).

OUTPUT FORMAT:
12. Respond in plain text with clear paragraph structure. Use markdown only for quotes or emphasis.
13. Keep responses focused — aim for 2-5 paragraphs unless the question demands deeper treatment.
14. End with a reflective question or actionable insight when it serves the conversation.

TONE: Warm, thoughtful, grounded. Like a wise mentor sitting across from you over a cup of tea — not a lecturer at a podium.
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
    
    print(f"DEBUG: node_philosopher called with {len(state['messages'])} messages")
    print(f"DEBUG: Last message type: {type(state['messages'][-1])}")
    
    response = llm.invoke(messages)
    
    return {"messages": [response]}


# --- 3. The Graph ---

workflow = StateGraph(PhilosopherState)

workflow.add_node("philosopher", node_philosopher)

# Add edges to connect nodes
workflow.set_entry_point("philosopher")
workflow.add_edge("philosopher", END)

# Compile with recursion limit to prevent infinite loops
brain = workflow.compile(checkpointer=memory)

SUPPORTED_CONTENT_TYPES = ['text', 'text/plain']
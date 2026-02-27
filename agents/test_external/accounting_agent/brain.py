


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

@tool
def get_exchange_rate(
    currency_from: str = 'USD',
    currency_to: str = 'EUR',
    currency_date: str = 'latest',
):
    """Use this to get current exchange rate.

    Args:
        currency_from: The currency to convert from (e.g., "USD").
        currency_to: The currency to convert to (e.g., "EUR").
        currency_date: The date for the exchange rate or "latest". Defaults to
            "latest".

    Returns:
        A dictionary containing the exchange rate data, or an error message if
        the request fails.
    """
    try:
        response = httpx.get(
            f'https://api.frankfurter.app/{currency_date}',
            params={'from': currency_from, 'to': currency_to},
        )
        response.raise_for_status()

        data = response.json()
        if 'rates' not in data:
            return {'error': 'Invalid API response format.'}
        return data
    except httpx.HTTPError as e:
        return {'error': f'API request failed: {e}'}
    except ValueError:
        return {'error': 'Invalid JSON response from API.'}

tools = [get_exchange_rate]
tools_by_name = {tool.name: tool for tool in tools}
model_with_tools = llm.bind_tools(tools)

print(f"Loaded tools: {list(tools_by_name.keys())}")
print(f"Raw Loaded tools: {tools_by_name}")

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
    You are a specialized accounting assistant working as part of an agent team.
    Your sole purpose is to use the 'get_exchange_rate' tool to answer questions about currency exchange rates and conversions.

    YOUR DOMAIN: Currency conversion, exchange rates, and basic accounting calculations.
    NOT YOUR DOMAIN: General knowledge, code, non-financial topics, or anything not related to currency/accounting.

    IMPORTANT INSTRUCTIONS:
    1. Use the 'get_exchange_rate' tool for currency questions. Do NOT fabricate exchange rates.
    2. Do not attempt to answer unrelated questions or use tools for other purposes.
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
    
    response = model_with_tools.invoke(messages)
    
    print(f"DEBUG: LLM response has tool_calls: {bool(response.tool_calls)}")
    
    return {"messages": [response]}
    # Pseudo-parsing the LLM response into a list
    # In prod, use structured_output (Pydantic)
    # new_plan = response.content.split("\n") 
    
    # return {"plan": new_plan, "squad_status": "EXECUTING"}

def tool_node(state: dict):
    """Performs the tool call"""

    result = []
    for tool_call in state["messages"][-1].tool_calls:
        tool = tools_by_name[tool_call["name"]]
        observation = tool.invoke(tool_call["args"])
        result.append(ToolMessage(content=observation, tool_call_id=tool_call["id"]))
    return {"messages": result}


def should_continue(state: SquadState):
    """Decide if we should continue the loop or stop based upon whether the LLM made a tool call"""

    messages = state["messages"]
    last_message = messages[-1]

    # If the LLM makes a tool call, then perform an action
    if last_message.tool_calls:
        return "tool_node"

    # Otherwise, we stop (reply to the user)
    return END
# --- 3. The Graph ---

workflow = StateGraph(SquadState)

workflow.add_node("planner", node_planner)
workflow.add_node("tool_node", tool_node)

# Add edges to connect nodes
workflow.set_entry_point("planner")
workflow.add_conditional_edges(
    "planner",
    should_continue
)
# After tool execution, go back to planner for final response
workflow.add_edge("tool_node", "planner")

# Compile with recursion limit to prevent infinite loops
brain = workflow.compile(checkpointer=memory)

SUPPORTED_CONTENT_TYPES = ['text', 'text/plain']

# class CurrencyAgent:
#     """CurrencyAgent - a specialized assistant for currency convesions."""

#     SYSTEM_INSTRUCTION = (
#         'You are a specialized assistant for currency conversions. '
#         "Your sole purpose is to use the 'get_exchange_rate' tool to answer questions about currency exchange rates. "
#         'If the user asks about anything other than currency conversion or exchange rates, '
#         'politely state that you cannot help with that topic and can only assist with currency-related queries. '
#         'Do not attempt to answer unrelated questions or use tools for other purposes.'
#     )

#     FORMAT_INSTRUCTION = (
#         'Set response status to input_required if the user needs to provide more information to complete the request.'
#         'Set response status to error if there is an error while processing the request.'
#         'Set response status to completed if the request is complete.'
#     )

#     def __init__(self):
#         self.model = ChatOpenAI(
#             model=OPENAI_MODEL_NAME,
#             openai_api_key=OPENAI_API_KEY,
#             openai_api_base=OPENAI_BASE_URL,
#             temperature=0,
#         )
#         self.tools = [get_exchange_rate]

#         self.graph = create_agent(
#             self.model,
#             tools=self.tools,
#             checkpointer=memory,
#             prompt=self.SYSTEM_INSTRUCTION,
#             response_format=(self.FORMAT_INSTRUCTION, ResponseFormat),
#         )

#     async def stream(self, query, context_id) -> AsyncIterable[dict[str, Any]]:
#         inputs = {'messages': [('user', query)]}
#         config = {'configurable': {'thread_id': context_id}}

#         for item in self.graph.stream(inputs, config, stream_mode='values'):
#             message = item['messages'][-1]
#             if (
#                 isinstance(message, AIMessage)
#                 and message.tool_calls
#                 and len(message.tool_calls) > 0
#             ):
#                 yield {
#                     'is_task_complete': False,
#                     'require_user_input': False,
#                     'content': 'Looking up the exchange rates...',
#                 }
#             elif isinstance(message, ToolMessage):
#                 yield {
#                     'is_task_complete': False,
#                     'require_user_input': False,
#                     'content': 'Processing the exchange rates..',
#                 }

#         yield self.get_agent_response(config)

#     def get_agent_response(self, config):
#         current_state = self.graph.get_state(config)
#         structured_response = current_state.values.get('structured_response')
#         if structured_response and isinstance(
#             structured_response, ResponseFormat
#         ):
#             if structured_response.status == 'input_required':
#                 return {
#                     'is_task_complete': False,
#                     'require_user_input': True,
#                     'content': structured_response.message,
#                 }
#             if structured_response.status == 'error':
#                 return {
#                     'is_task_complete': False,
#                     'require_user_input': True,
#                     'content': structured_response.message,
#                 }
#             if structured_response.status == 'completed':
#                 return {
#                     'is_task_complete': True,
#                     'require_user_input': False,
#                     'content': structured_response.message,
#                 }

#         return {
#             'is_task_complete': False,
#             'require_user_input': True,
#             'content': (
#                 'We are unable to process your request at the moment. '
#                 'Please try again.'
#             ),
#         }

#     SUPPORTED_CONTENT_TYPES = ['text', 'text/plain']
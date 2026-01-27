import operator
import os
import time
from typing import Annotated, List, TypedDict, Dict, Literal, Optional
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import SystemMessage, BaseMessage
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

load_dotenv()

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_NAME = "nvidia/nemotron-3-nano-30b-a3b:free"


# --- 1. The State ---
class SquadState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    plan: List[List[str]]
    assignments: Dict[str, str]
    available_squad_agents: List[str]
    squad_status: Literal["IDLE", "PLANNING", "EXECUTING", "BLOCKED", "DONE", "UPDATING"]
    next_actions: List[Dict[str, str]]
    # Track completed task results for context injection into subsequent stages
    completed_results: Dict[str, str]  # agent_id -> result content


# --- 2. LLM Instance ---
llm = ChatOpenAI(
    model=OPENAI_MODEL_NAME,
    base_url=OPENAI_BASE_URL,
    api_key=OPENAI_API_KEY,
)

# --- 3. The Nodes ---
def node_monitor(state: SquadState):
    """
    Analyzes the latest messages. Decides if we need to intervene.
    """
    last_msg = state['messages'][-1]
    result = {}

    if state.get("squad_status") == "UPDATING":
        result = {"squad_status": "UPDATING"}
    elif "MISSION" in str(last_msg.content) or "Please" in str(last_msg.content): 
        result = {"squad_status": "PLANNING"}
    else:
        # If currently EXECUTING, we don't switch to IDLE automatically essentially blocking the loop
        current = state.get("squad_status")
        if current not in ["EXECUTING", "UPDATING", "PLANNING"]:
            result = {"squad_status": "IDLE"}

    return result


def node_planner(state: SquadState):
    """
    The Strategic Thinker. Looks at history and generates/updates the DAG.
    """
    prompt = f"""
    You are the Squad Lead, a COORDINATOR agent.

    GOAL: Break down the user's request into a sequence of STAGES.

    AVAILABLE AGENTS:
    {state['available_squad_agents']}

    CURRENT PLAN: {state.get('plan', [])}
    LATEST REQUEST: {state['messages'][-1].content}

    CRITICAL RULES:
    1. You are ONLY a coordinator - NEVER assign tasks to yourself (squad-lead-agent).
    2. Delegate ALL work to the available worker agents.
    3. For summarizing/combining results, use generalist-simple-logic-agent.
    4. Tasks in the same STAGE run in PARALLEL.
    5. Tasks in later stages depend on earlier stages.

    FORMAT:
    STAGE
    agent-id: instruction
    agent-id: instruction
    STAGE
    agent-id: instruction

    Example Output:
    STAGE
    accounting-specialist-agent: Convert 1000 USD to MYR
    STAGE
    generalist-simple-logic-agent: Summarize the conversion result for the user
    """

    response = llm.invoke([SystemMessage(content=prompt)])

    # Parse the LLM response into List[List[str]]
    raw_lines = response.content.strip().split("\n")
    new_plan = []
    current_stage = []

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        if "STAGE" in line:
            if current_stage:
                new_plan.append(current_stage)
            current_stage = []
        elif ":" in line:
            current_stage.append(line)

    if current_stage:
        new_plan.append(current_stage)

    result = {"plan": new_plan, "squad_status": "EXECUTING"}
    
    return result


def node_executor(state: SquadState):
    """
    The Doer. Takes the next tasks from the plan and prepares the commands.
    Injects context from completed results into subsequent stage instructions.
    """
    plan = state.get('plan', [])
    assignments = state.get('assignments', {})
    completed_results = state.get('completed_results', {})

    if not plan:
        return {"next_actions": [], "squad_status": "DONE"}

    current_stage = plan[0]
    new_actions = []
    new_assignments = assignments.copy()

    # Build context from completed results if any exist
    context_block = ""
    if completed_results:
        context_lines = ["## Previous Results from Squad Members:\n"]
        for agent_id, result in completed_results.items():
            # Truncate very long results to avoid token explosion
            truncated = result[:2000] + "..." if len(result) > 2000 else result
            context_lines.append(f"### {agent_id}:\n{truncated}\n")
        context_block = "\n".join(context_lines) + "\n---\n\n"

    for task in current_stage:
        if ":" in task:
            target_id, instruction = task.split(":", 1)
            target_id = target_id.strip()

            if target_id not in assignments:
                # Inject context for tasks that likely need it (summarize, combine, etc.)
                final_instruction = instruction.strip()
                if context_block:
                    final_instruction = f"{context_block}**Your Task:** {final_instruction}"

                new_actions.append({
                    "target": target_id,
                    "instruction": final_instruction
                })
                new_assignments[target_id] = task

    result = {
        "next_actions": new_actions,
        "assignments": new_assignments,
        "squad_status": "EXECUTING"
    }

    return result


def node_updater(state: SquadState):
    """
    Updates the state after task execution (removes the finished task).
    Also stores completed results for context injection into subsequent stages.
    """
    last_msg = state['messages'][-1]
    source_agent = last_msg.name
    result_content = str(last_msg.content)

    assignments = state.get('assignments', {}).copy()
    plan = state.get('plan', [])
    completed_results = state.get('completed_results', {}).copy()

    # Store the completed result for context injection
    completed_results[source_agent] = result_content

    # Remove from assignments
    if source_agent in assignments:
        del assignments[source_agent]

    # Remove from plan
    result = {
        "assignments": assignments,
        "squad_status": "EXECUTING",
        "completed_results": completed_results
    }

    if plan:
        current_stage = plan[0]
        new_stage = [t for t in current_stage if not t.startswith(f"{source_agent}:")]

        if not new_stage:
            new_plan = plan[1:]
        else:
            new_plan = [new_stage] + plan[1:]

        result["plan"] = new_plan

    return result


# --- 5. The Router ---

def router(state: SquadState):
    """Route to the next node based on squad status."""
    status = state.get("squad_status")
    next_node = END

    if status == "PLANNING":
        next_node = "planner"
    elif status == "UPDATING":
        next_node = "updater"
    elif status == "EXECUTING":
        next_node = "executor"

    return next_node


# --- 6. Build the Graph ---

workflow = StateGraph(SquadState)

workflow.add_node("monitor", node_monitor)
workflow.add_node("planner", node_planner)
workflow.add_node("updater", node_updater)
workflow.add_node("executor", node_executor)

workflow.set_entry_point("monitor")

workflow.add_conditional_edges("monitor", router)
workflow.add_edge("planner", "executor")
workflow.add_edge("updater", "executor")
workflow.add_edge("executor", END)

# Compile with persistence
brain = workflow.compile(checkpointer=MemorySaver())

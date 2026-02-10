import logging
import operator
import os
from typing import Annotated, List, TypedDict, Dict, Literal
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import SystemMessage, BaseMessage
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_NAME = "nvidia/nemotron-3-nano-30b-a3b:free"


# --- 1. The State ---
class SquadState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    plan: List[List[str]]
    assignments: Dict[str, List[str]]  # agent_id -> list of task strings (supports multiple tasks per agent)
    available_squad_agents: List[str]
    squad_status: Literal["IDLE", "PLANNING", "EXECUTING", "BLOCKED", "DONE", "UPDATING"]
    next_actions: List[Dict[str, str]]
    # Track completed task results for context injection into subsequent stages
    completed_results: Dict[str, str]  # agent_id -> result content
    current_stage_index: int


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
    elif "having difficulty" in str(last_msg.content) or "need help" in str(last_msg.content).lower():
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

    AVAILABLE AGENTS (format: "agent-id: description of capabilities"):
    {chr(10).join('- ' + a for a in state['available_squad_agents'])}

    CURRENT PLAN: {state.get('plan', [])}
    LATEST REQUEST: {state['messages'][-1].content}

    CRITICAL RULES:
    1. You are ONLY a coordinator - NEVER assign tasks to yourself (squad-lead-agent).
    2. Delegate ALL work to the available worker agents.
    3. MATCH each task to the agent whose DESCRIPTION best fits that task.
       - Currency/math/finance tasks → agent with accounting/finance in description
       - General knowledge/facts/geography → agent with generalist/general-purpose in description
       - Summarizing/combining multiple results → generalist agent
    4. If NO specialist matches a task, assign it to the generalist agent.
    5. Tasks in the same STAGE run in PARALLEL.
    6. Tasks in later stages depend on earlier stages.
    7. Use ONLY the exact agent-id from the list above (the part before the colon).

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

    result = {"plan": new_plan, "squad_status": "EXECUTING", "current_stage_index": 0}

    return result


def node_executor(state: SquadState):
    """
    The Doer. Takes the next tasks from the plan and prepares the commands.
    Injects context from completed results into subsequent stage instructions.
    """
    plan = state.get('plan', [])
    assignments = state.get('assignments', {})
    completed_results = state.get('completed_results', {})
    current_stage_index = state.get("current_stage_index", 0)

    if not plan:
        return {
            "next_actions": [],
            "squad_status": "DONE",
            "current_stage_index": current_stage_index,
        }

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

    # Track which tasks in this stage are already assigned (by full task string)
    already_assigned_tasks = set()
    for task_list in assignments.values():
        already_assigned_tasks.update(task_list)

    for task in current_stage:
        if ":" in task:
            target_id, instruction = task.split(":", 1)
            target_id = target_id.strip()

            if task not in already_assigned_tasks:
                # Inject context for tasks that likely need it (summarize, combine, etc.)
                final_instruction = instruction.strip()
                if context_block:
                    final_instruction = f"{context_block}**Your Task:** {final_instruction}"

                new_actions.append({
                    "target": target_id,
                    "instruction": final_instruction
                })
                # Append to the agent's task list (supports multiple tasks per agent)
                if target_id not in new_assignments:
                    new_assignments[target_id] = []
                new_assignments[target_id].append(task)

    result = {
        "next_actions": new_actions,
        "assignments": new_assignments,
        "squad_status": "EXECUTING",
        "current_stage_index": current_stage_index,
    }

    return result


def _detect_task_failure(result_content: str) -> bool:
    """Detect whether a task result indicates failure or inability to complete."""
    failure_indicators = [
        "having difficulty with a task",
        "i cannot help",
        "outside my capabilities",
        "i'm unable to",
        "i am unable to",
        "i don't have the ability",
        "not within my expertise",
        "beyond my scope",
        "i can only assist with",
        "i'm not able to",
        "i am not able to",
        "cannot assist with that",
        "not equipped to handle",
    ]
    content_lower = result_content.lower()
    return any(indicator in content_lower for indicator in failure_indicators)


def node_updater(state: SquadState):
    """
    Updates the state after task execution (removes the finished task).
    Also stores completed results for context injection into subsequent stages.
    Handles re-delegation when a worker reports it is stuck.
    """
    last_msg = state['messages'][-1]
    source_agent = last_msg.name
    result_content = str(last_msg.content)

    assignments = state.get('assignments', {}).copy()
    plan = state.get('plan', [])
    completed_results = state.get('completed_results', {}).copy()
    available_agents = state.get('available_squad_agents', [])
    current_stage_index = state.get("current_stage_index", 0)

    # --- Help Request / Failure Detection: Re-delegate to a fallback agent ---
    if _detect_task_failure(result_content):
        agent_tasks = assignments.get(source_agent, [])

        # Identify which specific task failed — match against the result content
        # The failed task is the one whose instruction appears in the failure message,
        # or if we can't tell, take the first assigned task
        failed_task = None
        for task in agent_tasks:
            task_instruction = task.split(":", 1)[1].strip() if ":" in task else task
            # Check if the original task instruction is referenced in the response
            if task_instruction.lower()[:40] in result_content.lower():
                failed_task = task
                break
        if not failed_task and agent_tasks:
            failed_task = agent_tasks[0]  # Default to first task

        if failed_task and plan:
            # Remove only the failed task from assignments (keep other tasks for this agent)
            if source_agent in assignments:
                assignments[source_agent] = [t for t in assignments[source_agent] if t != failed_task]
                if not assignments[source_agent]:
                    del assignments[source_agent]

            # Find a fallback agent — prefer generalist since the specialist failed
            fallback_agent = None
            candidates = []
            for agent_entry in available_agents:
                agent_id = agent_entry.split(":")[0].strip() if ":" in agent_entry else agent_entry.strip()
                if agent_id != source_agent and agent_id != "squad-lead-agent":
                    candidates.append(agent_id)
            # Prefer generalist for re-delegation (specialist already failed)
            for c in candidates:
                if "generalist" in c:
                    fallback_agent = c
                    break
            if not fallback_agent and candidates:
                fallback_agent = candidates[0]

            if fallback_agent:
                # Extract instruction from original task
                instruction = failed_task.split(":", 1)[1].strip() if ":" in failed_task else failed_task
                new_task = f"{fallback_agent}: {instruction}"

                # Rebuild the current stage: remove only the failed task, add the re-delegated one
                current_stage = plan[0]
                new_stage = [t for t in current_stage if t != failed_task]
                new_stage.append(new_task)
                new_plan = [new_stage] + plan[1:]

                return {
                    "assignments": assignments,
                    "plan": new_plan,
                    "squad_status": "EXECUTING",
                    "completed_results": completed_results,
                    "current_stage_index": current_stage_index,
                }

        # Fallback: no failed task found or no plan, just continue
        return {
            "assignments": assignments,
            "squad_status": "EXECUTING",
            "completed_results": completed_results,
            "current_stage_index": current_stage_index,
        }

    # --- Normal update: task completed successfully ---

    # Store the completed result for context injection
    completed_results[source_agent] = result_content

    # Remove from assignments — remove ONE task for this agent (the completed one)
    if source_agent in assignments:
        agent_tasks = assignments[source_agent]
        if len(agent_tasks) > 1:
            # Multiple tasks — remove just the first (completed) one, keep the rest
            assignments[source_agent] = agent_tasks[1:]
        else:
            del assignments[source_agent]

    # Remove from plan — remove only this agent's completed task from the current stage
    result = {
        "assignments": assignments,
        "squad_status": "EXECUTING",
        "completed_results": completed_results,
        "current_stage_index": current_stage_index,
    }

    if plan:
        current_stage = plan[0]
        # Remove one task for this agent from the stage
        removed = False
        new_stage = []
        for t in current_stage:
            if not removed and t.startswith(f"{source_agent}:"):
                removed = True  # Remove only the first matching task
                continue
            new_stage.append(t)

        if not new_stage:
            # Stage completed — advance to next stage
            new_plan = plan[1:]
            result["current_stage_index"] = current_stage_index + 1
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

def _build_workflow() -> StateGraph:
    """Construct the LangGraph workflow (without compiling)."""
    wf = StateGraph(SquadState)

    wf.add_node("monitor", node_monitor)
    wf.add_node("planner", node_planner)
    wf.add_node("updater", node_updater)
    wf.add_node("executor", node_executor)

    wf.set_entry_point("monitor")

    wf.add_conditional_edges("monitor", router)
    wf.add_edge("planner", "executor")
    wf.add_edge("updater", "executor")
    wf.add_edge("executor", END)

    return wf


# Default compile with MemorySaver (backward compatible — works without Postgres)
brain = _build_workflow().compile(checkpointer=MemorySaver())


async def initialize_persistent_brain(database_url: str) -> None:
    """Re-compile the brain with a PostgreSQL-backed checkpointer.

    This replaces the module-level ``brain`` variable so that every
    call-site that accesses ``brain_module.brain`` picks up the new
    persistent graph.
    """
    global brain

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    checkpointer = AsyncPostgresSaver.from_conn_string(database_url)
    await checkpointer.setup()  # auto-creates checkpoint tables if missing

    brain = _build_workflow().compile(checkpointer=checkpointer)
    logger.info("Brain re-compiled with PostgreSQL checkpointer")

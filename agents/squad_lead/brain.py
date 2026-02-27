import logging
import operator
import os
import re
from typing import Annotated, Any, Callable, List, TypedDict, Dict, Literal, Optional
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import SystemMessage, BaseMessage
# from langchain_core.tools import tool
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
    squad_status: Literal["IDLE", "PLANNING", "EXECUTING", "BLOCKED", "DONE", "UPDATING", "NEEDS_AUGMENTATION", "WAITING_ASYNC", "WAITING_HITL"]
    next_actions: List[Dict[str, str]]
    # Track completed task results for context injection into subsequent stages
    completed_results: Dict[str, str]  # agent_id -> result content
    current_stage_index: int
    capability_gaps: List[str]  # capabilities missing from the current squad
    pending_blockers: List[Dict[str, str]]  # blockers preventing session completion


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

    current = state.get("squad_status")

    if current == "NEEDS_AUGMENTATION":
        # A new agent joined to fill the gap — re-plan with the augmented squad
        if "has joined session" in str(last_msg.content):
            result = {"squad_status": "PLANNING"}
        else:
            result = {"squad_status": "NEEDS_AUGMENTATION"}
    elif current == "UPDATING":
        result = {"squad_status": "UPDATING"}
    elif "having difficulty" in str(last_msg.content) or "need help" in str(last_msg.content).lower():
        result = {"squad_status": "UPDATING"}
    elif "MISSION" in str(last_msg.content) or "Please" in str(last_msg.content):
        result = {"squad_status": "PLANNING"}
    else:
        # If currently EXECUTING, we don't switch to IDLE automatically essentially blocking the loop
        if current not in ["EXECUTING", "UPDATING", "PLANNING", "WAITING_ASYNC", "WAITING_HITL"]:
            result = {"squad_status": "IDLE"}

    return result


def node_planner(state: SquadState):
    """
    The Strategic Thinker. Looks at history and generates/updates the DAG.
    """
    prompt = f"""
    You are the Squad Lead — a strategic COORDINATOR agent responsible for breaking complex user requests into executable plans.

    YOUR ROLE: Decompose tasks and delegate them to specialist worker agents. You NEVER perform tasks yourself.

    AVAILABLE AGENTS (format: "agent-id: description of capabilities"):
    {chr(10).join('- ' + a for a in state['available_squad_agents'])}

    CURRENT PLAN: {state.get('plan', [])}
    LATEST REQUEST: {state['messages'][-1].content}
    
    PLANNING RULES:
    1. You are ONLY a coordinator — NEVER assign tasks to yourself (squad-lead-agent).
    2. Delegate ALL work to the available worker agents listed above.
    3. MATCH each task to the agent whose DESCRIPTION best fits that task. Read descriptions carefully — do not assume capabilities.
    4. If NO specialist matches a task, assign it to the most general-purpose agent available.
    5. Tasks within the same STAGE run in PARALLEL — group independent tasks together.
    6. Tasks in later STAGES depend on results from earlier stages — use stages for sequential dependencies.
    7. Use ONLY the exact agent-id from the list above (the part before the colon).
    8. Keep the plan efficient — use the fewest stages necessary, but do NOT collapse stages at the expense of quality. A specialist retrieving raw data and a generalist presenting it to the user are always two separate concerns.
    9. Each instruction must be self-contained and specific enough for the worker to execute without guessing.
    10. When the final output is a user-facing summary, report, or explanation, ALWAYS add a final stage assigning the generalist agent to present the results clearly — even if an earlier specialist produced a raw version.

    ERROR HANDLING:
    11. If the request is ambiguous, create a single-stage plan with the generalist agent asking the user for clarification.
    12. If no available agent can handle the request, output a single stage with the generalist agent explaining the limitation.

    OUTPUT FORMAT (strict):
    STAGE
    agent-id: clear, specific instruction
    agent-id: clear, specific instruction
    STAGE
    agent-id [needs: source-agent-id-1, source-agent-id-2]: instruction that depends on their results
    agent-id: instruction that needs no prior context

    The [needs: ...] annotation is REQUIRED for tasks in Stage 2+ that depend on results from earlier stages.
    List ONLY the agent IDs whose output is actually needed — do not include all previous agents.
    Omit [needs: ...] entirely for tasks that are independent (e.g., Stage 1 tasks, or parallel tasks with no dependency).

    EXAMPLE:
    STAGE
    accounting-specialist-agent: Convert 1000 USD to MYR using the current exchange rate
    STAGE
    generalist-simple-logic-agent [needs: accounting-specialist-agent]: Summarize the conversion result in a user-friendly sentence

    CAPABILITY GAP DETECTION:
    13. Before finalizing the plan, check that every required agent type is present in AVAILABLE AGENTS.
    14. If Rule 10 requires a generalist/presenter agent for user-facing output but NO such agent appears in AVAILABLE AGENTS, do NOT force the task onto a specialist. Instead output ONLY this exact format (no STAGE lines):
        NEEDS_AUGMENTATION
        missing: general-purpose agent for formatting and presenting results to end users

    TONE: Decisive, efficient, and precise. You are a mission planner, not a conversationalist.
    """

    response = llm.invoke([SystemMessage(content=prompt)])

    response_text = response.content.strip()

    # Check for capability gap signal before normal parsing
    if response_text.startswith("NEEDS_AUGMENTATION"):
        missing = ""
        for line in response_text.split("\n"):
            line = line.strip()
            if line.startswith("missing:"):
                missing = line.split(":", 1)[1].strip()
                break
        return {
            "plan": [],
            "squad_status": "NEEDS_AUGMENTATION",
            "capability_gaps": [missing] if missing else ["general-purpose presentation agent"],
            "current_stage_index": 0,
        }

    # Parse the LLM response into List[List[str]]
    # Each task line may contain a [needs: agent-1, agent-2] annotation
    # which is preserved in the plan for the executor to use
    raw_lines = response_text.split("\n")
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

    # Build the set of agent IDs that are actually in this session's squad
    session_agent_ids = {
        (a.split(":")[0].strip() if ":" in a else a.strip())
        for a in state.get("available_squad_agents", [])
    }

    # Regex to extract [needs: agent-1, agent-2] annotation from task lines
    _needs_pattern = re.compile(r'\[needs:\s*([^\]]+)\]')

    # Track which tasks in this stage are already assigned (by full task string)
    already_assigned_tasks = set()
    for task_list in assignments.values():
        already_assigned_tasks.update(task_list)

    for task in current_stage:
        if ":" not in task:
            continue

        # Parse: "agent-id [needs: dep-1, dep-2]: instruction"
        # The first colon after the agent-id (and optional [needs:]) splits target from instruction
        needs_match = _needs_pattern.search(task)
        needed_agents = []
        if needs_match:
            needed_agents = [a.strip() for a in needs_match.group(1).split(",")]
            # Remove the [needs: ...] from the task line before splitting on ":"
            clean_task = task[:needs_match.start()] + task[needs_match.end():]
        else:
            clean_task = task

        if ":" not in clean_task:
            continue

        target_id, instruction = clean_task.split(":", 1)
        target_id = target_id.strip()

        # Guard: if the planner assigned a task to an agent that was never invited
        # to the session, do NOT dispatch directly. Instead, surface a capability gap
        # so the Director can properly invite the agent via the augmentation flow.
        if target_id != "squad-lead-agent" and target_id not in session_agent_ids:
            logger.warning(
                f"Executor blocked dispatch to '{target_id}' — agent is not a session member. "
                f"Triggering augmentation request."
            )
            return {
                "plan": plan,
                "next_actions": [],
                "squad_status": "NEEDS_AUGMENTATION",
                "capability_gaps": [
                    f"agent '{target_id}' required for: {instruction.strip()[:120]}"
                ],
                "current_stage_index": current_stage_index,
            }

        if task not in already_assigned_tasks:
            final_instruction = instruction.strip()

            # Build targeted context: only inject results from agents this task depends on
            if needed_agents and completed_results:
                context_lines = ["## Context from Previous Stage:\n"]
                for dep_id in needed_agents:
                    if dep_id in completed_results:
                        result = completed_results[dep_id]
                        truncated = result[:2000] + "..." if len(result) > 2000 else result
                        context_lines.append(f"### {dep_id}:\n{truncated}\n")
                if len(context_lines) > 1:  # Has actual results beyond the header
                    context_block = "\n".join(context_lines) + "\n---\n\n"
                    final_instruction = f"{context_block}**Your Task:** {final_instruction}"

            new_actions.append({
                "target": target_id,
                "instruction": final_instruction,
                "context_provided": bool(needed_agents and completed_results),
            })
            # Append to the agent's task list (supports multiple tasks per agent)
            if target_id not in new_assignments:
                new_assignments[target_id] = []
            new_assignments[target_id].append(task)

    # Filter out self-assignments (squad-lead-agent should never assign to itself)
    filtered_actions = []
    for action in new_actions:
        if action["target"] == "squad-lead-agent":
            # Try to find a fallback worker
            fallback = None
            for agent_entry in state.get('available_squad_agents', []):
                aid = agent_entry.split(":")[0].strip() if ":" in agent_entry else agent_entry.strip()
                if aid != "squad-lead-agent":
                    if not fallback or "generalist" in aid:
                        fallback = aid
            if fallback:
                action["target"] = fallback
                filtered_actions.append(action)
                logger.warning(f"Self-assignment redirected to {fallback}")
            else:
                logger.error("Self-assignment with no fallback agent available")
        else:
            filtered_actions.append(action)

    new_actions = filtered_actions

    # If all actions were self-assignments and no fallback exists → BLOCKED
    if not new_actions and not plan:
        return {
            "next_actions": [],
            "squad_status": "BLOCKED",
            "assignments": new_assignments,
            "current_stage_index": current_stage_index,
        }

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


# --- 4b. Session Continuity Check ---

async def node_continuity_check(state: SquadState, config: dict):
    """Deterministic gate that queries the DB for pending blockers before
    allowing the session to finalize as DONE.  No LLM call — pure DB query.

    The actual query function is injected via ``config["configurable"]["continuity_checker"]``
    so the node stays testable without a real database.
    """
    if state.get("squad_status") != "DONE":
        return {}

    configurable = config.get("configurable", {})
    checker: Optional[Callable] = configurable.get("continuity_checker")

    if checker is None:
        # No checker injected (e.g. in-memory mode) — let DONE pass through
        return {}

    conversation_id = configurable.get("thread_id", "")
    blockers: List[Dict[str, Any]] = await checker(conversation_id)

    if not blockers:
        return {}

    has_hitl = any(b.get("type") == "hitl" for b in blockers)
    new_status = "WAITING_HITL" if has_hitl else "WAITING_ASYNC"

    return {
        "squad_status": new_status,
        "pending_blockers": [
            {"type": b.get("type", ""), "id": b.get("id", ""), "title": b.get("title", ""), "status": b.get("status", "")}
            for b in blockers
        ],
    }


# --- 5. The Routers ---

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
    elif status == "NEEDS_AUGMENTATION":
        next_node = END  # main.py handles the Director callback
    elif status in ("WAITING_ASYNC", "WAITING_HITL"):
        next_node = END  # don't re-process while waiting

    return next_node


def planner_router(state: SquadState):
    """After the planner runs, skip the executor if a capability gap was detected."""
    if state.get("squad_status") == "NEEDS_AUGMENTATION":
        return END
    return "executor"


# --- 6. Build the Graph ---

def _build_workflow() -> StateGraph:
    """Construct the LangGraph workflow (without compiling)."""
    wf = StateGraph(SquadState)

    wf.add_node("monitor", node_monitor)
    wf.add_node("planner", node_planner)
    wf.add_node("updater", node_updater)
    wf.add_node("executor", node_executor)
    wf.add_node("continuity_check", node_continuity_check)

    wf.set_entry_point("monitor")

    wf.add_conditional_edges("monitor", router)
    wf.add_conditional_edges("planner", planner_router)
    wf.add_edge("updater", "executor")
    wf.add_edge("executor", "continuity_check")
    wf.add_edge("continuity_check", END)

    return wf


# Default compile with MemorySaver (backward compatible — works without Postgres)
brain = _build_workflow().compile(checkpointer=MemorySaver())

# Hold a reference to the connection pool so it can be closed on shutdown
_pg_pool = None


async def initialize_persistent_brain(database_url: str) -> None:
    """Re-compile the brain with a PostgreSQL-backed checkpointer.

    This replaces the module-level ``brain`` variable so that every
    call-site that accesses ``brain_module.brain`` picks up the new
    persistent graph.
    """
    global brain, _pg_pool

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    _pg_pool = AsyncConnectionPool(
        conninfo=database_url,
        open=False,
        kwargs={"autocommit": True},
    )
    await _pg_pool.open()

    checkpointer = AsyncPostgresSaver(_pg_pool)
    await checkpointer.setup()  # auto-creates checkpoint tables if missing

    brain = _build_workflow().compile(checkpointer=checkpointer)
    logger.info("Brain re-compiled with PostgreSQL checkpointer")


async def close_persistent_brain() -> None:
    """Close the PostgreSQL connection pool."""
    global _pg_pool
    if _pg_pool:
        await _pg_pool.close()
        _pg_pool = None
        logger.info("PostgreSQL checkpointer pool closed")

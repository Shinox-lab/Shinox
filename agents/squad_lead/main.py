"""
Squad Lead Agent - Orchestrator

The leader of the agent squad. Responsible for planning, coordination,
and delegation of tasks to specialized agents.
Uses the Shinox Agent SDK with custom session handler for complex orchestration.
"""

import asyncio
import logging
import os
from typing import Optional
import httpx

from langchain_core.messages import HumanMessage
from shinox_agent import ShinoxAgent, AgentMessage, A2AHeaders
import brain as brain_module
from assignment_store import AssignmentStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)

logger = logging.getLogger(__name__)

# --- Configuration ---
DATABASE_URL = os.getenv("DATABASE_URL")
TASK_TIMEOUT_SECONDS = int(os.getenv("SQUAD_TASK_TIMEOUT", "120"))
CHECK_INTERVAL_SECONDS = int(os.getenv("SQUAD_CHECK_INTERVAL", "30"))
COLLABORATION_GRACE_SECONDS = int(os.getenv("SQUAD_COLLABORATION_GRACE", "90"))
DIRECTOR_URL = os.getenv("DIRECTOR_URL", "http://director:8000")

# --- Persistent state (initialised at startup when DATABASE_URL is set) ---
_assignment_store: Optional[AssignmentStore] = None
_monitor_task: Optional[asyncio.Task] = None

# --- Agent Definition ---
plan_skill = AgentSkill(
    id="squad_planning_skill",
    name="Squad Planning Skill",
    description="Breaks down complex user requests into executable stages and tasks for other agents.",
    tags=["planning", "coordination", "strategy"],
    examples=[
        "Create a plan to audit the financial report",
        "Coordinate the response to the security incident",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

delegate_skill = AgentSkill(
    id="task_delegation_skill",
    name="Task Delegation Skill",
    description="Assigns specific tasks to appropriate agents based on their capabilities.",
    tags=["delegation", "management", "assignment"],
    examples=[
        "Assign the report generation to the Reporter Agent",
        "Instruct the Currency Agent to convert the totals",
    ],
    input_modes=["text"],
    output_modes=["text"],
)

agent_card = AgentCard(
    name="Squad Lead Agent",
    description="The leader of the agent squad. Responsible for planning, coordination, and delegation of tasks to specialized agents.",
    url="http://localhost:8001/",
    version="1.0.0",
    capabilities=AgentCapabilities(
        streaming=True,
        push_notifications=True,
    ),
    skills=[plan_skill, delegate_skill],
    default_input_modes=["text"],
    default_output_modes=["text"],
)

triggers = ["MISSION", "Please", "help", "coordinate"]


# --- Handlers ---

async def handle_brain_output(final_state: dict, initial_status: str, headers: A2AHeaders, agent: ShinoxAgent):
    """Handles the output from the brain: plan announcements, task dispatch, completion."""
    squad_status = final_state.get("squad_status", "IDLE")
    current_plan = final_state.get("plan", [])
    next_actions = final_state.get("next_actions", [])
    conversation_id = headers.conversation_id
    current_stage_index = final_state.get("current_stage_index", 0)

    # 1. Announce New Plan
    if initial_status != "UPDATING" and squad_status == "EXECUTING" and current_plan:
        plan_str = "\n".join([f"- Stage {i + 1}: {stage}" for i, stage in enumerate(current_plan)])
        content = f"**Plan Formulated**\n\nI have analyzed the mission and created the following execution plan:\n\n{plan_str}\n\nProceeding with execution..."
        await agent.publish_update(content, conversation_id, "INFO_UPDATE")

    # 1b. Announce stage completion when advancing to a new stage
    if current_stage_index > 0 and next_actions and initial_status == "UPDATING":
        await agent.publish_update(
            f"**Stage {current_stage_index} Complete** — advancing to stage {current_stage_index + 1}.",
            conversation_id,
            "INFO_UPDATE",
        )

    # 2. Dispatch Tasks
    if next_actions:
        # Announce actions (with re-delegation notice if applicable)
        action_descriptions = []
        for action in next_actions:
            target = action.get('target', 'unknown')
            instruction = action.get('instruction', 'unknown')
            action_descriptions.append(f"- **{target}**: {instruction}")

        if initial_status == "UPDATING" and next_actions:
            # Distinguish: was a PEER_REQUEST (actual difficulty) or a TASK_RESULT (stage advancing)?
            source_type = headers.interaction_type if hasattr(headers, 'interaction_type') else ""
            if source_type == "PEER_REQUEST":
                announcement = "**Squad Update: Re-delegating Tasks**\nA worker reported difficulty. Re-assigning to an available agent:\n" + "\n".join(action_descriptions)
            else:
                announcement = "**Squad Update: Advancing to Next Stage**\n" + "\n".join(action_descriptions)
        else:
            announcement = "**Squad Update: Dispatching Tasks**\n" + "\n".join(action_descriptions)
        await agent.publish_update(announcement, conversation_id, "INFO_UPDATE")

        # Send tasks
        for action in next_actions:
            target_agent = action['target']
            instruction = action['instruction']

            # Defense-in-depth: skip self-assignments that slipped through the brain filter
            if target_agent == agent.agent_id:
                logger.warning(f"Skipping self-assignment: {instruction[:100]}")
                continue

            target_topic = await agent.resolve_agent_inbox(target_agent)

            task_headers = A2AHeaders(
                source_agent_id=agent.agent_id,
                target_agent_id=target_agent,
                interaction_type="TASK_ASSIGNMENT",
                conversation_id=conversation_id
            )

            # Signal to worker that context is already injected (skip auto-history)
            task_metadata = {}
            if action.get("context_provided"):
                task_metadata["context_provided"] = True

            await agent.broker.publish(
                AgentMessage(content=instruction, headers=task_headers, metadata=task_metadata),
                topic=target_topic,
                headers={
                    "x-source-agent": agent.agent_id,
                    "x-dest-topic": target_topic,
                    "x-interaction-type": "TASK_ASSIGNMENT",
                    "x-conversation-id": conversation_id
                }
            )

            # Track assignment for timeout monitoring
            if _assignment_store:
                await _assignment_store.track_assignment(
                    conversation_id, target_agent, instruction
                )

    # 3. Handle Completion or Blocked
    if squad_status == "DONE":
        content = (
            "## Mission Accomplished\n\n"
            "The Squad Lead has confirmed that all planned tasks have been executed successfully.\n\n"
            "**Summary:**\n"
            "- All stages in the execution plan are complete.\n"
            "- No pending actions remain.\n\n"
            "Closing out this session context. Ready for new missions!"
        )
        await agent.publish_update(content, conversation_id, "SQUAD_COMPLETION")

    elif squad_status == "BLOCKED":
        content = (
            "## Squad Blocked\n\n"
            "I am unable to proceed with the current plan.\n"
            "Please provide guidance, clarify the mission, or check agent availability."
        )
        await agent.publish_update(content, conversation_id, "INFO_UPDATE")

    elif squad_status == "WAITING_ASYNC":
        pending = final_state.get("pending_blockers", [])
        task_summary = ", ".join(
            f"{b.get('title', 'unknown')} ({b.get('status', '?')})" for b in pending
        ) or "pending worker tasks"
        content = (
            "## Waiting for Async Tasks\n\n"
            f"All planned stages are complete, but **{len(pending)}** task(s) are still in-flight:\n"
            f"{task_summary}\n\n"
            "Session will resume automatically when results arrive."
        )
        await agent.publish_update(content, conversation_id, "INFO_UPDATE")
        if _assignment_store:
            await _assignment_store.update_group_chat_status(conversation_id, "WAITING_ASYNC")

    elif squad_status == "WAITING_HITL":
        pending = final_state.get("pending_blockers", [])
        hitl_items = [b for b in pending if b.get("type") == "hitl"]
        hitl_summary = ", ".join(
            f"{b.get('title', 'unknown')}" for b in hitl_items
        ) or "pending HITL approval(s)"
        content = (
            "## Waiting for Human Approval\n\n"
            f"All planned stages are complete, but **{len(hitl_items)}** HITL request(s) need human review:\n"
            f"{hitl_summary}\n\n"
            "Session will resume when approvals are granted."
        )
        await agent.publish_update(content, conversation_id, "INFO_UPDATE")
        if _assignment_store:
            await _assignment_store.update_group_chat_status(conversation_id, "WAITING_HITL")

    elif squad_status == "NEEDS_AUGMENTATION":
        gaps = final_state.get("capability_gaps", ["general-purpose presentation agent"])
        gap_str = ", ".join(gaps)
        await agent.publish_update(
            f"**Squad Capability Gap Detected**\n\n"
            f"The current squad cannot complete the mission. Missing: **{gap_str}**.\n\n"
            f"Requesting Director to find a suitable agent from the full registry...",
            conversation_id,
            "INFO_UPDATE",
        )
        current_squad = [a.split(":")[0].strip() for a in final_state.get("available_squad_agents", [])]
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{DIRECTOR_URL}/augment",
                    json={
                        "session_id": conversation_id,
                        "required_capabilities": gaps,
                        "current_squad_members": current_squad,
                    },
                    timeout=15.0,
                )
            logger.info(f"[{agent.agent_id}] Augmentation request sent to Director for {conversation_id}")
        except Exception as e:
            logger.error(f"[{agent.agent_id}] Failed to reach Director for augmentation: {e}")
            await agent.publish_update(
                "**Warning:** Could not reach Director to request squad augmentation. Mission may stall.",
                conversation_id,
                "INFO_UPDATE",
            )

    # 4. Fallback: Generic Response
    messages = final_state.get("messages", [])
    if messages and not current_plan and not next_actions and squad_status == "IDLE":
        last_msg = messages[-1]
        if hasattr(last_msg, 'content') and last_msg.content:
            if not isinstance(last_msg, HumanMessage):
                await agent.publish_update(last_msg.content, conversation_id, "INFO_UPDATE")


async def session_event_handler(msg: AgentMessage, agent: ShinoxAgent):
    """The Selective Attention Mechanism for Squad Lead."""
    headers = msg.headers
    my_id = agent.agent_id

    # Session Filtering
    if headers.conversation_id not in agent.active_sessions:
        return

    # Loop Prevention: Ignore own messages
    if headers.source_agent_id == my_id:
        return

    # PEER_RESPONSE passthrough — worker-to-worker traffic, don't interfere
    if headers.interaction_type == "PEER_RESPONSE":
        return

    # PEER_REQUEST grace period — if the source agent is tracked, defer re-delegation
    # and let the peer collaboration play out
    if headers.interaction_type == "PEER_REQUEST":
        if _assignment_store:
            entry = await _assignment_store.get_assignment(
                headers.conversation_id, headers.source_agent_id
            )
            if entry:
                await _assignment_store.grant_collaboration_grace(
                    headers.conversation_id, headers.source_agent_id
                )
                logger.info(
                    f"[{my_id}] Peer collaboration in progress for {headers.source_agent_id}, "
                    f"granting {COLLABORATION_GRACE_SECONDS}s grace period"
                )
                print(
                    f"[{my_id}] Deferring on PEER_REQUEST from {headers.source_agent_id} "
                    f"(collaboration in progress)"
                )
                return

    # Passive Memory Update — enrich low-confidence TASK_RESULT with failure hint
    # so the brain's node_updater detects it as a failure even if content phrasing varies
    msg_content = msg.content
    if headers.interaction_type == "TASK_RESULT":
        confidence = msg.metadata.get("confidence")
        if confidence is not None and confidence < 0.4:
            msg_content = (
                f"{msg.content}\n\n"
                f"[SYSTEM NOTE: This agent is having difficulty with a task. "
                f"Confidence: {confidence}. Consider re-delegating to another agent.]"
            )
            logger.info(
                f"[{my_id}] Low-confidence TASK_RESULT from {headers.source_agent_id} "
                f"(confidence={confidence}), injecting failure hint"
            )

    lc_msg = HumanMessage(content=msg_content, name=headers.source_agent_id)

    # Active Trigger Check
    should_wake_up = False

    if headers.target_agent_id == my_id:
        should_wake_up = True
    if f"@{my_id}" in msg.content:
        should_wake_up = True
    if headers.interaction_type == "TASK_RESULT":
        should_wake_up = True
    if headers.interaction_type == "SESSION_BRIEFING":
        should_wake_up = True
    if headers.interaction_type == "AGENT_JOINED":
        should_wake_up = True
    if headers.interaction_type == "PEER_REQUEST":
        should_wake_up = True
    if headers.interaction_type == "GROUP_QUERY":
        should_wake_up = True
    if headers.interaction_type == "HITL_RESOLVED":
        should_wake_up = True
    for trigger in agent.triggers:
        if trigger in msg.content:
            should_wake_up = True
            break

    if not should_wake_up:
        print(f"[{my_id}] Ignoring message from {headers.source_agent_id} (type: {headers.interaction_type})")
        return

    print(f"[{my_id}] Waking up for message from {headers.source_agent_id} (type: {headers.interaction_type})")

    # Clear assignment tracking when we receive a result from an agent
    if headers.interaction_type == "TASK_RESULT":
        if _assignment_store:
            await _assignment_store.remove_assignment(
                headers.conversation_id, headers.source_agent_id
            )
            logger.debug(f"[{my_id}] Cleared tracker for {headers.source_agent_id}")

    # --- Invoke the Brain ---

    config = {"configurable": {"thread_id": headers.conversation_id}}
    if _assignment_store:
        config["configurable"]["continuity_checker"] = _assignment_store.get_pending_blockers

    # 1. Check existing state
    current_state = await brain_module.brain.aget_state(config)
    existing_agents = current_state.values.get("available_squad_agents", []) if current_state and current_state.values else []
    current_squad_status = current_state.values.get("squad_status") if current_state and current_state.values else None

    squad_agents = []

    # 2. Check for Briefing with Roster from Director
    if headers.interaction_type == "SESSION_BRIEFING" and msg.metadata.get("squad_members"):
        # Director sends bare IDs — enrich with descriptions from registry
        # so the planner LLM knows each agent's capabilities
        briefing_ids = set(msg.metadata["squad_members"])
        all_agents = await agent.fetch_available_agents(exclude_self=False)
        squad_agents = [a for a in all_agents if a.split(":")[0].strip() in briefing_ids]
        # Include any briefing IDs not found in registry (shouldn't happen, but safe)
        found_ids = {a.split(":")[0].strip() for a in squad_agents}
        for aid in briefing_ids - found_ids:
            squad_agents.append(aid)

    # 3. Use existing state if available
    elif existing_agents:
        squad_agents = existing_agents

    # 4. Fallback: Fetch all active agents
    else:
        squad_agents = await agent.fetch_available_agents()

    # Determine state inputs
    # Special case: a new agent just joined to fill a capability gap — augment the squad and re-plan
    if headers.interaction_type == "AGENT_JOINED" and current_squad_status == "NEEDS_AUGMENTATION":
        new_agent_id = headers.source_agent_id
        existing_ids = {a.split(":")[0].strip() for a in squad_agents}
        if new_agent_id and new_agent_id not in existing_ids:
            all_agents = await agent.fetch_available_agents(exclude_self=False)
            matched = next(
                (a for a in all_agents if a.split(":")[0].strip() == new_agent_id),
                new_agent_id,
            )
            squad_agents = list(squad_agents) + [matched]
        state_input = {
            "messages": [lc_msg],
            "available_squad_agents": squad_agents,
            "squad_status": "PLANNING",
            "capability_gaps": [],
        }
        initial_status = "IDLE"
    else:
        state_input = {
            "messages": [lc_msg],
            "available_squad_agents": squad_agents,
        }
        if headers.interaction_type == "TASK_RESULT":
            state_input["squad_status"] = "UPDATING"
        if headers.interaction_type in ("PEER_REQUEST", "GROUP_QUERY"):
            state_input["squad_status"] = "UPDATING"
        if headers.interaction_type == "HITL_RESOLVED":
            state_input["squad_status"] = "UPDATING"
        initial_status = "UPDATING" if headers.interaction_type in ("TASK_RESULT", "PEER_REQUEST", "GROUP_QUERY", "HITL_RESOLVED") else "IDLE"

    # Run the graph
    try:
        final_state = await brain_module.brain.ainvoke(state_input, config=config)
    except Exception as e:
        logger.error(f"[{my_id}] Brain invocation failed: {e}", exc_info=True)
        await agent.publish_update(
            f"Squad Lead encountered an internal error while processing: {e}",
            headers.conversation_id,
            "INFO_UPDATE"
        )
        return

    # --- Dispatch Actions ---
    await handle_brain_output(final_state, initial_status, headers, agent)


# --- Timeout Monitor ---

async def _task_timeout_monitor(agent_instance: ShinoxAgent):
    """Background task that monitors assigned tasks for timeouts using the persistent store."""
    logger.info(f"[{agent_instance.agent_id}] Task timeout monitor started "
                f"(timeout: {TASK_TIMEOUT_SECONDS}s, check interval: {CHECK_INTERVAL_SECONDS}s)")

    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

            if not _assignment_store:
                continue

            # First timeout: assignments that haven't been checked yet
            first_check = await _assignment_store.get_timed_out_first_check(TASK_TIMEOUT_SECONDS)
            for row in first_check:
                agent_id = row["agent_id"]
                conversation_id = row["conversation_id"]
                logger.info(
                    f"[{agent_instance.agent_id}] Task timeout for {agent_id} "
                    f"in conversation {conversation_id}. Sending check-in."
                )
                try:
                    target_topic = await agent_instance.resolve_agent_inbox(agent_id)
                    checkin_headers = A2AHeaders(
                        source_agent_id=agent_instance.agent_id,
                        target_agent_id=agent_id,
                        interaction_type="DIRECT_COMMAND",
                        conversation_id=conversation_id,
                    )
                    await agent_instance.broker.publish(
                        AgentMessage(
                            content="How is the task going? Please provide a status update.",
                            headers=checkin_headers,
                        ),
                        topic=target_topic,
                        headers={
                            "x-source-agent": agent_instance.agent_id,
                            "x-dest-topic": target_topic,
                            "x-interaction-type": "DIRECT_COMMAND",
                            "x-conversation-id": conversation_id,
                        },
                    )
                    await _assignment_store.mark_check_sent(conversation_id, agent_id)
                except Exception as e:
                    logger.warning(f"[{agent_instance.agent_id}] Check-in send failed: {e}")

            # Second timeout: trigger re-delegation
            second_check = await _assignment_store.get_timed_out_second_check(TASK_TIMEOUT_SECONDS)
            for row in second_check:
                agent_id = row["agent_id"]
                conversation_id = row["conversation_id"]
                instruction = row["instruction"]
                logger.warning(
                    f"[{agent_instance.agent_id}] Agent {agent_id} unresponsive "
                    f"in conversation {conversation_id}. Triggering re-delegation."
                )
                try:
                    config = {"configurable": {"thread_id": conversation_id}}
                    timeout_msg = HumanMessage(
                        content=(
                            f"Agent {agent_id} is having difficulty with a task and is unresponsive. "
                            f"Original task: {instruction}"
                        ),
                        name=agent_id,
                    )
                    state_input = {
                        "messages": [timeout_msg],
                        "squad_status": "UPDATING",
                    }
                    final_state = await brain_module.brain.ainvoke(state_input, config=config)

                    synthetic_headers = A2AHeaders(
                        source_agent_id=agent_id,
                        interaction_type="PEER_REQUEST",
                        conversation_id=conversation_id,
                    )
                    await handle_brain_output(final_state, "UPDATING", synthetic_headers, agent_instance)

                    await _assignment_store.remove_assignment(conversation_id, agent_id)
                except Exception as e:
                    logger.error(f"[{agent_instance.agent_id}] Re-delegation failed: {e}")
                    await _assignment_store.remove_assignment(conversation_id, agent_id)

            # Expired HITL requests: auto-resolve by feeding through brain
            expired_hitl = await _assignment_store.get_expired_hitl_requests()
            for row in expired_hitl:
                hitl_id = row["id"]
                conversation_id = row["conversation_id"]
                agent_id = row.get("agent_id", "unknown")
                title = row.get("title", "HITL request")
                logger.warning(
                    f"[{agent_instance.agent_id}] HITL request {hitl_id} expired "
                    f"in conversation {conversation_id}. Synthesizing update."
                )
                try:
                    config = {"configurable": {"thread_id": conversation_id}}
                    if _assignment_store:
                        config["configurable"]["continuity_checker"] = _assignment_store.get_pending_blockers
                    expired_msg = HumanMessage(
                        content=(
                            f"HITL request '{title}' (id: {hitl_id}) has expired without human approval. "
                            f"Original agent: {agent_id}. Please re-evaluate session status."
                        ),
                        name=agent_id,
                    )
                    state_input = {
                        "messages": [expired_msg],
                        "squad_status": "UPDATING",
                    }
                    final_state = await brain_module.brain.ainvoke(state_input, config=config)

                    synthetic_headers = A2AHeaders(
                        source_agent_id=agent_id,
                        interaction_type="TASK_RESULT",
                        conversation_id=conversation_id,
                    )
                    await handle_brain_output(final_state, "UPDATING", synthetic_headers, agent_instance)
                except Exception as e:
                    logger.error(f"[{agent_instance.agent_id}] Expired HITL handling failed: {e}")

        except asyncio.CancelledError:
            logger.debug(f"[{agent_instance.agent_id}] Task timeout monitor cancelled")
            raise
        except Exception as e:
            logger.error(f"[{agent_instance.agent_id}] Timeout monitor error: {e}")


# --- Instantiate Agent ---
agent = ShinoxAgent(
    agent_card=agent_card,
    session_handler=session_event_handler,
    triggers=triggers
)
app = agent.app


# --- Lifecycle: Start/Stop ---

@app.on_startup
async def _on_startup():
    global _assignment_store, _monitor_task

    # 1. Initialise persistent assignment store (if DATABASE_URL is set)
    if DATABASE_URL:
        _assignment_store = AssignmentStore(DATABASE_URL)
        await _assignment_store.initialize()

        # 2. Re-compile brain with PostgreSQL checkpointer
        await brain_module.initialize_persistent_brain(DATABASE_URL)
    else:
        logger.warning("DATABASE_URL not set — running with in-memory state only (no persistence)")

    # 3. Start timeout monitor
    _monitor_task = asyncio.create_task(_task_timeout_monitor(agent))


@app.on_shutdown
async def _on_shutdown():
    global _monitor_task

    # Stop timeout monitor
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
        logger.info(f"[{agent.agent_id}] Task timeout monitor stopped")

    # Close brain's PostgreSQL pool
    await brain_module.close_persistent_brain()

    # Close assignment store pool
    if _assignment_store:
        await _assignment_store.close()


# --- Run ---
# faststream run main:app

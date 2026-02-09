"""
Squad Lead Agent - Orchestrator

The leader of the agent squad. Responsible for planning, coordination,
and delegation of tasks to specialized agents.
Uses the Shinox Agent SDK with custom session handler for complex orchestration.
"""

import asyncio
import logging
import os
import time
from typing import Dict, Optional

from langchain_core.messages import HumanMessage
from shinox_agent import ShinoxAgent, AgentMessage, A2AHeaders
from brain import brain
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)

logger = logging.getLogger(__name__)

# --- Assignment Tracking for Timeout Monitoring ---
# Structure: {conversation_id: {agent_id: {"assigned_at": float, "instruction": str, "check_sent": bool}}}
_assignment_tracker: Dict[str, Dict[str, Dict]] = {}
TASK_TIMEOUT_SECONDS = int(os.getenv("SQUAD_TASK_TIMEOUT", "120"))
CHECK_INTERVAL_SECONDS = int(os.getenv("SQUAD_CHECK_INTERVAL", "30"))
COLLABORATION_GRACE_SECONDS = int(os.getenv("SQUAD_COLLABORATION_GRACE", "90"))
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

    # 1. Announce New Plan
    if initial_status != "UPDATING" and squad_status == "EXECUTING" and current_plan:
        plan_str = "\n".join([f"- Stage {i + 1}: {stage}" for i, stage in enumerate(current_plan)])
        content = f"**Plan Formulated**\n\nI have analyzed the mission and created the following execution plan:\n\n{plan_str}\n\nProceeding with execution..."
        await agent.publish_update(content, conversation_id, "INFO_UPDATE")

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

            target_topic = await agent.resolve_agent_inbox(target_agent)

            task_headers = A2AHeaders(
                source_agent_id=agent.agent_id,
                target_agent_id=target_agent,
                interaction_type="TASK_ASSIGNMENT",
                conversation_id=conversation_id
            )

            await agent.broker.publish(
                AgentMessage(content=instruction, headers=task_headers),
                topic=target_topic,
                headers={
                    "x-source-agent": agent.agent_id,
                    "x-dest-topic": target_topic,
                    "x-interaction-type": "TASK_ASSIGNMENT",
                    "x-conversation-id": conversation_id
                }
            )

            # Track assignment for timeout monitoring
            if conversation_id not in _assignment_tracker:
                _assignment_tracker[conversation_id] = {}
            _assignment_tracker[conversation_id][target_agent] = {
                "assigned_at": time.time(),
                "instruction": instruction[:500],
                "check_sent": False,
            }

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
        conv_tracker = _assignment_tracker.get(headers.conversation_id, {})
        if headers.source_agent_id in conv_tracker:
            tracker_entry = conv_tracker[headers.source_agent_id]
            tracker_entry["assigned_at"] = time.time()  # Reset timer — grant grace period
            tracker_entry["collaboration_in_progress"] = True
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
        conv_tracker = _assignment_tracker.get(headers.conversation_id, {})
        if headers.source_agent_id in conv_tracker:
            del conv_tracker[headers.source_agent_id]
            logger.debug(f"[{my_id}] Cleared tracker for {headers.source_agent_id}")

    # --- Invoke the Brain ---

    config = {"configurable": {"thread_id": headers.conversation_id}}

    # 1. Check existing state
    current_state = await brain.aget_state(config)
    existing_agents = current_state.values.get("available_squad_agents", []) if current_state and current_state.values else []

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
    state_input = {
        "messages": [lc_msg],
        "available_squad_agents": squad_agents,
    }

    if headers.interaction_type == "TASK_RESULT":
        state_input["squad_status"] = "UPDATING"

    if headers.interaction_type in ("PEER_REQUEST", "GROUP_QUERY"):
        state_input["squad_status"] = "UPDATING"

    initial_status = "UPDATING" if headers.interaction_type in ("TASK_RESULT", "PEER_REQUEST", "GROUP_QUERY") else "IDLE"

    # Run the graph
    final_state = await brain.ainvoke(state_input, config=config)

    # --- Dispatch Actions ---
    await handle_brain_output(final_state, initial_status, headers, agent)


# --- Timeout Monitor ---

async def _task_timeout_monitor(agent_instance: ShinoxAgent):
    """Background task that monitors assigned tasks for timeouts."""
    global _monitor_task
    logger.info(f"[{agent_instance.agent_id}] Task timeout monitor started "
                f"(timeout: {TASK_TIMEOUT_SECONDS}s, check interval: {CHECK_INTERVAL_SECONDS}s)")

    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            now = time.time()

            for conversation_id, agents in list(_assignment_tracker.items()):
                for agent_id, info in list(agents.items()):
                    elapsed = now - info["assigned_at"]

                    # First timeout: send a check-in
                    if elapsed > TASK_TIMEOUT_SECONDS and not info["check_sent"]:
                        logger.info(
                            f"[{agent_instance.agent_id}] Task timeout for {agent_id} "
                            f"({elapsed:.0f}s elapsed). Sending check-in."
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
                            info["check_sent"] = True
                        except Exception as e:
                            logger.warning(f"[{agent_instance.agent_id}] Check-in send failed: {e}")

                    # Second timeout: trigger re-delegation
                    elif elapsed > TASK_TIMEOUT_SECONDS * 2 and info["check_sent"]:
                        logger.warning(
                            f"[{agent_instance.agent_id}] Agent {agent_id} unresponsive "
                            f"({elapsed:.0f}s elapsed). Triggering re-delegation."
                        )
                        try:
                            # Inject synthetic timeout message into brain
                            config = {"configurable": {"thread_id": conversation_id}}
                            timeout_msg = HumanMessage(
                                content=(
                                    f"Agent {agent_id} is having difficulty with a task and is unresponsive. "
                                    f"Original task: {info['instruction']}"
                                ),
                                name=agent_id,
                            )
                            state_input = {
                                "messages": [timeout_msg],
                                "squad_status": "UPDATING",
                            }
                            final_state = await brain.ainvoke(state_input, config=config)

                            # Build synthetic headers for dispatch
                            synthetic_headers = A2AHeaders(
                                source_agent_id=agent_id,
                                interaction_type="PEER_REQUEST",
                                conversation_id=conversation_id,
                            )
                            await handle_brain_output(final_state, "UPDATING", synthetic_headers, agent_instance)

                            # Remove from tracker
                            del agents[agent_id]
                        except Exception as e:
                            logger.error(f"[{agent_instance.agent_id}] Re-delegation failed: {e}")
                            # Remove to avoid infinite retries
                            del agents[agent_id]

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


# --- Lifecycle: Start/Stop Timeout Monitor ---

@app.on_startup
async def _start_timeout_monitor():
    global _monitor_task
    _monitor_task = asyncio.create_task(_task_timeout_monitor(agent))


@app.on_shutdown
async def _stop_timeout_monitor():
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        try:
            await _monitor_task
        except asyncio.CancelledError:
            pass
        logger.info(f"[{agent.agent_id}] Task timeout monitor stopped")


# --- Run ---
# faststream run main:app
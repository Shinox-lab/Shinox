"""
Squad Lead Agent - Orchestrator

The leader of the agent squad. Responsible for planning, coordination,
and delegation of tasks to specialized agents.
Uses the Shinox Agent SDK with custom session handler for complex orchestration.
"""

from langchain_core.messages import HumanMessage
from shinox_agent import ShinoxAgent, AgentMessage, A2AHeaders
from brain import brain
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)

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
        # Announce actions
        action_descriptions = []
        for action in next_actions:
            target = action.get('target', 'unknown')
            instruction = action.get('instruction', 'unknown')
            action_descriptions.append(f"- **{target}**: {instruction}")

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

    # Passive Memory Update
    lc_msg = HumanMessage(content=msg.content, name=headers.source_agent_id)

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
    for trigger in agent.triggers:
        if trigger in msg.content:
            should_wake_up = True
            break

    if not should_wake_up:
        print(f"[{my_id}] Ignoring message from {headers.source_agent_id} (type: {headers.interaction_type})")
        return

    print(f"[{my_id}] Waking up for message from {headers.source_agent_id} (type: {headers.interaction_type})")

    # --- Invoke the Brain ---

    config = {"configurable": {"thread_id": headers.conversation_id}}

    # 1. Check existing state
    current_state = await brain.aget_state(config)
    existing_agents = current_state.values.get("available_squad_agents", []) if current_state and current_state.values else []

    squad_agents = []

    # 2. Check for Briefing with Roster from Director
    if headers.interaction_type == "SESSION_BRIEFING" and msg.metadata.get("squad_members"):
        squad_agents = msg.metadata["squad_members"]

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

    initial_status = "UPDATING" if headers.interaction_type == "TASK_RESULT" else "IDLE"

    # Run the graph
    final_state = await brain.ainvoke(state_input, config=config)

    # --- Dispatch Actions ---
    await handle_brain_output(final_state, initial_status, headers, agent)


# --- Instantiate Agent ---
agent = ShinoxAgent(
    agent_card=agent_card,
    session_handler=session_event_handler,
    triggers=triggers
)
app = agent.app

# --- Run ---
# faststream run main:app
import asyncio
import os
from faststream import FastStream, Context
from faststream.kafka import KafkaBroker
from faststream.kafka.annotations import KafkaMessage
from schemas import AgentMessage, A2AHeaders, SystemCommand
from brain import brain
from agent import agent_card, SELF_INTRODUCTION
from langchain_core.messages import HumanMessage, BaseMessage
import json
import httpx

# --- Configuration ---
BROKER_URL = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
REGISTRY_URL = os.getenv("AGENT_REGISTRY_URL", "http://localhost:9000")
MY_AGENT_ID = os.getenv("AGENT_ID", "agent-generalist-01")
MY_ACTIVE_SESSIONS = set() # In prod, this is dynamic

broker = KafkaBroker(BROKER_URL)
app = FastStream(broker)

@app.on_startup
async def register_with_registry():
    """
    Register this agent with the central registry on startup.
    """
    registry_url = f"{REGISTRY_URL}/register"
    
    # Convert Pydantic model to dict
    # Try model_dump (v2) or dict (v1)
    try:
        card_data = agent_card.model_dump(mode='json')
    except AttributeError:
        print("Using Pydantic v1 dict() method")
        card_data = agent_card.dict()
    
    payload = {
        "agent_id": MY_AGENT_ID,
        "agent_url": "http://localhost:10002", 
        "card": card_data,
        "metadata": {"inbox_topic": f"mesh.agent.{MY_AGENT_ID}.inbox"}
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(registry_url, json=payload)
            if resp.status_code == 200:
                print(f"✅ Successfully registered with Registry: {resp.json()}")
            else:
                print(f"⚠️ Failed to register: {resp.text}")
    except Exception as e:
        print(f"❌ Error registering agent: {e}")

@app.on_shutdown
async def shutdown_registry_update():
    """
    Update registry status on shutdown.
    """
    registry_url = f"{REGISTRY_URL}/agent/{MY_AGENT_ID}/health"
    
    try:
        async with httpx.AsyncClient() as client:
            # Set status to 'offline'
            resp = await client.post(registry_url, params={"status": "offline"})
            if resp.status_code == 200:
                print(f"✅ Successfully updated registry status to offline")
            else:
                print(f"⚠️ Failed to update registry status: {resp.text}")
    except Exception as e:
        print(f"❌ Error updating registry on shutdown: {e}")

# --- The "Ear": Session Listener ---
# We listen to session topics. In a real implementation, this would be a dynamic subscription.
# We use a regex pattern to subscribe to all 'session.*' topics.
@broker.subscriber(f"mesh.agent.{MY_AGENT_ID}.inbox", persistent=False)
async def session_event_handler(
    body: dict,
    message: KafkaMessage,
    logger=Context(),
):
    """
    Listen for both system commands and agent messages
    """
    # Parse message body as generic dict first
    msg_body = body
    
    # Check if it's a SystemCommand
    if isinstance(msg_body, dict) and msg_body.get("type") == "SYSTEM_COMMAND":
        try:
            sys_cmd = SystemCommand(**msg_body)
            if sys_cmd.metadata.command == "JOIN_SESSION":
                session_id = sys_cmd.metadata.session_id
                MY_ACTIVE_SESSIONS.add(session_id)
                logger.info(f"Joined session: {session_id}")
            
                # send back to the topic saying that the agent has joined
                join_ack_headers = {
                    "source_agent_id": MY_AGENT_ID,
                    "interaction_type": "AGENT_JOINED",
                    "conversation_id": session_id,
                    "governance_status": "PENDING"
                }
                
                # Route through governance to archive in global.events
                join_ack_msg = AgentMessage(
                    content=f"Agent {MY_AGENT_ID} has joined session {session_id} \n Self Introduction: {SELF_INTRODUCTION}",
                    headers=A2AHeaders(**join_ack_headers)
                )
                
                # Send through pending buffer to get archived
                await broker.publish(
                    join_ack_msg,
                    topic="mesh.responses.pending",
                    headers={
                        "x-source-agent": MY_AGENT_ID,
                        "x-dest-topic": f"{session_id},mesh.global.events",
                        "x-interaction-type": "AGENT_JOINED",
                        "x-conversation-id": session_id
                    }
                )
                print(f"Sent join acknowledgment for session: {session_id}")
                
            return
        except Exception as e:
            logger.error(f"Failed to parse SystemCommand: {e}")
            return
    
    # Otherwise, try to parse as AgentMessage
    try:
        msg = AgentMessage(**msg_body)
    except Exception as e:
        logger.error(f"Failed to parse AgentMessage: {e}")
        return
        
    """
    The Selective Attention Mechanism.
    """
    # 1. Header Extraction (FastStream parses JSON body to Pydantic 'msg')
    headers = msg.headers
    
    # 2. Session Filtering (Ignore noise)
    if headers.conversation_id not in MY_ACTIVE_SESSIONS:
        return # Drop immediately (0 cost)

    # 3. Passive Memory Update
    # Convert A2A message to LangChain format
    lc_msg = HumanMessage(content=msg.content, name=headers.source_agent_id)
    
    # Update the persisted state (conceptually)
    # logic: await checkpoint.save(headers.conversation_id, {"messages": [lc_msg]})
    
    logger.info(f"Session {headers.conversation_id}: Received {headers.interaction_type}")

    # 4. Active Trigger Check
    should_wake_up = False
    
    # Trigger 1: Directly spoken to
    if headers.target_agent_id == MY_AGENT_ID:
        should_wake_up = True
        
    # Trigger 2: Mentioned in chat
    if f"@{MY_AGENT_ID}" in msg.content:
        should_wake_up = True

    if not should_wake_up:
        return # Stay in passive mode

    # --- The "Brain": Invoke LangGraph ---
    logger.info("🧠 Waking up Brain...")
    
    # Load state (mocked here, use PostgresCheckpointer in prod)
    current_state = {"messages": [lc_msg]}

    # Run the graph
    config = {"configurable": {"thread_id": headers.conversation_id}}
    final_state = await brain.ainvoke(current_state, config=config)
    
    # --- OBSERVE: Broadcast Internal State ---
    try:
        # Serialize LangChain messages to text for readabilty
        msgs = final_state.get("messages", [])
        debug_trace = [f"{type(m).__name__}: {m.content}" for m in msgs]
        
        snapshot_msg = AgentMessage(
            content=json.dumps({"thought_trace": debug_trace}, default=str),
            headers=A2AHeaders(
                source_agent_id=MY_AGENT_ID,
                interaction_type="AGENT_STATE_SNAPSHOT",
                conversation_id=headers.conversation_id,
                governance_status="VERIFIED"
            )
        )
        
        await broker.publish(
            snapshot_msg,
            topic="mesh.responses.pending",
            headers={
                "x-source-agent": MY_AGENT_ID,
                "x-dest-topic": "mesh.global.events",
                "x-interaction-type": "AGENT_STATE_SNAPSHOT",
                "x-conversation-id": headers.conversation_id,
                "x-replay-semantics": "OBSERVATIONAL" # Ignored by Replayer logic
            }
        )
    except Exception as e:
        logger.error(f"Snapshot failed: {e}")
    
    # --- The "Mouth": Dispatch Commands ---
    # Extract the last message from the brain's output
    last_message = final_state["messages"][-1]
    response_content = last_message.content
    
    # Construct the A2A Command
    # In new architecture, we send to Pending, and Governance routes it.
    target_topic = headers.conversation_id 
    
    outbound_headers = A2AHeaders(
        source_agent_id=MY_AGENT_ID,
        target_agent_id=headers.source_agent_id, # Reply to whoever sent it
        interaction_type="TASK_RESULT",
        conversation_id=headers.conversation_id
    )
    
    outbound_msg = AgentMessage(
        content=str(response_content),
        headers=outbound_headers
    )
    
    # Headers for Governance Router
    routing_headers = {
        "x-source-agent": MY_AGENT_ID,
        "x-dest-topic": target_topic,
        "x-interaction-type": "DIRECT_COMMAND",
        "x-conversation-id": headers.conversation_id
    }
    
    # Publish to the Pending Buffer
    await broker.publish(
        outbound_msg, 
        topic="mesh.responses.pending",
        headers=routing_headers
    )
# --- Run ---
# faststream run main:app
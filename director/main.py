import os
import json
import yaml
import uuid
import logging
import time
from datetime import datetime
import httpx
from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager
from kafka import KafkaProducer, KafkaAdminClient  # Standard Kafka client works with Redpanda
from kafka.admin import NewTopic
from openai import OpenAI
from pydantic import BaseModel, Field
from typing import Dict, Optional, Any

# --- CONFIGURATION ---
ROSTER_PATH = "agents.yaml"
REDPANDA_BROKERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
AGENT_REGISTRY_URL = os.getenv("AGENT_REGISTRY_URL", "http://agent-registry:9000")
OPENAI_BASE_URL = 'https://openrouter.ai/api/v1'
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_NAME = "nvidia/nemotron-3-nano-30b-a3b:free"  # Use a smart model for orchestration

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Director")

# --- GLOBAL STATE ---
roster_data = []
producer = None
admin_client = None
openai_client = None

# --- PROMPT TEMPLATE ---
DIRECTOR_SYSTEM_PROMPT = """
You are the "Chief of Staff" AI for a Decentralized Autonomous Organization. 
Your goal is to analyze an incoming request/event and assemble the PERFECT squad of agents to handle it.

CORE RULES:
1. **Minimalism:** Do not invite the whole company. Invite ONLY the agents whose capabilities match the intent.
2. **Planner:** If the task is complex or multi-step, ALWAYS invite the 'agent-triage-lead' (Planner).
3. **Context:** Write a "Briefing Note" that frames the problem clearly for the selected agents.

Here is your available Workforce (The Roster):
{minified_roster_json}

OUTPUT FORMAT (JSON ONLY):
{{
    "reasoning": "Brief thought process on why these agents were chosen.",
    "selected_agent_ids": ["agent-id-1", "agent-id-2"],
    "title": "A concise, descriptive title for the session.",
    "briefing_note": "A clear, directive statement telling the squad what the goal is.",
    "priority": "HIGH" | "NORMAL" | "LOW"
}}
"""

# --- MODELS ---
class OrganizationalEvent(BaseModel):
    source: str
    sender_id: Optional[str] = None
    content: str
    metadata: Dict[str, Any] = {}

class SquadAllocation(BaseModel):
    session_id: str
    selected_agents: list[str]
    briefing: str

# --- LIFECYCLE ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global roster_data, producer, admin_client, openai_client

    # 1. Load Roster (Registry First)
    logger.info(f"Connecting to Registry at {AGENT_REGISTRY_URL}...")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{AGENT_REGISTRY_URL}/discover?status=active")
            if resp.status_code == 200:
                agents_map = resp.json()
                roster_data = []
                for aid, info in agents_map.items():
                    roster_data.append({
                        "id": info.get("agent_id"),
                        "role": info.get("agent_name", "Worker"),
                        "capabilities": info.get("skills", []),
                        "description": info.get("description", "")
                    })
                logger.info(f"Loaded {len(roster_data)} agents from Registry.")
            else:
                logger.warning(f"Registry returned {resp.status_code}. Fallback to file.")
                raise Exception("Registry lookup failed")
    except Exception as e:
        logger.warning(f"Failed to load from registry ({e}). Trying local file.")
        try:
            if os.path.exists(ROSTER_PATH):
                with open(ROSTER_PATH, "r") as f:
                    roster_data = yaml.safe_load(f)
                logger.info(f"Loaded {len(roster_data)} agents from local roster file.")
            else:
                logger.warning("No local roster file found. Starting with empty roster.")
                roster_data = []
        except Exception as e2:
            logger.error(f"Failed to load roster from file: {e2}")
            roster_data = []

    # 2. Init Redpanda Producer
    try:
        producer = KafkaProducer(
            bootstrap_servers=REDPANDA_BROKERS,
            value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )
        logger.info(f"Connected to Redpanda at {REDPANDA_BROKERS}")
    except Exception as e:
        logger.error(f"Failed to connect to Redpanda: {e}")
        raise e

    # 2.1. Init Kafka Admin Client
    try:
        admin_client = KafkaAdminClient(
            bootstrap_servers=REDPANDA_BROKERS,
            client_id='director-admin'
        )
        logger.info("Kafka AdminClient initialized")
    except Exception as e:
        logger.error(f"Failed to init Admin Client: {e}")
        raise e

    # 3. Init OpenAI
    openai_client = OpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY
    )
    
    yield
    
    # Cleanup
    if producer:
        producer.close()
    if admin_client:
        admin_client.close()

app = FastAPI(lifespan=lifespan)

# --- LOGIC ---
async def fetch_roster():
    """Fetch active agents from the registry."""
    if not AGENT_REGISTRY_URL:
        return None
        
    try:
        async with httpx.AsyncClient() as client:
            # Short timeout to avoid blocking dispatch too long
            resp = await client.get(f"{AGENT_REGISTRY_URL}/discover?status=active", timeout=2.0)
            if resp.status_code == 200:
                agents_map = resp.json()
                new_roster = []
                for aid, info in agents_map.items():
                    new_roster.append({
                        "id": info.get("agent_id"),
                        "role": info.get("agent_name", "Worker"),
                        "capabilities": info.get("skills", []),
                        "description": info.get("description", "")
                    })
                return new_roster
            else:
                logger.warning(f"Registry returned {resp.status_code}")
    except Exception as e:
        logger.warning(f"Registry fetch failed: {e}")
    return None

@app.post("/dispatch", response_model=SquadAllocation)
async def dispatch_event(event: OrganizationalEvent):
    """
    The Main Entrypoint. 
    Accepts any event, consults LLM, creates a Session, and invites Agents.
    """
    
    # Refresh roster from registry if available
    latest_roster = await fetch_roster()
    current_roster = latest_roster if latest_roster else roster_data

    # 1. Construct Prompt
    # We strip the roster down to just ID, Role, and Capabilities to save tokens
    minified_roster = [
        {k: v for k, v in agent.items() if k in ['id', 'role', 'capabilities']} 
        for agent in current_roster
    ]
    
    system_prompt = DIRECTOR_SYSTEM_PROMPT.format(
        minified_roster_json=json.dumps(minified_roster, indent=2)
    )
    
    user_content = f"""
    SOURCE: {event.source}
    SENDER: {event.sender_id}
    CONTENT: {event.content}
    METADATA: {event.metadata}
    """

    # 2. Call LLM (The Decision)
    correlation_id = str(uuid.uuid4())  # Track this request chain
    try:
        completion = openai_client.chat.completions.create(
            model=OPENAI_MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            response_format={"type": "json_object"},
            temperature=0.1 # Low temp for deterministic routing
        )
        decision = json.loads(completion.choices[0].message.content)
        
        # Log decision to Global Events (Archival) with full context for replay
        log_payload = {
            "id": str(uuid.uuid4()),
            "type": "SYSTEM_LOG",
            "source": "Director",
            "timestamp": datetime.now().isoformat(),
            "content": json.dumps(decision),
            "correlation_id": correlation_id,
            "metadata": {
                "type": "squad_selection_decision",
                "trigger_source": event.source,
                "trigger_content": event.content,
                "selected_agents": decision.get('selected_agent_ids', []),
                "reasoning": decision.get('reasoning', ''),
                "priority": decision.get('priority', 'NORMAL')
            }
        }
        log_headers = [
            ("x-source-agent", b"director"),
            ("x-dest-topic", b"mesh.global.events"),
            ("x-interaction-type", b"SYSTEM_LOG"),
            ("x-correlation-id", correlation_id.encode('utf-8'))
        ]
        producer.send("mesh.responses.pending", value=log_payload, headers=log_headers)

    except Exception as e:
        logger.error(f"LLM Error: {e}")
        raise HTTPException(status_code=500, detail="Director Brain Malfunction")

    # 3. Create Session
    title = decision.get('title', 'untitled_session')
    # Sanitize title for topic name
    safe_title = "".join(c for c in title.lower().replace(' ', '_') if c.isalnum() or c == '_')
    session_id = f"session.{safe_title}_{uuid.uuid4().hex[:4]}"
    session_created_at = datetime.now().isoformat()
    
    # 3.1 Create the Session Topic
    try:
        topic_list = [NewTopic(name=session_id, num_partitions=1, replication_factor=1)]
        admin_client.create_topics(new_topics=topic_list, validate_only=False)
        logger.info(f"Created session topic: {session_id}")
        
        # Log session creation to global.events for replay
        session_created_payload = {
            "id": str(uuid.uuid4()),
            "type": "SYSTEM_LOG",
            "source": "Director",
            "timestamp": session_created_at,
            "content": f"Session created: {session_id}",
            "correlation_id": correlation_id,
            "metadata": {
                "type": "session_created",
                "session_id": session_id,
                "session_title": title,
                "created_at": session_created_at,
                "trigger_source": event.source,
                "priority": decision.get('priority', 'NORMAL')
            }
        }
        session_headers = [
            ("x-source-agent", b"director"),
            ("x-dest-topic", b"mesh.global.events"),
            ("x-interaction-type", b"SYSTEM_LOG"),
            ("x-correlation-id", correlation_id.encode('utf-8')),
            ("x-conversation-id", session_id.encode('utf-8'))
        ]
        producer.send("mesh.responses.pending", value=session_created_payload, headers=session_headers)
        
    except Exception as e:
        # Topic might already exist, which is fine
        logger.warning(f"Topic creation warning (likely already exists): {e}")
    
    # 4. Validate Agents (Hallucination Check)
    valid_ids = {a['id'] for a in current_roster}
    selected_ids = [aid for aid in decision.get('selected_agent_ids', []) if aid in valid_ids]
    
    if not selected_ids:
        logger.warning("LLM selected no valid agents. Defaulting to Squad Lead.")
        selected_ids = ["squad-lead-agent"]
        
    # 4.1 Always add the squad-lead-agent if not already included
    if "squad-lead-agent" not in selected_ids:
        selected_ids.append("squad-lead-agent")

    # 5. EXECUTION: The Wake Up Calls
    
    # A. Send Invites to specific Agent Inboxes via Governance Router
    for agent_id in selected_ids:
        invite_id = str(uuid.uuid4())
        invite_payload = {
            "id": invite_id,
            "type": "SYSTEM_COMMAND",
            "source": "Director",
            "timestamp": datetime.now().isoformat(),
            "content": "You have been drafted.",
            "correlation_id": correlation_id,  # Link to original request
            "metadata": {
                "command": "JOIN_SESSION",
                "session_id": session_id,
                "priority": decision.get("priority", "NORMAL"),
                "session_title": title,
                "session_briefing": decision['briefing_note']
            }
        }
        # Topic: mesh.responses.pending -> Governance -> mesh.agent.{id}.inbox + mesh.global.events
        # Use comma-separated destinations to archive invite to global.events
        headers = [
            ("x-source-agent", b"director"),
            ("x-dest-topic", f"mesh.agent.{agent_id}.inbox,mesh.global.events".encode('utf-8')),
            ("x-interaction-type", b"SYSTEM_COMMAND"),
            ("x-correlation-id", correlation_id.encode('utf-8')),
            ("x-conversation-id", session_id.encode('utf-8'))
        ]
        producer.send("mesh.responses.pending", value=invite_payload, headers=headers)
        logger.info(f"Invited {agent_id} to {session_id}")
        
    # B. Check and wait for "squad-lead-agent" to join (mocked with sleep here)
    # In production, implement proper join acknowledgment logic
    logger.info("Waiting for squad lead to join...")
    time.sleep(5)  # Mock wait
    logger.info("Squad lead has joined.")

    # B. Post the Briefing to the Session Topic via Governance Router
    # This sets the context for the agents once they join
    briefing_headers_dict = {
        "source_agent_id": "director",
        "interaction_type": "SESSION_BRIEFING",  # Match the Kafka header for consistency
        "conversation_id": session_id,
        "governance_status": "VERIFIED"
    }
    
    briefing_payload = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "content": f"## MISSION BRIEFING\n\n**Goal:** {decision['briefing_note']}\n**Priority:** {decision.get('priority')}\n**Source:** {event.source}. @squad-lead-agent , please coordinate the squad.",
        "correlation_id": correlation_id,  # Link entire session to original request
        "headers": briefing_headers_dict,
        "metadata": {
            "type": "session_briefing",
            "session_id": session_id,
            "squad_members": selected_ids,
            "trigger_source": event.source,
            "priority": decision.get('priority'),
            "reasoning": decision.get('reasoning')
        }
    }
    # Topic: mesh.responses.pending -> Governance -> session.{session_id} + mesh.global.events
    # We send Kafka headers for the Router, and Body headers for the Agent
    # Use comma-separated list for multiple destinations
    kafka_headers = [
        ("x-source-agent", b"director"),
        ("x-dest-topic", f"{session_id},mesh.global.events".encode('utf-8')),
        ("x-interaction-type", b"SESSION_BRIEFING"),
        ("x-correlation-id", correlation_id.encode('utf-8')),
        ("x-conversation-id", session_id.encode('utf-8'))
    ]
    producer.send("mesh.responses.pending", value=briefing_payload, headers=kafka_headers)

    return SquadAllocation(
        session_id=session_id,
        selected_agents=selected_ids,
        briefing=decision['briefing_note']
    )
    

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app=app, 
        host="0.0.0.0", 
        port=8000,
        log_level="info"
    )
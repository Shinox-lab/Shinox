"""
Agent Registry Service - A2A Compliant

Central registry for agent discovery in the Agent Squad ecosystem.
Agents register themselves on startup and can query for other agents.

This registry is itself an A2A-compliant agent that provides discovery services.

Features:
- Agent registration with A2A agent card
- Semantic embeddings for heuristic wake-up (using sentence-transformers)
- Multi-vector matching with per-skill embeddings
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import asyncpg
import httpx
import os
import numpy as np

# Lazy load sentence-transformers to speed up startup
_embedding_model = None

# --- Configuration ---
REGISTRY_ID = os.getenv("REGISTRY_ID", "agent-registry-001")
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:9000")
REGISTRY_PORT = int(os.getenv("REGISTRY_PORT", "9000"))
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:adminpassword@localhost:5432/agentsquaddb?sslmode=disable")

# Embedding model configuration
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
EMBEDDING_DIMENSIONS = 384  # all-MiniLM-L6-v2 produces 384-dimensional vectors


def get_embedding_model():
    """Lazy load the embedding model."""
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL_NAME}")
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        logger.info("Embedding model loaded successfully")
    return _embedding_model


def generate_embedding(text: str) -> List[float]:
    """Generate embedding for a text string."""
    model = get_embedding_model()
    embedding = model.encode(text, convert_to_numpy=True)
    return embedding.tolist()


def generate_embeddings_batch(texts: List[str]) -> List[List[float]]:
    """Generate embeddings for multiple texts in a batch (more efficient)."""
    if not texts:
        return []
    model = get_embedding_model()
    embeddings = model.encode(texts, convert_to_numpy=True)
    return embeddings.tolist()

# Heartbeat configuration: mark agents as offline if no heartbeat for this many seconds
HEARTBEAT_STALE_THRESHOLD_SECONDS = int(os.getenv("HEARTBEAT_STALE_THRESHOLD", "20"))
HEARTBEAT_CHECK_INTERVAL_SECONDS = int(os.getenv("HEARTBEAT_CHECK_INTERVAL", "10"))

logger = logging.getLogger("AgentRegistry")

import sys
from pythonjsonlogger.json import JsonFormatter
_formatter = JsonFormatter(
    fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
    rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    static_fields={"service": "agent-registry"},
)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_formatter)
logging.root.handlers.clear()
logging.root.addHandler(_handler)
logging.root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())


def parse_jsonb(value) -> dict:
    """Parse a JSONB field that may be a string or dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}

# --- Database Connection Pool ---
db_pool: Optional[asyncpg.Pool] = None

# --- Background Tasks ---
_heartbeat_checker_task: Optional[asyncio.Task] = None


async def _check_stale_agents():
    """Background task that marks agents as offline if heartbeat is stale."""
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_CHECK_INTERVAL_SECONDS)

            # Mark agents as offline if last_heartbeat_at is older than threshold
            result = await db_pool.execute("""
                UPDATE agents
                SET status = 'offline', updated_at = NOW()
                WHERE status = 'active'
                  AND last_heartbeat_at < NOW() - INTERVAL '$1 seconds'
            """.replace("$1", str(HEARTBEAT_STALE_THRESHOLD_SECONDS)))

            # Log if any agents were marked offline
            if result and result != "UPDATE 0":
                count = int(result.split(" ")[1])
                if count > 0:
                    logger.info(f"Marked {count} stale agent(s) as offline (no heartbeat for >{HEARTBEAT_STALE_THRESHOLD_SECONDS}s)")

        except asyncio.CancelledError:
            logger.debug("Heartbeat checker cancelled")
            raise
        except Exception as e:
            logger.error(f"Error checking stale agents: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for startup and shutdown events.
    """
    global db_pool, _heartbeat_checker_task

    # Startup
    logger.info(f"Connecting to database: {DATABASE_URL}")
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
    )
    logger.info("Database connected")

    # Start heartbeat checker background task
    _heartbeat_checker_task = asyncio.create_task(_check_stale_agents())
    logger.info(f"Heartbeat checker started (stale threshold: {HEARTBEAT_STALE_THRESHOLD_SECONDS}s)")

    await startup_event()
    yield

    # Shutdown
    logger.info("Shutting down Agent Registry Service...")

    # Cancel heartbeat checker
    if _heartbeat_checker_task and not _heartbeat_checker_task.done():
        _heartbeat_checker_task.cancel()
        try:
            await _heartbeat_checker_task
        except asyncio.CancelledError:
            pass
        logger.info("Heartbeat checker stopped")

    if db_pool:
        await db_pool.close()
        logger.info("Database connection closed")


app = FastAPI(
    title="Agent Registry",
    description="A2A-compliant central registry for agent discovery",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Templates
templates = Jinja2Templates(directory="templates")

# --- Models ---

class AgentRegistration(BaseModel):
    agent_id: str = Field(..., description="Unique agent identifier")
    agent_url: str = Field(..., description="URL where agent is accessible")
    metadata: Optional[Dict] = Field(default={}, description="Optional metadata")
    card: Optional[Dict] = Field(default=None, description="The full agent card. If provided, registry skips fetching from URL.")

class AgentInfo(BaseModel):
    agent_id: str
    agent_url: str
    agent_name: str
    description: str
    skills: List[str]
    registered_at: str
    last_health_check: Optional[str] = None
    status: str = "active"


class SessionMessage(BaseModel):
    """A message in a session history."""
    message_id: str
    source_agent_id: str
    target_agent_id: Optional[str] = None
    interaction_type: str
    content: str
    created_at: str
    metadata: Optional[Dict] = None


class SessionHistoryResponse(BaseModel):
    """Response for session history queries."""
    session_id: str
    total_count: int
    messages: List[SessionMessage]
    has_more: bool

# --- Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Render the Agent Registry Dashboard.
    """
    # Fetch agents from database
    rows = await db_pool.fetch("""
        SELECT agent_id, name, description, skills, kafka_inbox_topic,
               a2a_endpoint, status, metadata, created_at, last_heartbeat_at
        FROM agents
        ORDER BY status DESC, agent_id
    """)

    agents = []
    all_skills = set()
    active_count = 0
    for row in rows:
        skills_data = parse_jsonb(row['skills'])
        skill_names = list(skills_data.keys())
        all_skills.update(skill_names)

        metadata = parse_jsonb(row['metadata'])
        agent_url = metadata.get("agent_url") or row['a2a_endpoint'] or ""

        status = row['status'] or "unknown"
        if status == "active":
            active_count += 1

        agents.append({
            "agent_id": row['agent_id'],
            "agent_name": row['name'],
            "description": row['description'] or "",
            "skills": skill_names,
            "status": status,
            "agent_url": agent_url,
            "kafka_inbox_topic": row['kafka_inbox_topic'] or "",
            "registered_at": row['created_at'].isoformat() if row['created_at'] else None,
            "last_health_check": row['last_heartbeat_at'].isoformat() if row['last_heartbeat_at'] else None,
        })

    stats = {
        "total_agents": len(agents),
        "active_agents": active_count,
        "offline_agents": len(agents) - active_count,
        "total_skills": len(all_skills)
    }

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "stats": stats,
            "agents": agents,
            "Registry_ID": REGISTRY_ID,
            "heartbeat_threshold": HEARTBEAT_STALE_THRESHOLD_SECONDS,
        }
    )

@app.get("/.well-known/agent-card")
async def get_agent_card():
    """
    A2A Protocol: Return the agent card for the registry itself.
    This makes the registry discoverable as an A2A agent.
    """
    return {
        "id": REGISTRY_ID,
        "name": "Agent Registry",
        "description": "Central discovery service for A2A agents. Provides agent registration, discovery, and capability lookup services.",
        "url": REGISTRY_URL,
        "version": "1.0.0",
        "capabilities": {
            "streaming": False,
            "push_notifications": False,
            "input_modes": ["application/json"],
            "output_modes": ["application/json"]
        },
        "skills": [
            {
                "id": "skill-register-agent",
                "name": "register_agent",
                "description": "Register a new A2A agent with the registry by fetching and storing its agent card",
                "tags": ["registration", "onboarding", "discovery"],
                "examples": [
                    "Register agent squad-lead-001 at http://localhost:8001",
                    "Add new agent to the registry"
                ]
            },
            {
                "id": "skill-discover-agents",
                "name": "discover_agents",
                "description": "Find agents by skill, capability, or status. Supports filtering and search",
                "tags": ["discovery", "search", "lookup"],
                "examples": [
                    "Find all agents with skill coordinate_squad",
                    "Show me all active agents",
                    "Which agents can handle code generation?"
                ]
            },
            {
                "id": "skill-get-agent-info",
                "name": "get_agent_info",
                "description": "Retrieve detailed information about a specific agent including its full agent card",
                "tags": ["lookup", "details", "agent-card"],
                "examples": [
                    "Get details for agent squad-lead-001",
                    "Show me the agent card for the coder agent"
                ]
            },
            {
                "id": "skill-list-skills",
                "name": "list_skills",
                "description": "List all unique skills available across all registered agents with agent mapping",
                "tags": ["capabilities", "inventory", "skills"],
                "examples": [
                    "What skills are available in the system?",
                    "Show me all agent capabilities",
                    "Which agents provide which skills?"
                ]
            },
            {
                "id": "skill-registry-stats",
                "name": "registry_stats",
                "description": "Get statistics about the registry including agent counts, skill distribution, and health status",
                "tags": ["monitoring", "statistics", "health"],
                "examples": [
                    "Show registry statistics",
                    "How many agents are registered?",
                    "What's the current system status?"
                ]
            }
        ],
        "interfaces": [
            {
                "url": f"{REGISTRY_URL}",
                "transport": "HTTP+JSON"
            }
        ],
        "metadata": {
            "deployment": "agent-squad",
            "type": "registry",
            "protocols": ["A2A", "REST"],
            "storage": "postgresql"
        }
    }

@app.post("/register", response_model=Dict)
async def register_agent(registration: AgentRegistration):
    """
    Register an agent with the registry.
    Fetches the agent card from the agent's URL and stores in database.
    """
    agent_id = registration.agent_id
    agent_url = registration.agent_url

    logger.info(f"Registering agent: {agent_id} at {agent_url}")

    try:
        agent_card = registration.card

        if not agent_card:
            # Fetch agent card from the agent's well-known endpoint
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{agent_url}/.well-known/agent-card",
                    timeout=10.0
                )

                if response.status_code != 200:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to fetch agent card: {response.status_code}"
                    )

                agent_card = response.json()

        # Extract data from agent card
        agent_name = agent_card.get("name", agent_id)
        description = agent_card.get("description", "")

        # Build skills JSONB: {skill_name: skill_details, ...}
        skills_list = agent_card.get("skills", [])
        skills_dict = {}
        skill_names = []
        for skill in skills_list:
            skill_name = skill.get("name", skill.get("id", "unknown"))
            skill_names.append(skill_name)
            skills_dict[skill_name] = skill

        # Get inbox topic from metadata
        kafka_inbox_topic = registration.metadata.get("inbox_topic") if registration.metadata else None

        # Build metadata including the full card for reference
        metadata = registration.metadata or {}
        metadata["card"] = agent_card
        metadata["agent_url"] = agent_url

        # Generate embeddings for semantic matching
        logger.info(f"Generating embeddings for {agent_id}...")

        # 1. Description embedding
        description_embedding = None
        if description:
            description_embedding = generate_embedding(description)

        # 2. Skill embeddings - embed each skill's name + description
        skill_embeddings_data = []
        for skill in skills_list:
            skill_name = skill.get("name", skill.get("id", "unknown"))
            skill_desc = skill.get("description", "")
            # Combine skill name and description for richer embedding
            skill_text = f"{skill_name}: {skill_desc}" if skill_desc else skill_name
            skill_embedding = generate_embedding(skill_text)
            skill_embeddings_data.append({
                "skill_name": skill_name,
                "skill_text": skill_text[:1000],  # Truncate if too long
                "embedding": skill_embedding
            })

        # Convert description embedding to pgvector format
        desc_emb_str = None
        if description_embedding:
            desc_emb_str = "[" + ",".join(map(str, description_embedding)) + "]"

        # Upsert agent into database (with description embedding)
        await db_pool.execute("""
            INSERT INTO agents (
                agent_id, name, description, skills,
                kafka_inbox_topic, a2a_endpoint, status, metadata,
                description_embedding
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::vector)
            ON CONFLICT (agent_id) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                skills = EXCLUDED.skills,
                kafka_inbox_topic = EXCLUDED.kafka_inbox_topic,
                a2a_endpoint = EXCLUDED.a2a_endpoint,
                status = EXCLUDED.status,
                metadata = EXCLUDED.metadata,
                description_embedding = EXCLUDED.description_embedding,
                last_heartbeat_at = NOW(),
                updated_at = NOW()
        """,
            agent_id,
            agent_name,
            description,
            json.dumps(skills_dict),
            kafka_inbox_topic,
            agent_url,  # a2a_endpoint is the agent URL
            "active",
            json.dumps(metadata),
            desc_emb_str,
        )

        # Delete old skill embeddings and insert new ones
        await db_pool.execute(
            "DELETE FROM agent_skill_embeddings WHERE agent_id = $1",
            agent_id
        )

        for skill_data in skill_embeddings_data:
            emb_str = "[" + ",".join(map(str, skill_data["embedding"])) + "]"
            await db_pool.execute("""
                INSERT INTO agent_skill_embeddings (agent_id, skill_name, skill_text, embedding)
                VALUES ($1, $2, $3, $4::vector)
            """,
                agent_id,
                skill_data["skill_name"],
                skill_data["skill_text"],
                emb_str
            )

        logger.info(f"Successfully registered: {agent_name} ({agent_id})")
        logger.info(f"  Skills: {skill_names}")
        logger.info(f"  Embeddings: description + {len(skill_embeddings_data)} skills")

        return {
            "status": "registered",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "skills": skill_names,
            "embeddings_generated": True
        }

    except httpx.RequestError as e:
        logger.error(f"Failed to connect to agent {agent_id}: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach agent at {agent_url}"
        )
    except Exception as e:
        logger.error(f"Error registering agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/discover", response_model=Dict[str, AgentInfo])
async def discover_agents(
    skill: Optional[str] = None,
    status: Optional[str] = None
):
    """
    Discover agents by skill or status.

    Args:
        skill: Filter by specific skill (e.g., "coordinate_squad")
        status: Filter by status (e.g., "active")

    Returns:
        Dictionary of agents matching the criteria
    """
    # Build query with optional filters
    query = """
        SELECT agent_id, name, description, skills, kafka_inbox_topic,
               a2a_endpoint, status, metadata, created_at, last_heartbeat_at
        FROM agents
        WHERE ($1::text IS NULL OR status = $1)
    """
    rows = await db_pool.fetch(query, status)

    filtered_agents = {}
    for row in rows:
        skills_data = parse_jsonb(row['skills'])
        skill_names = list(skills_data.keys())

        # Filter by skill if specified
        if skill and skill not in skill_names:
            continue

        metadata = parse_jsonb(row['metadata'])
        agent_url = metadata.get("agent_url") or row['a2a_endpoint'] or ""

        filtered_agents[row['agent_id']] = AgentInfo(
            agent_id=row['agent_id'],
            agent_url=agent_url,
            agent_name=row['name'],
            description=row['description'] or "",
            skills=skill_names,
            registered_at=row['created_at'].isoformat() if row['created_at'] else "",
            last_health_check=row['last_heartbeat_at'].isoformat() if row['last_heartbeat_at'] else None,
            status=row['status'] or "active"
        )

    logger.info(f"Discovery query - skill:{skill}, status:{status} - Found: {len(filtered_agents)} agents")

    return filtered_agents

@app.get("/agent/{agent_id}", response_model=Dict)
async def get_agent_info(agent_id: str):
    """Get detailed information about a specific agent"""
    row = await db_pool.fetchrow("""
        SELECT agent_id, name, description, skills, kafka_inbox_topic,
               a2a_endpoint, status, metadata, created_at, last_heartbeat_at
        FROM agents
        WHERE agent_id = $1
    """, agent_id)

    if not row:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    skills_data = parse_jsonb(row['skills'])
    skill_names = list(skills_data.keys())
    metadata = parse_jsonb(row['metadata'])
    agent_url = metadata.get("agent_url") or row['a2a_endpoint'] or ""

    return {
        "agent_id": row['agent_id'],
        "agent_url": agent_url,
        "agent_name": row['name'],
        "description": row['description'] or "",
        "skills": skill_names,
        "skill_details": list(skills_data.values()),
        "card": metadata.get("card"),
        "registered_at": row['created_at'].isoformat() if row['created_at'] else None,
        "last_health_check": row['last_heartbeat_at'].isoformat() if row['last_heartbeat_at'] else None,
        "status": row['status'] or "active",
        "metadata": metadata,
    }


@app.get("/agent/{agent_id}/embeddings", response_model=Dict)
async def get_agent_embeddings(agent_id: str):
    """
    Get embeddings for an agent (for semantic wake-up).

    Returns the description embedding and all skill embeddings
    that agents use for semantic similarity matching.
    """
    # Get description embedding from agents table
    agent_row = await db_pool.fetchrow("""
        SELECT agent_id, description, description_embedding
        FROM agents
        WHERE agent_id = $1
    """, agent_id)

    if not agent_row:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    # Get skill embeddings from agent_skill_embeddings table
    skill_rows = await db_pool.fetch("""
        SELECT skill_name, skill_text, embedding
        FROM agent_skill_embeddings
        WHERE agent_id = $1
    """, agent_id)

    # Parse description embedding (pgvector returns as string or list)
    description_embedding = None
    if agent_row['description_embedding']:
        emb = agent_row['description_embedding']
        if isinstance(emb, str):
            # Parse "[0.1,0.2,...]" format
            description_embedding = json.loads(emb.replace("(", "[").replace(")", "]"))
        elif hasattr(emb, 'tolist'):
            description_embedding = emb.tolist()
        else:
            description_embedding = list(emb)

    # Parse skill embeddings
    skill_embeddings = {}
    for row in skill_rows:
        emb = row['embedding']
        if isinstance(emb, str):
            emb_list = json.loads(emb.replace("(", "[").replace(")", "]"))
        elif hasattr(emb, 'tolist'):
            emb_list = emb.tolist()
        else:
            emb_list = list(emb)

        skill_embeddings[row['skill_name']] = {
            "text": row['skill_text'],
            "embedding": emb_list
        }

    return {
        "agent_id": agent_id,
        "description": agent_row['description'],
        "description_embedding": description_embedding,
        "skill_embeddings": skill_embeddings,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "model": EMBEDDING_MODEL_NAME
    }


@app.get("/session/{session_id}/history", response_model=SessionHistoryResponse)
async def get_session_history(
    session_id: str,
    filter_agent: Optional[str] = None,
    filter_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    include_content: bool = True,
):
    """
    Get message history for a session.

    This endpoint enables agents to query their session history for context.
    Used by the Agent History Tool for on-demand context retrieval.

    Args:
        session_id: The session/conversation ID (e.g., "session.my_squad_123")
        filter_agent: Optional - only get messages from this agent
        filter_type: Optional - filter by interaction_type (e.g., "TASK_RESULT", "TASK_ASSIGNMENT")
        limit: Max number of messages to return (default: 50, max: 200)
        offset: Number of messages to skip (for pagination)
        include_content: Whether to include full message content (default: True)

    Returns:
        SessionHistoryResponse with messages sorted by created_at ascending
    """
    # Validate limits
    limit = min(limit, 200)

    # Build query with filters
    conditions = ["conversation_id = $1"]
    params = [session_id]
    param_count = 1

    if filter_agent:
        param_count += 1
        conditions.append(f"source_agent_id = ${param_count}")
        params.append(filter_agent)

    if filter_type:
        param_count += 1
        conditions.append(f"interaction_type = ${param_count}")
        params.append(filter_type)

    where_clause = " AND ".join(conditions)

    # Get total count
    count_query = f"SELECT COUNT(*) FROM messages WHERE {where_clause}"
    total_count = await db_pool.fetchval(count_query, *params)

    # Get messages with pagination
    select_fields = "message_id, source_agent_id, target_agent_id, interaction_type, created_at, metadata"
    if include_content:
        select_fields += ", content"

    query = f"""
        SELECT {select_fields}
        FROM messages
        WHERE {where_clause}
        ORDER BY created_at ASC
        LIMIT ${param_count + 1} OFFSET ${param_count + 2}
    """
    params.extend([limit, offset])

    rows = await db_pool.fetch(query, *params)

    # Build response
    messages = []
    for row in rows:
        msg = SessionMessage(
            message_id=row['message_id'],
            source_agent_id=row['source_agent_id'],
            target_agent_id=row['target_agent_id'],
            interaction_type=row['interaction_type'],
            content=row['content'] if include_content else "[content hidden]",
            created_at=row['created_at'].isoformat() if row['created_at'] else "",
            metadata=parse_jsonb(row['metadata']) if row['metadata'] else None,
        )
        messages.append(msg)

    has_more = (offset + len(messages)) < total_count

    logger.info(
        f"Session history query - session:{session_id}, "
        f"filter_agent:{filter_agent}, filter_type:{filter_type}, "
        f"returned {len(messages)}/{total_count} messages"
    )

    return SessionHistoryResponse(
        session_id=session_id,
        total_count=total_count,
        messages=messages,
        has_more=has_more,
    )


@app.get("/session/{session_id}/results")
async def get_session_results(
    session_id: str,
    limit: int = 20,
):
    """
    Get TASK_RESULT messages for a session (shortcut for common use case).

    This is a convenience endpoint for agents that need to see what
    other agents have produced in the session.

    Args:
        session_id: The session/conversation ID
        limit: Max number of results to return (default: 20)

    Returns:
        List of task results with agent_id and content
    """
    limit = min(limit, 100)

    rows = await db_pool.fetch("""
        SELECT source_agent_id, content, created_at
        FROM messages
        WHERE conversation_id = $1
          AND interaction_type = 'TASK_RESULT'
        ORDER BY created_at ASC
        LIMIT $2
    """, session_id, limit)

    results = []
    for row in rows:
        results.append({
            "agent_id": row['source_agent_id'],
            "content": row['content'],
            "created_at": row['created_at'].isoformat() if row['created_at'] else "",
        })

    logger.info(f"Session results query - session:{session_id}, returned {len(results)} results")

    return {
        "session_id": session_id,
        "count": len(results),
        "results": results,
    }


@app.delete("/agent/{agent_id}")
async def unregister_agent(agent_id: str):
    """Unregister an agent from the registry"""
    # Get agent name before deleting
    row = await db_pool.fetchrow(
        "SELECT name FROM agents WHERE agent_id = $1", agent_id
    )

    if not row:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    agent_name = row['name']

    # Delete the agent
    await db_pool.execute("DELETE FROM agents WHERE agent_id = $1", agent_id)

    logger.info(f"Unregistered agent: {agent_name} ({agent_id})")

    return {"status": "unregistered", "agent_id": agent_id}


@app.post("/agent/{agent_id}/health")
async def update_agent_health(agent_id: str, status: str = "active"):
    """Update agent's last health check timestamp and status"""
    result = await db_pool.execute("""
        UPDATE agents
        SET status = $2, last_heartbeat_at = NOW(), updated_at = NOW()
        WHERE agent_id = $1
    """, agent_id, status)

    # Check if agent was found
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    timestamp = datetime.now(timezone.utc).isoformat()

    return {"status": "ok", "timestamp": timestamp, "agent_status": status}

@app.get("/skills")
async def list_all_skills():
    """List all unique skills available across all agents"""
    rows = await db_pool.fetch("""
        SELECT agent_id, name, skills, a2a_endpoint, metadata
        FROM agents
    """)

    all_skills = set()
    skill_to_agents = {}

    for row in rows:
        skills_data = parse_jsonb(row['skills'])
        skill_names = list(skills_data.keys())
        metadata = parse_jsonb(row['metadata'])
        agent_url = metadata.get("agent_url") or row['a2a_endpoint'] or ""

        for skill in skill_names:
            all_skills.add(skill)

            if skill not in skill_to_agents:
                skill_to_agents[skill] = []

            skill_to_agents[skill].append({
                "agent_id": row['agent_id'],
                "agent_name": row['name'],
                "agent_url": agent_url
            })

    return {
        "total_skills": len(all_skills),
        "skills": sorted(list(all_skills)),
        "skill_mapping": skill_to_agents
    }


@app.get("/stats")
async def get_registry_stats():
    """Get statistics about the agent registry"""
    rows = await db_pool.fetch("""
        SELECT agent_id, name, skills, status
        FROM agents
    """)

    total_agents = len(rows)
    active_agents = sum(1 for row in rows if row['status'] == "active")

    skill_counts = {}
    agents_list = []

    for row in rows:
        skills_data = parse_jsonb(row['skills'])
        skill_names = list(skills_data.keys())

        for skill in skill_names:
            skill_counts[skill] = skill_counts.get(skill, 0) + 1

        agents_list.append({
            "id": row['agent_id'],
            "name": row['name'],
            "status": row['status'] or "unknown"
        })

    return {
        "total_agents": total_agents,
        "active_agents": active_agents,
        "total_skills": len(skill_counts),
        "skill_distribution": skill_counts,
        "agents": agents_list
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    # Get agent count from database
    count = await db_pool.fetchval("SELECT COUNT(*) FROM agents")
    return {"status": "healthy", "agents": count}

# --- Startup Event ---

async def startup_event():
    logger.info("=" * 60)
    logger.info("Agent Registry Service (A2A-Compliant) started")
    logger.info("=" * 60)
    logger.info(f"Registry ID: {REGISTRY_ID}")
    logger.info(f"Registry URL: {REGISTRY_URL}")
    logger.info(f"Port: {REGISTRY_PORT}")
    logger.info(f"Storage: PostgreSQL")
    logger.info(f"Heartbeat stale threshold: {HEARTBEAT_STALE_THRESHOLD_SECONDS}s")
    logger.info(f"A2A Agent Card: {REGISTRY_URL}/.well-known/agent-card")
    logger.info("=" * 60)
    logger.info("Available Skills:")
    logger.info("  - register_agent: Register new A2A agents")
    logger.info("  - discover_agents: Find agents by skill/status")
    logger.info("  - get_agent_info: Get detailed agent information")
    logger.info("  - list_skills: List all available skills")
    logger.info("  - registry_stats: View registry statistics")
    logger.info("=" * 60)
    logger.info("Waiting for agents to register...")
    logger.info("=" * 60)


if __name__ == "__main__":
    import uvicorn
    
    logger.info("Starting Agent Registry on http://0.0.0.0:9000")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=9000,
        log_level="info"
    )

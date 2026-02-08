"""
API Gateway Service
Provides REST API and WebSocket connections for the frontend to access
squad data, messages, and real-time Kafka updates.
"""
import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pythonjsonlogger.json import JsonFormatter

# --- Structured JSON Logging ---
_formatter = JsonFormatter(
    fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
    rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    static_fields={"service": "api-gateway"},
)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_formatter)
logging.root.handlers.clear()
logging.root.addHandler(_handler)
logging.root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("ApiGateway")

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:adminpassword@localhost:5432/agentsquaddb")
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
API_PORT = int(os.getenv("API_PORT", "8002"))


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


def map_agent_status(status: str) -> str:
    """Map database status to frontend expected status."""
    status_map = {
        "active": "ONLINE",
        "offline": "OFFLINE",
        "busy": "BUSY",
        "hibernating": "HIBERNATING",
    }
    return status_map.get(status, "OFFLINE")

# Global connections
db_pool: Optional[asyncpg.Pool] = None
kafka_producer: Optional[AIOKafkaProducer] = None
kafka_consumer: Optional[AIOKafkaConsumer] = None

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}  # squad_id -> websockets
        self.all_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket, squad_id: Optional[str] = None):
        await websocket.accept()
        self.all_connections.append(websocket)
        if squad_id:
            if squad_id not in self.active_connections:
                self.active_connections[squad_id] = []
            self.active_connections[squad_id].append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.all_connections:
            self.all_connections.remove(websocket)
        for squad_id in self.active_connections:
            if websocket in self.active_connections[squad_id]:
                self.active_connections[squad_id].remove(websocket)

    def subscribe(self, websocket: WebSocket, squad_id: str):
        if squad_id not in self.active_connections:
            self.active_connections[squad_id] = []
        if websocket not in self.active_connections[squad_id]:
            self.active_connections[squad_id].append(websocket)

    def unsubscribe(self, websocket: WebSocket, squad_id: str):
        if squad_id in self.active_connections:
            if websocket in self.active_connections[squad_id]:
                self.active_connections[squad_id].remove(websocket)

    async def broadcast_to_squad(self, squad_id: str, message: dict):
        if squad_id in self.active_connections:
            for connection in self.active_connections[squad_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    pass

    async def broadcast_all(self, message: dict):
        for connection in self.all_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass


manager = ConnectionManager()


# Pydantic models
class SendMessageRequest(BaseModel):
    content: str


class DispatchRequest(BaseModel):
    event_type: str
    source: str
    payload: dict
    priority: str = "NORMAL"


class TaskApprovalRequest(BaseModel):
    approved: bool
    reason: Optional[str] = None


# Lifespan management
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, kafka_producer, kafka_consumer

    # Startup
    logger.info("Connecting to database: %s", DATABASE_URL)
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=5,
        max_size=20,
    )
    logger.info("Database connected")

    kafka_producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        value_serializer=lambda v: json.dumps(v).encode('utf-8'),
    )
    await kafka_producer.start()
    logger.info("Kafka producer started")

    # Start Kafka consumer for real-time updates
    kafka_consumer = AIOKafkaConsumer(
        'mesh.global.events',
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id='api-gateway-realtime',
        auto_offset_reset='latest',
    )
    await kafka_consumer.start()
    logger.info("Kafka consumer started")

    # Start background task for consuming Kafka messages
    consumer_task = asyncio.create_task(consume_kafka_messages())

    yield

    # Shutdown
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    if kafka_consumer:
        await kafka_consumer.stop()
    if kafka_producer:
        await kafka_producer.stop()
    if db_pool:
        await db_pool.close()
    logger.info("Shutdown complete")


async def consume_kafka_messages():
    """Background task to consume Kafka messages and broadcast to WebSocket clients."""
    global kafka_consumer
    try:
        async for msg in kafka_consumer:
            try:
                data = json.loads(msg.value.decode('utf-8'))
                event_type = data.get('event_type', 'MESSAGE')
                conversation_id = data.get('conversation_id')

                ws_message = {
                    'type': event_type,
                    'payload': data,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                }

                if conversation_id:
                    await manager.broadcast_to_squad(conversation_id, ws_message)
                else:
                    await manager.broadcast_all(ws_message)
            except Exception as e:
                logger.error("Error processing Kafka message: %s", e)
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Agentic Mesh API Gateway",
    description="REST API and WebSocket gateway for the Agentic Mesh frontend",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health check
@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "api-gateway"}


# ==========================================
# SQUAD ENDPOINTS
# ==========================================

@app.get("/api/squads")
async def get_squads(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=100),
):
    """Get all squads with optional filtering."""
    query = """
        SELECT
            squad_name, goal, priority, status, kafka_inbox_topic,
            trigger_source, metadata, created_at, updated_at, completed_at
        FROM group_chat
        WHERE ($1::text IS NULL OR status = $1)
        ORDER BY created_at DESC
        LIMIT $2
    """
    rows = await db_pool.fetch(query, status, limit)

    squads = []
    for row in rows:
        # Get members for each squad
        members_query = """
            SELECT
                gcm.role, gcm.joined_at, gcm.context_tokens, gcm.status as member_status,
                a.agent_id, a.name, a.description,
                a.skills, a.status as agent_status
            FROM group_chat_members gcm
            JOIN agents a ON gcm.agent_id = a.agent_id
            WHERE gcm.group_chat_name = $1 AND gcm.status = 'ACTIVE'
        """
        members_rows = await db_pool.fetch(members_query, row['squad_name'])

        members = []
        for m in members_rows:
            skills_data = parse_jsonb(m['skills'])
            skill_names = list(skills_data.keys()) if skills_data else []
            members.append({
                "agent": {
                    "agentId": m['agent_id'],
                    "name": m['name'],
                    "description": m['description'] or "",
                    "skills": skill_names,
                    "status": map_agent_status(m['agent_status'] or "offline"),
                },
                "role": m['role'],
                "joinedAt": m['joined_at'].isoformat() if m['joined_at'] else None,
                "contextTokens": m['context_tokens'] or 0,
            })

        squads.append({
            "squadId": row['squad_name'],
            "name": row['squad_name'],
            "goal": row['goal'],
            "priority": row['priority'],
            "status": row['status'],
            "kafkaTopic": row['kafka_inbox_topic'],
            "triggerSource": row['trigger_source'],
            "metadata": parse_jsonb(row['metadata']),
            "members": members,
            "createdAt": row['created_at'].isoformat() if row['created_at'] else None,
        })

    return squads


@app.get("/api/squads/{squad_id}")
async def get_squad(squad_id: str):
    """Get a specific squad by ID."""
    query = """
        SELECT
            squad_name, goal, priority, status, kafka_inbox_topic,
            trigger_source, metadata, created_at
        FROM group_chat
        WHERE squad_name = $1
    """
    row = await db_pool.fetchrow(query, squad_id)

    if not row:
        raise HTTPException(status_code=404, detail="Squad not found")

    # Get members
    members_query = """
        SELECT
            gcm.role, gcm.joined_at, gcm.context_tokens,
            a.agent_id, a.name, a.description,
            a.skills, a.status as agent_status
        FROM group_chat_members gcm
        JOIN agents a ON gcm.agent_id = a.agent_id
        WHERE gcm.group_chat_name = $1 AND gcm.status = 'ACTIVE'
    """
    members_rows = await db_pool.fetch(members_query, row['squad_name'])

    members = []
    for m in members_rows:
        skills_data = parse_jsonb(m['skills'])
        skill_names = list(skills_data.keys()) if skills_data else []
        members.append({
            "agent": {
                "agentId": m['agent_id'],
                "name": m['name'],
                "description": m['description'] or "",
                "skills": skill_names,
                "status": map_agent_status(m['agent_status'] or "offline"),
            },
            "role": m['role'],
            "joinedAt": m['joined_at'].isoformat() if m['joined_at'] else None,
            "contextTokens": m['context_tokens'] or 0,
        })

    return {
        "squadId": row['squad_name'],
        "name": row['squad_name'],
        "goal": row['goal'],
        "priority": row['priority'],
        "status": row['status'],
        "kafkaTopic": row['kafka_inbox_topic'],
        "triggerSource": row['trigger_source'],
        "metadata": parse_jsonb(row['metadata']),
        "members": members,
        "createdAt": row['created_at'].isoformat() if row['created_at'] else None,
    }


# ==========================================
# MESSAGE ENDPOINTS
# ==========================================

@app.get("/api/squads/{squad_id}/messages")
async def get_squad_messages(
    squad_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Get messages for a specific squad."""
    query = """
        SELECT
            m.id, m.message_id, m.source_agent_id, m.target_agent_id,
            m.conversation_id, m.interaction_type, m.message_type,
            m.content, m.tool_name, m.tool_input, m.tool_output,
            m.governance_status, m.governance_reason, m.metadata, m.created_at
        FROM messages m
        WHERE m.conversation_id = $1
        ORDER BY m.kafka_offset ASC
        LIMIT $2 OFFSET $3
    """
    rows = await db_pool.fetch(query, squad_id, limit, offset)

    return [
        {
            "id": str(row['id']),
            "messageId": row['message_id'],
            "sourceAgentId": row['source_agent_id'],
            "targetAgentId": row['target_agent_id'],
            "conversationId": row['conversation_id'],
            "interactionType": row['interaction_type'],
            "messageType": row['message_type'],
            "content": row['content'],
            "toolName": row['tool_name'],
            "toolInput": row['tool_input'],
            "toolOutput": row['tool_output'],
            "governanceStatus": row['governance_status'],
            "governanceReason": row['governance_reason'],
            "createdAt": row['created_at'].isoformat() if row['created_at'] else None,
        }
        for row in rows
    ]


@app.post("/api/squads/{squad_id}/messages")
async def send_message(squad_id: str, request: SendMessageRequest):
    """Send a human admin message to a squad."""
    # Verify squad exists
    squad = await db_pool.fetchrow(
        "SELECT kafka_inbox_topic FROM group_chat WHERE squad_name = $1",
        squad_id
    )
    if not squad:
        raise HTTPException(status_code=404, detail="Squad not found")

    message_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    # Create message payload
    message_payload = {
        "id": message_id,
        "timestamp": timestamp,
        "source_agent_id": "human-admin",
        "conversation_id": squad_id,
        "type": "THOUGHT",
        "interaction_type": "DIRECT_COMMAND",
        "content": request.content,
        "governance_status": "PENDING",
    }

    # Publish to Kafka (mesh.responses.pending for governance check)
    await kafka_producer.send_and_wait(
        'mesh.responses.pending',
        value=message_payload,
        headers=[
            ("x-source-agent", b"human-admin"),
            ("x-dest-topic", squad['kafka_inbox_topic'].encode()),
            ("x-conversation-id", squad_id.encode()),
            ("x-interaction-type", b"DIRECT_COMMAND"),
        ],
    )

    return {
        "id": message_id,
        "status": "sent",
        "timestamp": timestamp,
    }


# ==========================================
# AGENT ENDPOINTS
# ==========================================

@app.get("/api/agents")
async def get_agents(
    status: Optional[str] = Query(None),
):
    """Get all registered agents."""
    query = """
        SELECT
            agent_id, name, description, skills,
            kafka_inbox_topic, a2a_endpoint, status, last_heartbeat_at, created_at
        FROM agents
        WHERE ($1::text IS NULL OR status = $1)
        ORDER BY created_at DESC
    """
    rows = await db_pool.fetch(query, status)

    agents = []
    for row in rows:
        # Parse skills JSONB and extract skill names as array
        skills_data = parse_jsonb(row['skills'])
        skill_names = list(skills_data.keys()) if skills_data else []

        agents.append({
            "agentId": row['agent_id'],
            "name": row['name'],
            "description": row['description'] or "",
            "skills": skill_names,
            "kafkaInboxTopic": row['kafka_inbox_topic'],
            "a2aEndpoint": row['a2a_endpoint'],
            "status": map_agent_status(row['status'] or "offline"),
            "lastHeartbeat": row['last_heartbeat_at'].isoformat() if row['last_heartbeat_at'] else None,
            "createdAt": row['created_at'].isoformat() if row['created_at'] else None,
        })

    return agents


# ==========================================
# TASK ENDPOINTS
# ==========================================

@app.get("/api/squads/{squad_id}/tasks")
async def get_squad_tasks(squad_id: str):
    """Get tasks for a specific squad - NOT IMPLEMENTED (tasks table commented out)."""
    return {
        "implemented": False,
        "message": "Tasks table is not yet implemented in the schema",
        "tasks": []
    }


@app.post("/api/tasks/{task_id}/approve")
async def approve_task(task_id: str, request: TaskApprovalRequest):
    """Approve or reject a task requiring HITL review - NOT IMPLEMENTED (tasks table commented out)."""
    return {
        "implemented": False,
        "message": "Tasks table is not yet implemented in the schema",
        "status": "FAILED"
    }


# ==========================================
# GOVERNANCE ENDPOINTS
# ==========================================

@app.get("/api/governance/alerts")
async def get_governance_alerts(
    limit: int = Query(50, ge=1, le=100),
):
    """Get recent governance alerts.
    NOTE: governance_alerts table is currently commented out in schema.
    """
    return {
        "implemented": False,
        "message": "governance_alerts table is not yet enabled in the schema",
        "alerts": []
    }


@app.post("/api/governance/halt/{squad_id}")
async def halt_squad(squad_id: str):
    """Emergency halt for a squad."""
    signal_id = str(uuid.uuid4())

    await kafka_producer.send_and_wait(
        'sys.governance.signals',
        value={
            "signal_id": signal_id,
            "action": "STOP",
            "target_agent_id": "ALL",
            "reason": f"Human admin halted squad {squad_id}",
            "conversation_id": squad_id,
        },
    )

    # Update squad status
    await db_pool.execute(
        "UPDATE group_chat SET status = 'PAUSED' WHERE squad_name = $1",
        squad_id
    )

    return {"status": "halted", "signalId": signal_id}


# ==========================================
# GLOBAL EVENTS ENDPOINT
# ==========================================

@app.get("/api/events")
async def get_global_events(
    event_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """Get global events from the archive.
    NOTE: global_events table is currently commented out in schema.
    """
    return {
        "implemented": False,
        "message": "global_events table is not yet enabled in the schema",
        "events": []
    }


# ==========================================
# RAG CONTEXT ENDPOINT (Not Implemented)
# ==========================================

@app.get("/api/rag/context")
async def get_rag_context(
    query: Optional[str] = Query(None),
    squad_id: Optional[str] = Query(None),
):
    """Get RAG context for a query - NOT IMPLEMENTED YET."""
    return {
        "implemented": False,
        "message": "RAG context retrieval not implemented yet",
        "query": query,
        "nodes": [],
        "relationships": [],
    }


@app.post("/api/rag/search")
async def search_rag(query: str):
    """Search RAG knowledge base - NOT IMPLEMENTED YET."""
    return {
        "implemented": False,
        "message": "RAG search not implemented yet",
        "query": query,
        "results": [],
    }


# ==========================================
# HITL ENDPOINTS (Partially Implemented)
# ==========================================

@app.get("/api/hitl/pending")
async def get_pending_hitl_requests():
    """Get pending HITL approval requests - NOT IMPLEMENTED YET."""
    return {
        "implemented": False,
        "message": "HITL requests endpoint not implemented yet",
        "requests": [],
    }


# ==========================================
# KAFKA LOG ENDPOINT (Not Implemented)
# ==========================================

@app.get("/api/squads/{squad_id}/kafka-log")
async def get_kafka_log(squad_id: str, limit: int = Query(100, ge=1, le=1000)):
    """Get raw Kafka log for a squad - NOT IMPLEMENTED YET."""
    return {
        "implemented": False,
        "message": "Kafka log viewing not implemented yet",
        "squadId": squad_id,
        "events": [],
    }


# ==========================================
# METRICS ENDPOINT (Not Implemented)
# ==========================================

@app.get("/api/metrics")
async def get_metrics():
    """Get system metrics - NOT IMPLEMENTED YET."""
    return {
        "implemented": False,
        "message": "Metrics endpoint not implemented yet",
        "metrics": {},
    }


# ==========================================
# WEBSOCKET ENDPOINT
# ==========================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get('type')

            if msg_type == 'SUBSCRIBE':
                squad_id = data.get('payload', {}).get('squadId')
                if squad_id:
                    manager.subscribe(websocket, squad_id)
                    await websocket.send_json({
                        'type': 'SUBSCRIBED',
                        'payload': {'squadId': squad_id},
                    })

            elif msg_type == 'UNSUBSCRIBE':
                squad_id = data.get('payload', {}).get('squadId')
                if squad_id:
                    manager.unsubscribe(websocket, squad_id)
                    await websocket.send_json({
                        'type': 'UNSUBSCRIBED',
                        'payload': {'squadId': squad_id},
                    })

            elif msg_type == 'HUMAN_MESSAGE':
                # Handle human message sent via WebSocket
                payload = data.get('payload', {})
                squad_id = payload.get('squadId')
                content = payload.get('content')

                if squad_id and content:
                    # Send to Kafka
                    message_id = str(uuid.uuid4())
                    message_payload = {
                        "id": message_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "source_agent_id": "human-admin",
                        "conversation_id": squad_id,
                        "type": "THOUGHT",
                        "interaction_type": "DIRECT_COMMAND",
                        "content": content,
                        "governance_status": "PENDING",
                    }

                    squad = await db_pool.fetchrow(
                        "SELECT kafka_inbox_topic FROM group_chat WHERE squad_name = $1",
                        squad_id
                    )

                    if squad:
                        await kafka_producer.send_and_wait(
                            'mesh.responses.pending',
                            value=message_payload,
                            headers=[
                                ("x-source-agent", b"human-admin"),
                                ("x-dest-topic", squad['kafka_inbox_topic'].encode()),
                                ("x-conversation-id", squad_id.encode()),
                                ("x-interaction-type", b"DIRECT_COMMAND"),
                            ],
                        )

                        await websocket.send_json({
                            'type': 'MESSAGE_SENT',
                            'payload': {'messageId': message_id},
                        })

            elif msg_type == 'PING':
                await websocket.send_json({'type': 'PONG'})

    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)

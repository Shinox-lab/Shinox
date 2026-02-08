import asyncio
import logging
import os
import sys
from pythonjsonlogger.json import JsonFormatter
from faststream import FastStream, Context
from faststream.kafka import KafkaBroker
from pydantic import BaseModel, Field
from typing import Optional, Literal, Dict, Any
from datetime import datetime, timezone
import uuid

# --- Structured JSON Logging ---
_formatter = JsonFormatter(
    fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
    rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    static_fields={"service": "governance"},
)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_formatter)
logging.root.handlers.clear()
logging.root.addHandler(_handler)
logging.root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

# --- Configuration ---
BROKER_URL = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
logger = logging.getLogger("Governance")

broker = KafkaBroker(BROKER_URL)
app = FastStream(broker)

# --- Schemas (Simplified) ---
class A2AHeaders(BaseModel):
    source_agent_id: str
    target_agent_id: Optional[str] = None
    interaction_type: str
    conversation_id: Optional[str] = None
    governance_status: str = "PENDING"
    
class AgentMessage(BaseModel):
    id: str
    timestamp: datetime
    content: str
    headers: Dict[str, Any] # Receive as dict, validate logic inside

class GlobalEventRecord(BaseModel):
    """Standardized format for mesh.global.events replay capability"""
    event_id: str
    event_type: str  # SESSION_CREATED, AGENT_JOINED, MESSAGE, TASK_ASSIGNED, etc.
    timestamp: str
    source_agent_id: str
    session_id: Optional[str] = None
    sequence_number: Optional[int] = None  # For ordering within session
    causation_id: Optional[str] = None  # ID of event that triggered this
    correlation_id: Optional[str] = None  # For tracking request chains
    payload: Dict[str, Any]  # The actual message/command
    metadata: Dict[str, Any] = {}  # Additional context for replay

# --- The Router Logic ---
# We accept Dict to be polymorphic (handle both AgentMessage and SystemCommand)
@broker.subscriber("mesh.responses.pending", group_id="governance-router")
async def governance_router(msg: Dict[str, Any], logger=Context(), message=Context()):
    """
    The Smart Switchboard.
    Consumes from Pending, Validates, and Routes to Destination.
    """
    # FastStream provides access to Kafka headers via Context or message object
    # We need to extract them. 'message' is the raw KafkaMessage.
    # message.headers is typically a tuple of tuples in aiokafka
    raw_headers = message.headers
    kafka_headers = dict(raw_headers) if raw_headers else {}
    
    # Decode headers to string for logic
    source_agent_raw = kafka_headers.get("x-source-agent", b"unknown")
    source_agent = source_agent_raw.decode("utf-8") if isinstance(source_agent_raw, bytes) else str(source_agent_raw)
    
    dest_topic_raw = kafka_headers.get("x-dest-topic")
    if dest_topic_raw:
        dest_topic_str = dest_topic_raw.decode("utf-8") if isinstance(dest_topic_raw, bytes) else str(dest_topic_raw)
    else:
        dest_topic_str = None
    
    # Parse destination(s) - can be comma-separated list
    dest_topics = []
    if dest_topic_str:
        dest_topics = [t.strip() for t in dest_topic_str.split(",")]
    
    logger.info(f"Processing message from {source_agent} -> {dest_topics}")

    # 1. Validation (Mock)
    # In prod: Check PII, Toxicity, ACLs
    is_valid = True 
    content = msg.get("content", "")
    if isinstance(content, str) and "DROP_ME" in content:
        is_valid = False
    
    if not is_valid:
        logger.warning(f"Blocked message from {source_agent}")
        # Send to signals?
        return

    # 2. Routing
    if not dest_topics:
        logger.error("Missing x-dest-topic header")
        return

    # 3. Fan Out to All Destinations
    # We inject the governance status into Kafka headers
    # FastStream expects header values as strings, not bytes
    outbound_headers = {}
    for key, value in kafka_headers.items():
        # Normalize to string
        str_key = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        str_value = value.decode("utf-8") if isinstance(value, bytes) else str(value) if value is not None else ""
        outbound_headers[str_key] = str_value
    
    outbound_headers["x-governance-status"] = "VERIFIED"
    outbound_headers["x-verified-at"] = datetime.now(timezone.utc).isoformat()

    # Also stamp the message payload so kafka2db persists the correct status
    if isinstance(msg.get("headers"), dict):
        msg["headers"]["governance_status"] = "VERIFIED"

    # 4. ARCHIVE TO GLOBAL EVENTS (Replay Capability)
    # Create a standardized event record for permanent storage
    interaction_type_raw = kafka_headers.get("x-interaction-type", b"MESSAGE")
    interaction_type = interaction_type_raw.decode("utf-8") if isinstance(interaction_type_raw, bytes) else str(interaction_type_raw)
    
    conversation_id_raw = kafka_headers.get("x-conversation-id")
    conversation_id = conversation_id_raw.decode("utf-8") if conversation_id_raw and isinstance(conversation_id_raw, bytes) else str(conversation_id_raw) if conversation_id_raw else None
    
    # Determine event type from message structure
    msg_type = msg.get("type", "MESSAGE")
    event_type_map = {
        "SYSTEM_COMMAND": "SYSTEM_COMMAND",
        "SYSTEM_LOG": "SYSTEM_LOG",
        "MESSAGE": interaction_type
    }
    event_type = event_type_map.get(msg_type, msg_type)
    
    global_event = GlobalEventRecord(
        event_id=msg.get("id", str(uuid.uuid4())),
        event_type=event_type,
        timestamp=msg.get("timestamp", datetime.now(timezone.utc).isoformat()),
        source_agent_id=source_agent,
        session_id=conversation_id,
        causation_id=msg.get("causation_id"),  # Track what caused this event
        correlation_id=msg.get("correlation_id"),  # Track request chains
        payload=msg,  # Full original message for replay
        metadata={
            "destinations": dest_topics,
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "governance_status": "VERIFIED",
            "original_headers": {k: v for k, v in outbound_headers.items()}
        }
    )
    
    # Always archive to global.events (compact topic for infinite retention)
    try:
        await broker.publish(
            global_event.model_dump(mode='json'),
            topic="mesh.global.events",
            headers={"event-type": event_type, "session-id": conversation_id or "system"}
        )
        logger.info(f"Archived to global.events: {event_type}")
    except Exception as e:
        logger.error(f"Failed to archive to global.events: {e}")
    
    # 5. Publish to each destination
    for dest_topic in dest_topics:
        await broker.publish(
            msg,
            topic=dest_topic,
            headers=outbound_headers
        )
        logger.info(f"Routed to {dest_topic}")

if __name__ == "__main__":
    import uvicorn
    # FastStream app run
    pass

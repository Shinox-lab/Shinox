"""
Shinox Agent SDK - Message Schemas

Pydantic models for A2A protocol messages used in the Shinox mesh.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any, Union
from uuid import uuid4
from datetime import datetime


# Type definitions based on A2A Protocol
InteractionType = Literal[
    "DIRECT_COMMAND",
    "BROADCAST_QUERY",
    "INFO_UPDATE",
    "TASK_RESULT",
    "AGENT_JOINED",
    "AGENT_STATE_SNAPSHOT",
    "SESSION_BRIEFING",
    "SQUAD_COMPLETION",
    "TASK_ASSIGNMENT",
]

GovernanceStatus = Literal["PENDING", "VERIFIED", "BLOCKED"]


class A2AHeaders(BaseModel):
    """Headers for agent-to-agent messages following A2A protocol."""

    source_agent_id: str
    target_agent_id: Optional[str] = None
    interaction_type: InteractionType
    conversation_id: str
    correlation_id: Optional[str] = None
    governance_status: GovernanceStatus = "VERIFIED"


class AgentMessage(BaseModel):
    """Standard message format for agent communication."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: Union[datetime, str] = Field(default_factory=datetime.now)
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    correlation_id: Optional[str] = None
    metadata: Dict[str, Any] = {}
    headers: A2AHeaders


class SystemCommandMetadata(BaseModel):
    """Metadata for system commands from Director."""

    command: str
    session_id: str
    priority: str = "NORMAL"
    session_title: Optional[str] = None
    session_briefing: Optional[str] = None

    class Config:
        extra = "allow"


class SystemCommand(BaseModel):
    """System command message (e.g., JOIN_SESSION from Director)."""

    id: str
    type: Literal["SYSTEM_COMMAND"]
    source: str
    timestamp: Union[int, str, datetime]
    content: Optional[str] = None
    correlation_id: Optional[str] = None
    metadata: SystemCommandMetadata

    class Config:
        extra = "allow"

from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any, Union
from uuid import uuid4
from datetime import datetime

# Enums based on A2A Protocol
InteractionType = Literal["DIRECT_COMMAND", "BROADCAST_QUERY", "INFO_UPDATE", "TASK_RESULT", "AGENT_JOINED", "AGENT_STATE_SNAPSHOT", "SESSION_BRIEFING", "SQUAD_COMPLETION"]
GovernanceStatus = Literal["PENDING", "VERIFIED", "BLOCKED"]

class A2AHeaders(BaseModel):
    source_agent_id: str
    target_agent_id: Optional[str] = None
    interaction_type: InteractionType
    conversation_id: str
    correlation_id: Optional[str] = None
    governance_status: GovernanceStatus = "VERIFIED"

class AgentMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: Union[datetime, str] = Field(default_factory=datetime.now)  # Accept both formats
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    correlation_id: Optional[str] = None
    
    # We flatten headers for easier FastStream consumption, 
    # but logically they belong to the envelope
    headers: A2AHeaders

class SystemCommandMetadata(BaseModel):
    command: str
    session_id: str
    priority: str

class SystemCommand(BaseModel):
    id: str
    type: Literal["SYSTEM_COMMAND"]
    source: str
    timestamp: Union[int, str]  # Accept both int (unix) and str (ISO) formats
    content: Optional[str] = None
    correlation_id: Optional[str] = None
    metadata: SystemCommandMetadata
"""
Agent Registry Service - A2A Compliant

Central registry for agent discovery in the Agent Squad ecosystem.
Agents register themselves on startup and can query for other agents.

This registry is itself an A2A-compliant agent that provides discovery services.
"""

import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from pydantic import BaseModel, Field
import httpx
import os

# --- Configuration ---
REGISTRY_ID = os.getenv("REGISTRY_ID", "agent-registry-001")
REGISTRY_URL = os.getenv("REGISTRY_URL", "http://localhost:9000")
REGISTRY_PORT = int(os.getenv("REGISTRY_PORT", "9000"))

logger = logging.getLogger("AgentRegistry")
logging.basicConfig(level=logging.INFO)

@asynccontextmanager
async def lifespace(app: FastAPI):
    """
    Lifespan context manager for startup and shutdown events.
    """
    await startup_event()
    yield
    logger.info("Shutting down Agent Registry Service...")

app = FastAPI(
    title="Agent Registry",
    description="A2A-compliant central registry for agent discovery",
    version="1.0.0",
    lifespan=lifespace
)

# Setup Templates
templates = Jinja2Templates(directory="templates")

# --- In-Memory Storage (Use Redis/PostgreSQL in production) ---
agent_registry: Dict[str, dict] = {}

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

# --- Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Render the Agent Registry Dashboard.
    """
    # Calculate stats for the dashboard
    all_skills = set()
    for agent in agent_registry.values():
        all_skills.update(agent.get("skills", []))
    
    stats = {
        "total_agents": len(agent_registry),
        "total_skills": len(all_skills)
    }
    
    # Sort agents by ID for consistent display
    sorted_agents = sorted(agent_registry.values(), key=lambda x: x["agent_id"])
    
    return templates.TemplateResponse(
        "index.html", 
        {
            "request": request, 
            "stats": stats, 
            "agents": sorted_agents,
            "Registry_ID": REGISTRY_ID
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
            "storage": "in-memory"
        }
    }

@app.post("/register", response_model=Dict)
async def register_agent(registration: AgentRegistration):
    """
    Register an agent with the registry.
    Fetches the agent card from the agent's URL.
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
        
        # Store agent information
        agent_registry[agent_id] = {
            "agent_id": agent_id,
            "agent_url": agent_url,
            "agent_name": agent_card.get("name", agent_id),
            "description": agent_card.get("description", ""),
            "skills": [s["name"] for s in agent_card.get("skills", [])],
            "skill_details": agent_card.get("skills", []),
            "card": agent_card,
            "registered_at": datetime.now().isoformat(),
            "last_health_check": datetime.now().isoformat(),
            "status": "active",
            "metadata": registration.metadata
        }
        
        logger.info(f"Successfully registered: {agent_card.get('name')} ({agent_id})")
        logger.info(f"  Skills: {agent_registry[agent_id]['skills']}")
        
        return {
            "status": "registered",
            "agent_id": agent_id,
            "agent_name": agent_card.get("name"),
            "skills": agent_registry[agent_id]['skills']
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
    
    filtered_agents = {}
    
    for agent_id, agent_data in agent_registry.items():
        # Apply filters
        if skill and skill not in agent_data["skills"]:
            continue
        
        if status and agent_data.get("status") != status:
            continue
        
        # Build response
        filtered_agents[agent_id] = AgentInfo(
            agent_id=agent_data["agent_id"],
            agent_url=agent_data["agent_url"],
            agent_name=agent_data["agent_name"],
            description=agent_data["description"],
            skills=agent_data["skills"],
            registered_at=agent_data["registered_at"],
            last_health_check=agent_data.get("last_health_check"),
            status=agent_data["status"]
        )
    
    logger.info(f"Discovery query - skill:{skill}, status:{status} - Found: {len(filtered_agents)} agents")
    
    return filtered_agents

@app.get("/agent/{agent_id}", response_model=Dict)
async def get_agent_info(agent_id: str):
    """Get detailed information about a specific agent"""
    
    if agent_id not in agent_registry:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    
    return agent_registry[agent_id]

@app.delete("/agent/{agent_id}")
async def unregister_agent(agent_id: str):
    """Unregister an agent from the registry"""
    
    if agent_id not in agent_registry:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    
    agent_name = agent_registry[agent_id]["agent_name"]
    del agent_registry[agent_id]
    
    logger.info(f"Unregistered agent: {agent_name} ({agent_id})")
    
    return {"status": "unregistered", "agent_id": agent_id}

@app.post("/agent/{agent_id}/health")
async def update_agent_health(agent_id: str, status: str = "active"):
    """Update agent's last health check timestamp and status"""
    
    if agent_id not in agent_registry:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")
    
    agent_registry[agent_id]["last_health_check"] = datetime.now().isoformat()
    agent_registry[agent_id]["status"] = status
    
    return {"status": "ok", "timestamp": agent_registry[agent_id]["last_health_check"], "agent_status": status}

@app.get("/skills")
async def list_all_skills():
    """List all unique skills available across all agents"""
    
    all_skills = set()
    skill_to_agents = {}
    
    for agent_id, agent_data in agent_registry.items():
        for skill in agent_data["skills"]:
            all_skills.add(skill)
            
            if skill not in skill_to_agents:
                skill_to_agents[skill] = []
            
            skill_to_agents[skill].append({
                "agent_id": agent_id,
                "agent_name": agent_data["agent_name"],
                "agent_url": agent_data["agent_url"]
            })
    
    return {
        "total_skills": len(all_skills),
        "skills": sorted(list(all_skills)),
        "skill_mapping": skill_to_agents
    }

@app.get("/stats")
async def get_registry_stats():
    """Get statistics about the agent registry"""
    
    total_agents = len(agent_registry)
    active_agents = sum(1 for a in agent_registry.values() if a.get("status") == "active")
    
    skill_counts = {}
    for agent_data in agent_registry.values():
        for skill in agent_data["skills"]:
            skill_counts[skill] = skill_counts.get(skill, 0) + 1
    
    return {
        "total_agents": total_agents,
        "active_agents": active_agents,
        "total_skills": len(skill_counts),
        "skill_distribution": skill_counts,
        "agents": [
            {
                "id": a["agent_id"],
                "name": a["agent_name"],
                "status": a["status"]
            }
            for a in agent_registry.values()
        ]
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "agents": len(agent_registry)}

# --- Startup Event ---

async def startup_event():
    logger.info("=" * 60)
    logger.info("Agent Registry Service (A2A-Compliant) started")
    logger.info("=" * 60)
    logger.info(f"Registry ID: {REGISTRY_ID}")
    logger.info(f"Registry URL: {REGISTRY_URL}")
    logger.info(f"Port: {REGISTRY_PORT}")
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

# Agent Registry Service (A2A-Compliant)

Central discovery service for A2A-compliant agents in the Agent Squad ecosystem.

**The registry itself is an A2A agent**, exposing its capabilities through a standard agent card.

## Overview

The Agent Registry provides:
- **Agent Registration**: Agents auto-register on startup
- **Discovery**: Find agents by skill, name, or status
- **Health Monitoring**: Track agent availability
- **Skill Mapping**: Browse all available skills across agents
- **A2A Compliance**: Registry is discoverable as an A2A agent

## A2A Agent Card

The registry exposes its own agent card at:
```bash
curl http://localhost:9000/.well-known/agent-card | jq
```

**Registry Skills:**
- `register_agent` - Register new A2A agents
- `discover_agents` - Find agents by skill/status
- `get_agent_info` - Get detailed agent information
- `list_skills` - List all available skills
- `registry_stats` - View registry statistics

## API Endpoints

### Register an Agent
```http
POST /register
Content-Type: application/json

{
  "agent_id": "squad-lead-001",
  "agent_url": "http://localhost:8001",
  "metadata": {}
}
```

The registry fetches the agent card from `<agent_url>/.well-known/agent-card` and stores it.

### Discover Agents
```http
GET /discover?skill=coordinate_squad&status=active
```

Returns all agents matching the criteria.

### Get Agent Details
```http
GET /agent/squad-lead-001
```

Returns full agent information including agent card.

### List All Skills
```http
GET /skills
```

Returns all unique skills and which agents provide them.

### Registry Statistics
```http
GET /stats
```

Returns registry stats (total agents, active agents, skill distribution).

### Health Check
```http
GET /health
```

Returns registry health status.

## Running the Service

### Local Development
```bash
cd infra/agent_registry
pip install -r requirements.txt
python main.py
```

The service runs on `http://0.0.0.0:9000`.

### Docker
```bash
docker-compose up agent-registry
```

## Usage in Agents

Agents auto-register on startup:

```python
import httpx

async def register_with_registry():
    async with httpx.AsyncClient() as client:
        await client.post(
            "http://localhost:9000/register",
            json={
                "agent_id": "squad-lead-001",
                "agent_url": "http://localhost:8001"
            }
        )
```

Agents can discover peers:

```python
async def discover_agent(skill: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"http://localhost:9000/discover",
            params={"skill": skill, "status": "active"}
        )
        return response.json()
```

## Storage

Currently uses in-memory storage for simplicity. For production:
- Use **Redis** for fast in-memory storage with persistence
- Use **PostgreSQL** for durable relational storage
- Use **Consul** or **etcd** for distributed service discovery

## Environment Variables

```bash
REGISTRY_ID=agent-registry-001
REGISTRY_URL=http://agent-registry:9000
REGISTRY_HOST=0.0.0.0
REGISTRY_PORT=9000
LOG_LEVEL=INFO
```

## Why is the Registry A2A-Compliant?

Making the registry itself an A2A agent provides several benefits:

1. **Consistency**: Everything in the ecosystem follows the same protocol
2. **Discoverability**: The registry can be discovered just like any other agent
3. **Interoperability**: External A2A systems can query the registry
4. **Self-Documentation**: The agent card describes all registry capabilities
5. **Future-Proofing**: Easy integration with A2A orchestrators and dashboards

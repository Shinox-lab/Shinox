# Shinox Agent SDK

A Python library for building agents in the Shinox decentralized mesh network.

## Installation

```bash
# Using pip
pip install shinox-agent-sdk

# Using uv
uv add shinox-agent-sdk

# With LangChain support
pip install shinox-agent-sdk[langchain]
```

For local development:
```bash
cd shinox-agent-sdk
pip install -e .
```

## Quick Start

### Worker Agent (Simple)

For agents that execute tasks and return results:

```python
from shinox_agent import ShinoxWorkerAgent
from brain import brain  # Your LangGraph/LangChain brain
from agent import agent_card  # Your A2A AgentCard

agent = ShinoxWorkerAgent(
    agent_card=agent_card,
    brain=brain,
    triggers=["calculate", "process"],  # Optional custom wake-up words
)

app = agent.app

# Run with: faststream run main:app
```

The worker automatically:
- Registers with the agent registry
- Joins sessions when invited by Director
- Wakes up when @mentioned or targeted
- Invokes your brain and publishes results

### Custom Agent (Advanced)

For agents with complex logic (like Squad Lead):

```python
from shinox_agent import ShinoxAgent, AgentMessage

async def my_handler(msg: AgentMessage, agent: ShinoxAgent):
    headers = msg.headers

    # Custom wake-up logic
    if headers.interaction_type == "SESSION_BRIEFING":
        # Handle briefing
        pass

    if headers.target_agent_id == agent.agent_id:
        # Process and respond
        await agent.publish_message(
            content="Task completed",
            conversation_id=headers.conversation_id,
            interaction_type="TASK_RESULT",
        )

agent = ShinoxAgent(
    agent_card=agent_card,
    session_handler=my_handler,
    triggers=["coordinate", "plan"],
)

app = agent.app
```

## API Reference

### ShinoxAgent

Base class for all agents.

```python
ShinoxAgent(
    agent_card: AgentCard,           # A2A agent card
    session_handler: Callable,        # async def handler(msg, agent)
    triggers: list[str] = None,       # Wake-up keywords
    agent_url: str = None,            # URL for registry (default: env AGENT_URL)
)
```

**Attributes:**
- `agent_id: str` - Generated from agent_card.name
- `active_sessions: set[str]` - Currently joined sessions
- `broker: KafkaBroker` - FastStream Kafka broker
- `app: FastStream` - FastStream application

**Methods:**
- `publish_message(content, conversation_id, interaction_type, target_agent_id=None)`
- `publish_update(content, conversation_id, interaction_type)` - Alias for publish_message
- `resolve_agent_inbox(agent_id) -> str` - Get inbox topic for an agent
- `fetch_available_agents(exclude_self=True) -> list[str]` - Get active agents from registry

### ShinoxWorkerAgent

Worker agent with default wake-up logic.

```python
ShinoxWorkerAgent(
    agent_card: AgentCard,
    brain: Any,                       # LangGraph brain with ainvoke()
    agent_url: str = None,
    triggers: list[str] = None,       # Additional wake-up keywords
)
```

**Additional Methods:**
- `publish_task_result(content, conversation_id, target_agent_id=None)`

**Default Wake-up Triggers:**
- `target_agent_id` matches this agent
- `@{agent_id}` mentioned in content
- Any custom trigger word found (case-insensitive)

### Message Schemas

```python
from shinox_agent import AgentMessage, A2AHeaders

# Create a message
msg = AgentMessage(
    content="Hello",
    headers=A2AHeaders(
        source_agent_id="my-agent",
        interaction_type="INFO_UPDATE",
        conversation_id="session.123",
    )
)
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:19092` | Kafka/Redpanda broker URL |
| `AGENT_REGISTRY_URL` | `http://localhost:9000` | Agent registry URL |
| `AGENT_URL` | `http://localhost:8001` | This agent's URL for registry |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    ShinoxAgent (Base)                        │
├─────────────────────────────────────────────────────────────┤
│  • Broker setup (Kafka/Redpanda)                            │
│  • Registry registration                                     │
│  • Inbox subscription (mesh.agent.{id}.inbox)               │
│  • Dynamic session subscription                              │
│  • Message parsing                                           │
│  • publish_message(), resolve_agent_inbox()                  │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ extends
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                  ShinoxWorkerAgent                           │
├─────────────────────────────────────────────────────────────┤
│  • Default wake-up logic                                     │
│  • Brain invocation                                          │
│  • publish_task_result()                                     │
└─────────────────────────────────────────────────────────────┘
```

## Message Flow

1. **Director** sends `JOIN_SESSION` to agent inbox
2. **Agent** joins session, subscribes to session topic, sends acknowledgment
3. **Director** sends `SESSION_BRIEFING` to session topic
4. **Agent** wakes up (if triggered), invokes brain, publishes result

## License

MIT

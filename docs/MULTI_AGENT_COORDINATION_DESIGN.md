# Multi-Agent Coordination Protocol Design

## Problem Statement

The current architecture uses strict orchestration where all communication flows through the Squad Lead. This creates bottlenecks and doesn't support:

1. **Group Chat Semantics** - Agents seeing and responding to relevant messages
2. **Peer-to-Peer Collaboration** - Agents helping each other without orchestrator
3. **Emergent Expertise** - Agents joining conversations when their skills are relevant
4. **Decentralized Coordination** - Reducing Squad Lead as single point of control

## Interaction Type Taxonomy

### Current Types (Orchestrated)

| Type | Producer | Consumer | Wake Behavior |
|------|----------|----------|---------------|
| `TASK_ASSIGNMENT` | Squad Lead | Target Agent | Always wake |
| `TASK_RESULT` | Worker | Squad Lead | Squad Lead processes |
| `INFO_UPDATE` | Any | All | Passive (memory only) |
| `SESSION_BRIEFING` | Squad Lead | All | Squad Lead handles |
| `AGENT_JOINED` | System | All | Passive |

### New Types (Decentralized)

| Type | Producer | Consumer | Wake Behavior |
|------|----------|----------|---------------|
| `GROUP_QUERY` | Any Agent/User | All in session | **Semantic matching** |
| `PEER_REQUEST` | Agent A | Agent B (+ observers) | Target wakes, others semantic |
| `EXPERTISE_OFFER` | Agent | All | Semantic matching |
| `COLLABORATION_INVITE` | Agent | Specific agents | Targets wake |

## Wake-Up Decision Tree (Revised)

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Message Arrives                                  │
└───────────────────────────┬─────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  1. Am I the explicit target? (target_agent_id == my_id)            │
│     → YES: WAKE (reason: direct_target)                             │
└───────────────────────────┬─────────────────────────────────────────┘
                            ▼ NO
┌─────────────────────────────────────────────────────────────────────┐
│  2. Am I @mentioned in content?                                      │
│     → YES: WAKE (reason: mention)                                   │
└───────────────────────────┬─────────────────────────────────────────┘
                            ▼ NO
┌─────────────────────────────────────────────────────────────────────┐
│  3. Is this a TASK_ASSIGNMENT to me?                                │
│     → YES: WAKE (reason: task_assignment)                           │
└───────────────────────────┬─────────────────────────────────────────┘
                            ▼ NO
┌─────────────────────────────────────────────────────────────────────┐
│  4. Is this a group-visible message type?                           │
│     (GROUP_QUERY, PEER_REQUEST, BROADCAST_QUERY, EXPERTISE_OFFER)   │
│                                                                      │
│     → Run semantic matching against my capabilities                  │
│     → If score > threshold: WAKE (reason: semantic:skill_name)      │
│     → Else: OBSERVE (update context, don't invoke LLM)              │
└───────────────────────────┬─────────────────────────────────────────┘
                            ▼ NO
┌─────────────────────────────────────────────────────────────────────┐
│  5. Is this INFO_UPDATE or TASK_RESULT?                             │
│     → OBSERVE (update memory/context, no LLM)                       │
└───────────────────────────┬─────────────────────────────────────────┘
                            ▼ NO
┌─────────────────────────────────────────────────────────────────────┐
│  6. IGNORE (not relevant to me)                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Scenario Examples

### Scenario 1: User Asks General Question (GROUP_QUERY)

```
User: "What's the current exchange rate for USD to MYR?"

Message published:
  interaction_type: GROUP_QUERY
  source_agent_id: user-session-001
  target_agent_id: null (broadcast)
  content: "What's the current exchange rate for USD to MYR?"

Agent behavior:
  - accounting-agent: semantic score 0.89 (currency, exchange) → WAKES → Responds
  - generalist-agent: semantic score 0.42 → OBSERVES (below threshold)
  - coder-agent: semantic score 0.15 → IGNORES
```

### Scenario 2: Agent Requests Peer Help (PEER_REQUEST)

```
coder-agent is stuck on database schema, asks for help:

Message published:
  interaction_type: PEER_REQUEST
  source_agent_id: coder-agent
  target_agent_id: null (anyone who can help)
  content: "I need help designing a schema for user authentication with OAuth tokens"

Agent behavior:
  - database-agent: semantic score 0.91 (schema, database) → WAKES → Offers help
  - security-agent: semantic score 0.78 (authentication, OAuth) → WAKES → Contributes security advice
  - accounting-agent: semantic score 0.12 → IGNORES
```

### Scenario 3: Collaborative Problem Solving

```
Conversation flow:

1. Squad Lead: TASK_ASSIGNMENT to coder-agent
   "Build user login API"

2. coder-agent works, realizes needs database help:
   PEER_REQUEST: "Need help with PostgreSQL connection pooling"

3. database-agent (semantic match): WAKES
   "I can help. You should use pgbouncer with..."

4. security-agent (semantic match on "login" context): WAKES
   "Also consider rate limiting for login attempts..."

5. coder-agent integrates both inputs
   TASK_RESULT: "Login API complete with pooling and rate limiting"
```

## Implementation Plan

### Phase 1: Enable GROUP_QUERY Production

**Where GROUP_QUERY should be produced:**

1. **User Messages (non-directed)**
   - When user posts to session without @mentioning specific agent
   - Frontend/API gateway marks as `GROUP_QUERY`

2. **Agent Help Requests**
   - New SDK method: `agent.request_group_help(question)`
   - Publishes with `interaction_type: GROUP_QUERY`

3. **Squad Lead Discovery**
   - Before assigning task, Squad Lead can ask group:
     "Who has experience with OAuth implementation?"
   - Agents respond based on semantic match

### Phase 2: Conversation Context Awareness

For agents to intelligently join conversations, they need:

1. **Conversation Threading**
   - Track which messages are replies to which
   - `reply_to_message_id` header field

2. **Context Window per Agent**
   - Each agent maintains recent conversation history
   - Use for deciding if follow-up is relevant

3. **Conversation Participation Tracking**
   - If agent contributed earlier, lower threshold for re-engagement
   - "I'm already in this conversation" logic

### Phase 3: Coordination Protocols

Implement formal coordination patterns:

1. **Contract Net Protocol**
   - Agent A broadcasts task announcement
   - Interested agents bid with capabilities
   - Agent A selects winner

2. **Blackboard System**
   - Shared knowledge space (the session messages)
   - Agents contribute when they have relevant info

3. **Commitment Protocol**
   - Agents can commit to tasks
   - Track commitments to prevent conflicts

## SDK Changes Required

### New Worker Methods

```python
class ShinoxWorkerAgent:

    async def broadcast_query(
        self,
        question: str,
        conversation_id: str,
    ):
        """
        Ask a question to the group. Any agent with relevant
        capabilities may respond.
        """
        await self.publish_message(
            content=question,
            conversation_id=conversation_id,
            interaction_type="GROUP_QUERY",
            target_agent_id=None,  # Broadcast
        )

    async def request_peer_help(
        self,
        request: str,
        conversation_id: str,
        preferred_agents: Optional[List[str]] = None,
    ):
        """
        Request help from peers. If preferred_agents specified,
        they get direct notification, but others can still join
        if semantically relevant.
        """
        await self.publish_message(
            content=request,
            conversation_id=conversation_id,
            interaction_type="PEER_REQUEST",
            target_agent_id=None,
            metadata={"preferred_agents": preferred_agents},
        )

    async def offer_expertise(
        self,
        offer: str,
        conversation_id: str,
    ):
        """
        Proactively offer expertise to the group when agent
        detects it may be helpful.
        """
        await self.publish_message(
            content=offer,
            conversation_id=conversation_id,
            interaction_type="EXPERTISE_OFFER",
        )
```

### Updated Wake-Up Logic

```python
# In worker.py _default_session_handler

# Group-visible message types that trigger semantic matching
GROUP_VISIBLE_TYPES = {
    "GROUP_QUERY",
    "PEER_REQUEST",
    "BROADCAST_QUERY",
    "EXPERTISE_OFFER",
}

# Updated wake-up logic
elif interaction_type in GROUP_VISIBLE_TYPES:
    # Check if we're a preferred agent (for PEER_REQUEST)
    preferred = headers.metadata.get("preferred_agents", [])
    if my_id in preferred:
        should_wake = True
        wake_reason = "preferred_peer"

    # Otherwise, use semantic matching
    elif agent._semantic_matcher and agent._semantic_matcher.initialized:
        wake, score, component = agent._semantic_matcher.should_wake(msg.content)
        if wake:
            should_wake = True
            wake_reason = f"semantic:{component}(score={score:.2f})"
        else:
            # Still observe the message for context
            agent._observe_message(msg)

    # Fallback to keyword triggers
    else:
        content_lower = msg.content.lower()
        for trigger in agent._custom_triggers:
            if trigger.lower() in content_lower:
                should_wake = True
                wake_reason = f"keyword:{trigger}"
                break
```

## Conflict Resolution

When multiple agents wake on the same GROUP_QUERY:

### Option 1: First Responder Wins
- First agent to respond handles it
- Others see the response and stand down
- Simple but may miss expertise

### Option 2: Collaborative Response
- All relevant agents respond
- Squad Lead or user synthesizes
- More comprehensive but noisy

### Option 3: Confidence-Based Selection (Recommended)
- Agents include confidence score in response metadata
- Highest confidence response is primary
- Others are supplementary
- Requires agents to self-assess

```python
# Agent response with confidence
await agent.publish_task_result(
    content="The exchange rate is 4.72 MYR per USD",
    conversation_id=conv_id,
    metadata={
        "confidence": 0.95,
        "source": "real-time API",
        "can_elaborate": True,
    }
)
```

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Session Topic                                 │
│                   (All messages visible)                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────┐     GROUP_QUERY      ┌──────────┐                    │
│  │   User   │ ───────────────────→ │   All    │                    │
│  └──────────┘                       │  Agents  │                    │
│                                     └────┬─────┘                    │
│                                          │                          │
│                              ┌───────────┼───────────┐              │
│                              ▼           ▼           ▼              │
│                         ┌────────┐  ┌────────┐  ┌────────┐         │
│                         │ Agent A│  │ Agent B│  │ Agent C│         │
│                         │ sem:0.9│  │ sem:0.3│  │ sem:0.7│         │
│                         │ WAKES  │  │ IGNORE │  │ WAKES  │         │
│                         └───┬────┘  └────────┘  └───┬────┘         │
│                             │                       │               │
│                             ▼                       ▼               │
│                      TASK_RESULT            TASK_RESULT             │
│                      (conf: 0.95)           (conf: 0.78)            │
│                             │                       │               │
│                             └───────────┬───────────┘               │
│                                         ▼                           │
│                                  User sees both                     │
│                                  (primary: Agent A)                 │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘

Peer-to-Peer with Observer:

┌─────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  ┌──────────┐     PEER_REQUEST      ┌──────────┐                   │
│  │ Coder    │ ──"help with auth"──→ │   All    │                   │
│  │ Agent    │                        │  Agents  │                   │
│  └──────────┘                        └────┬─────┘                   │
│       ▲                                   │                         │
│       │                       ┌───────────┼───────────┐             │
│       │                       ▼           ▼           ▼             │
│       │                  ┌────────┐  ┌────────┐  ┌────────┐        │
│       │                  │Database│  │Security│  │  UI    │        │
│       │                  │ sem:0.8│  │ sem:0.9│  │ sem:0.1│        │
│       │                  │ WAKES  │  │ WAKES  │  │ IGNORE │        │
│       │                  └───┬────┘  └───┬────┘  └────────┘        │
│       │                      │           │                          │
│       │    "Use connection   │           │  "Add rate limiting"    │
│       │     pooling..."      │           │                          │
│       └──────────────────────┴───────────┘                          │
│                                                                      │
│  Result: Coder gets help from BOTH relevant experts                 │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

## Migration Path

### Step 1: Add New Interaction Types (Non-breaking)
- Add `GROUP_QUERY`, `PEER_REQUEST`, `EXPERTISE_OFFER` to schema
- Existing agents ignore unknown types (safe)

### Step 2: Update Worker Wake-Up Logic
- Extend semantic matching to new types
- Maintain backward compatibility with existing types

### Step 3: Add SDK Helper Methods
- `broadcast_query()`, `request_peer_help()`, `offer_expertise()`
- Agents can start using peer communication

### Step 4: Update Squad Lead to Use GROUP_QUERY
- For discovery: "Who can handle this task?"
- For consensus: "Does anyone disagree with this plan?"

### Step 5: Implement Conflict Resolution
- Add confidence scores to responses
- Build response aggregation logic

## Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Messages through Squad Lead | 100% | <50% |
| Avg response time (peer help) | N/A | <2s |
| Expertise matching accuracy | N/A | >80% |
| False positive wake-ups | N/A | <10% |
| Agent collaboration events | 0 | >5/session |

## Conclusion

This design enables the transition from **orchestrated** to **choreographed** multi-agent communication while maintaining backward compatibility. The semantic wake-up mechanism becomes the key enabler for agents to intelligently participate in group conversations based on their capabilities.

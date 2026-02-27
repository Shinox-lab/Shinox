-- Active: 1762308987956@@127.0.0.1@5432
-- Shinox Agentic Mesh Database Schema
-- Version: 2.0.0
-- Description: Core tables for agent coordination, sessions, message persistence,
--              task tracking, HITL workflows, and agent state management

-- print all databases
SELECT datname FROM pg_database;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- AGENTS: Registry of all agents in the mesh
-- ============================================================================
CREATE TABLE IF NOT EXISTS agents (
    agent_id VARCHAR(255) UNIQUE NOT NULL, -- e.g., "squad-lead-001"
    name VARCHAR(255) NOT NULL, -- Human-readable name
    description TEXT,
    description_embedding vector (384), -- Semantic embedding for description
    skills JSONB, -- Array of capabilities
    kafka_inbox_topic VARCHAR(255), -- mesh.agent.{id}.inbox
    a2a_endpoint VARCHAR(512), -- HTTP endpoint for A2A protocol
    status VARCHAR(20) DEFAULT 'OFFLINE', -- ONLINE, OFFLINE, BUSY, HIBERNATING
    metadata JSONB DEFAULT '{}', -- version, and other info
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_heartbeat_at TIMESTAMP WITH TIME ZONE
);

-- Create index for fast similarity search on description
CREATE INDEX IF NOT EXISTS idx_agents_description_embedding ON agents USING ivfflat (
    description_embedding vector_cosine_ops
)
WITH (lists = 100);

-- ============================================================================
-- AGENT_SKILL_EMBEDDINGS: Per-skill embeddings for multi-vector matching
-- ============================================================================
CREATE TABLE IF NOT EXISTS agent_skill_embeddings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4 (),
    agent_id VARCHAR(255) NOT NULL REFERENCES agents (agent_id) ON DELETE CASCADE,
    skill_name VARCHAR(255) NOT NULL,
    skill_text VARCHAR(1000) NOT NULL, -- The text that was embedded (skill name + description)
    embedding vector (384) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (agent_id, skill_name)
);

-- Create index for fast similarity search on skills
CREATE INDEX IF NOT EXISTS idx_skill_embeddings_agent ON agent_skill_embeddings (agent_id);

CREATE INDEX IF NOT EXISTS idx_skill_embeddings_vector ON agent_skill_embeddings USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);

-- ============================================================================
-- GROUP_CHAT: Active and historical group chat sessions
-- ============================================================================
CREATE TABLE IF NOT EXISTS group_chat (
    squad_name VARCHAR(255) PRIMARY KEY, -- e.g., "ops-incident-404"
    goal TEXT NOT NULL, -- Mission objective
    priority VARCHAR(20) DEFAULT 'NORMAL', -- LOW, NORMAL, HIGH, CRITICAL
    status VARCHAR(30) DEFAULT 'ACTIVE', -- ACTIVE, PAUSED, COMPLETED, FAILED
    kafka_inbox_topic VARCHAR(255), -- session.{squad_name}
    trigger_source VARCHAR(255), -- e.g., "PagerDuty Webhook"
    trigger_payload JSONB,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE
);

-- ============================================================================
-- GROUP_CHAT_MEMBERS: Junction table for agents in group chats
-- ============================================================================
CREATE TABLE IF NOT EXISTS group_chat_members (
    group_chat_name VARCHAR(255) REFERENCES group_chat (squad_name) ON DELETE CASCADE,
    agent_id VARCHAR(255) REFERENCES agents (agent_id) ON DELETE CASCADE,
    role VARCHAR(50) DEFAULT 'MEMBER', -- LEAD, MEMBER
    joined_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    left_at TIMESTAMP WITH TIME ZONE,
    context_tokens INTEGER DEFAULT 0, -- Current context window usage
    status VARCHAR(20) DEFAULT 'ACTIVE', -- ACTIVE, IDLE, LEFT
    PRIMARY KEY (group_chat_name, agent_id)
);

-- ============================================================================
-- MESSAGES: All agent communications (source of truth)
-- ============================================================================
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4 (),
    message_id VARCHAR(255) UNIQUE NOT NULL, -- Kafka message ID
    group_chat_name VARCHAR(255) REFERENCES group_chat (squad_name) ON DELETE SET NULL,
    source_agent_id VARCHAR(255) NOT NULL, -- Sending agent
    target_agent_id VARCHAR(255), -- Target agent (null for broadcast)
    conversation_id VARCHAR(255) NOT NULL, -- Session/correlation ID
    interaction_type VARCHAR(50) NOT NULL, -- A2A interaction type
    message_type VARCHAR(50) NOT NULL, -- THOUGHT, TOOL_USE, FINAL_ANSWER, SYSTEM_NOTIFICATION
    content TEXT NOT NULL,
    tool_name VARCHAR(255), -- If TOOL_USE
    tool_input JSONB, -- Tool parameters
    tool_output JSONB, -- Tool result
    governance_status VARCHAR(20) DEFAULT 'PENDING', -- PENDING, VERIFIED, BLOCKED
    governance_reason TEXT, -- Block reason if blocked
    metadata JSONB DEFAULT '{}',
    kafka_topic VARCHAR(255), -- Source topic
    kafka_partition INTEGER,
    kafka_offset BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    processed_at TIMESTAMP WITH TIME ZONE
);

-- ============================================================================
-- GLOBAL_EVENTS: Archive of all mesh events (from mesh.global.events)
-- ============================================================================
CREATE TABLE IF NOT EXISTS global_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4 (),
    event_id VARCHAR(255) UNIQUE NOT NULL,
    event_type VARCHAR(100) NOT NULL, -- SESSION_CREATED, AGENT_JOINED, MESSAGE, GOVERNANCE_DECISION, etc.
    source_topic VARCHAR(255) NOT NULL, -- Original Kafka topic
    source_agent_id VARCHAR(255),
    conversation_id VARCHAR(255),
    payload JSONB NOT NULL, -- Full event payload
    kafka_partition INTEGER,
    kafka_offset BIGINT,
    archived_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_global_events_type ON global_events (event_type);

CREATE INDEX IF NOT EXISTS idx_global_events_source ON global_events (source_agent_id);

CREATE INDEX IF NOT EXISTS idx_global_events_conversation ON global_events (conversation_id);

CREATE INDEX IF NOT EXISTS idx_global_events_archived ON global_events (archived_at DESC);

-- ============================================================================
-- GOVERNANCE_ALERTS: Audit log of governance decisions
-- ============================================================================
CREATE TABLE IF NOT EXISTS governance_alerts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4 (),
    alert_id VARCHAR(255) UNIQUE NOT NULL,
    agent_id VARCHAR(255) NOT NULL,
    conversation_id VARCHAR(255),
    group_chat_name VARCHAR(255) REFERENCES group_chat (squad_name) ON DELETE SET NULL,
    action VARCHAR(50) NOT NULL, -- BLOCKED, WARNED, FLAGGED
    reason TEXT NOT NULL,
    original_message_id VARCHAR(255),
    original_content TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_governance_alerts_agent ON governance_alerts (agent_id);

CREATE INDEX IF NOT EXISTS idx_governance_alerts_action ON governance_alerts (action);

CREATE INDEX IF NOT EXISTS idx_governance_alerts_created ON governance_alerts (created_at DESC);

-- ============================================================================
-- SQUAD_ASSIGNMENTS: Tracking which agents are assigned to which squads and their instructions
-- ============================================================================
CREATE TABLE IF NOT EXISTS squad_assignments (
    conversation_id VARCHAR(255) NOT NULL,
    agent_id VARCHAR(255) NOT NULL,
    instruction TEXT NOT NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    check_sent BOOLEAN NOT NULL DEFAULT FALSE,
    collaboration_in_progress BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_squad_assignment UNIQUE (conversation_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_squad_assignments_conv ON squad_assignments (conversation_id);

CREATE INDEX IF NOT EXISTS idx_squad_assignments_agent ON squad_assignments (agent_id);

CREATE INDEX IF NOT EXISTS idx_squad_assignments_assigned ON squad_assignments (assigned_at);

-- ============================================================================
-- TASKS: Async task tracking (PRs, document updates, etc.)
-- ============================================================================
CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4 (),
    task_id VARCHAR(255) UNIQUE NOT NULL,
    group_chat_name VARCHAR(255) REFERENCES group_chat (squad_name) ON DELETE SET NULL,
    agent_id VARCHAR(255) NOT NULL, -- Assigned agent
    title VARCHAR(500) NOT NULL,
    description TEXT,
    task_type VARCHAR(50) NOT NULL, -- PR, DOC_UPDATE, DEPLOYMENT, ANALYSIS
    status VARCHAR(30) DEFAULT 'PENDING', -- PENDING, IN_PROGRESS, HITL_REVIEW, COMPLETED, FAILED
    progress INTEGER DEFAULT 0, -- 0-100
    requires_approval BOOLEAN DEFAULT FALSE,
    approved_by VARCHAR(255),
    approved_at TIMESTAMP WITH TIME ZONE,
    result JSONB,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_tasks_group_chat ON tasks (group_chat_name);

CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks (agent_id);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);

CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks (task_type);

-- ============================================================================
-- HITL_REQUESTS: Human-in-the-loop approval requests
-- ============================================================================
CREATE TABLE IF NOT EXISTS hitl_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4 (),
    request_id VARCHAR(255) UNIQUE NOT NULL,
    group_chat_name VARCHAR(255) REFERENCES group_chat (squad_name) ON DELETE SET NULL,
    agent_id VARCHAR(255) NOT NULL,
    request_type VARCHAR(50) NOT NULL, -- APPROVAL, REVIEW, ESCALATION, HALT
    title VARCHAR(500) NOT NULL,
    description TEXT NOT NULL,
    context JSONB, -- Additional context
    status VARCHAR(30) DEFAULT 'PENDING', -- PENDING, APPROVED, REJECTED, EXPIRED
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMP WITH TIME ZONE,
    response TEXT,
    expires_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hitl_requests_status ON hitl_requests (status);

CREATE INDEX IF NOT EXISTS idx_hitl_requests_group_chat ON hitl_requests (group_chat_name);

CREATE INDEX IF NOT EXISTS idx_hitl_requests_created ON hitl_requests (created_at DESC);

-- ============================================================================
-- AGENT_STATE_SNAPSHOTS: Checkpoints for agent hibernation/recovery
-- ============================================================================
CREATE TABLE IF NOT EXISTS agent_state_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4 (),
    agent_id VARCHAR(255) NOT NULL,
    group_chat_name VARCHAR(255) REFERENCES group_chat (squad_name) ON DELETE SET NULL,
    conversation_id VARCHAR(255),
    state_type VARCHAR(50) NOT NULL, -- HIBERNATION, CHECKPOINT, RECOVERY
    state_data JSONB NOT NULL, -- Full agent state
    langgraph_state JSONB, -- LangGraph-specific state
    context_messages JSONB, -- Message history
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_snapshots_agent ON agent_state_snapshots (agent_id);

CREATE INDEX IF NOT EXISTS idx_agent_snapshots_group_chat ON agent_state_snapshots (group_chat_name);

CREATE INDEX IF NOT EXISTS idx_agent_snapshots_created ON agent_state_snapshots (created_at DESC);

-- ============================================================================
-- FUNCTIONS: Auto-update timestamps
-- ============================================================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply triggers (use IF NOT EXISTS pattern via DO blocks)
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'tr_agents_updated_at') THEN
        CREATE TRIGGER tr_agents_updated_at BEFORE UPDATE ON agents FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'tr_group_chat_updated_at') THEN
        CREATE TRIGGER tr_group_chat_updated_at BEFORE UPDATE ON group_chat FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'tr_tasks_updated_at') THEN
        CREATE TRIGGER tr_tasks_updated_at BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'tr_squad_assignments_updated_at') THEN
        CREATE TRIGGER tr_squad_assignments_updated_at BEFORE UPDATE ON squad_assignments FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END $$;
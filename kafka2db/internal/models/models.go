package models

import (
	"encoding/json"
	"strings"
	"time"
)

// FlexTime is a time.Time that can parse multiple timestamp formats
type FlexTime struct {
	time.Time
}

// UnmarshalJSON implements json.Unmarshaler for FlexTime
func (ft *FlexTime) UnmarshalJSON(data []byte) error {
	s := strings.Trim(string(data), `"`)
	if s == "null" || s == "" {
		ft.Time = time.Time{}
		return nil
	}

	// Try different formats
	formats := []string{
		time.RFC3339Nano,
		time.RFC3339,
		"2006-01-02T15:04:05.999999",  // Without timezone
		"2006-01-02T15:04:05.999999Z", // With Z
		"2006-01-02T15:04:05",         // Without microseconds
		"2006-01-02 15:04:05",         // Space separator
	}

	var parseErr error
	for _, format := range formats {
		t, err := time.Parse(format, s)
		if err == nil {
			ft.Time = t
			return nil
		}
		parseErr = err
	}

	return parseErr
}

// MarshalJSON implements json.Marshaler for FlexTime
func (ft FlexTime) MarshalJSON() ([]byte, error) {
	return json.Marshal(ft.Time.Format(time.RFC3339Nano))
}

// A2AHeaders represents the nested headers in AgentMessage (matches Python A2AHeaders)
type A2AHeaders struct {
	SourceAgentID    string  `json:"source_agent_id"`
	TargetAgentID    *string `json:"target_agent_id,omitempty"`
	InteractionType  string  `json:"interaction_type"`
	ConversationID   string  `json:"conversation_id"`
	CorrelationID    *string `json:"correlation_id,omitempty"`
	GovernanceStatus string  `json:"governance_status"`
}

// GlobalEvent represents an archived event from mesh.global.events
type GlobalEvent struct {
	ID             string                 `json:"id"`
	EventType      string                 `json:"event_type"`
	SourceTopic    string                 `json:"source_topic"`
	SourceAgentID  *string                `json:"source_agent_id,omitempty"`
	ConversationID *string                `json:"conversation_id,omitempty"`
	Payload        map[string]interface{} `json:"payload"`
	Timestamp      FlexTime               `json:"timestamp"`
	KafkaPartition int32                  `json:"-"`
	KafkaOffset    int64                  `json:"-"`
}

// GovernanceAlert represents an alert from sys.governance.alerts
type GovernanceAlert struct {
	ID                string                 `json:"signal_id"`
	AgentID           string                 `json:"agent_id"`
	ConversationID    *string                `json:"conversation_id,omitempty"`
	Action            string                 `json:"action"` // BLOCKED, WARNED, FLAGGED
	Reason            string                 `json:"reason"`
	OriginalMessageID *string                `json:"original_message_id,omitempty"`
	OriginalContent   *string                `json:"original_content,omitempty"`
	Metadata          map[string]interface{} `json:"metadata,omitempty"`
	Timestamp         FlexTime               `json:"timestamp"`
}

// GovernanceSignal represents a control signal from sys.governance.signals
type GovernanceSignal struct {
	SignalID      string `json:"signal_id"`
	Action        string `json:"action"` // STOP, PAUSE, RESUME
	TargetAgentID string `json:"target_agent_id"`
	Reason        string `json:"reason"`
}

// GroupChat represents a group chat session (formerly Squad)
type GroupChat struct {
	SquadName       string                 `json:"squad_name"` // PK
	Goal            string                 `json:"goal"`
	Priority        string                 `json:"priority"`
	Status          string                 `json:"status"`
	KafkaInboxTopic string                 `json:"kafka_inbox_topic"`
	TriggerSource   *string                `json:"trigger_source,omitempty"`
	TriggerPayload  map[string]interface{} `json:"trigger_payload,omitempty"`
	Metadata        map[string]interface{} `json:"metadata,omitempty"`
	CreatedAt       FlexTime               `json:"created_at"`
	UpdatedAt       FlexTime               `json:"updated_at"`
	CompletedAt     *FlexTime              `json:"completed_at,omitempty"`
}

// GroupChatMember represents the junction between group_chat and agents
type GroupChatMember struct {
	GroupChatName string    `json:"group_chat_name"`
	AgentID       string    `json:"agent_id"`
	Role          string    `json:"role"`
	JoinedAt      FlexTime  `json:"joined_at"`
	LeftAt        *FlexTime `json:"left_at,omitempty"`
	ContextTokens int       `json:"context_tokens"`
	Status        string    `json:"status"`
}

// Agent represents an agent in the mesh
type Agent struct {
	AgentID         string                 `json:"agent_id"`
	Name            string                 `json:"name"`
	Description     *string                `json:"description,omitempty"`
	Skills          map[string]interface{} `json:"skills,omitempty"` // JSONB
	KafkaInboxTopic *string                `json:"kafka_inbox_topic,omitempty"`
	A2AEndpoint     *string                `json:"a2a_endpoint,omitempty"`
	Status          string                 `json:"status"`
	Metadata        map[string]interface{} `json:"metadata,omitempty"`
	CreatedAt       FlexTime               `json:"created_at"`
	UpdatedAt       FlexTime               `json:"updated_at"`
	LastHeartbeatAt *FlexTime              `json:"last_heartbeat_at,omitempty"`
}

// AgentMessage represents a message from session topics (matches Python AgentMessage)
// Updated for new messages table
type AgentMessage struct {
	ID            string                   `json:"id"`
	Timestamp     FlexTime                 `json:"timestamp"`
	Content       string                   `json:"content"`
	ToolCalls     []map[string]interface{} `json:"tool_calls,omitempty"`
	CorrelationID *string                  `json:"correlation_id,omitempty"`
	Headers       A2AHeaders               `json:"headers"`

	// Flattened fields populated from headers or Kafka headers for DB insertion
	SourceAgentID   string  `json:"-"`
	TargetAgentID   *string `json:"-"`
	ConversationID  string  `json:"-"`
	InteractionType string  `json:"-"`
	MessageType     string  `json:"-"`

	GovernanceStatus string  `json:"-"`
	GovernanceReason *string `json:"-"` // Added

	Metadata map[string]interface{} `json:"metadata,omitempty"`

	// Tool-related fields (extracted from tool_calls if present)
	ToolName   *string                `json:"-"`
	ToolInput  map[string]interface{} `json:"-"` // JSONB
	ToolOutput map[string]interface{} `json:"-"` // JSONB

	// Kafka metadata
	KafkaTopic     string `json:"-"`
	KafkaPartition int32  `json:"-"`
	KafkaOffset    int64  `json:"-"`
}

// KafkaHeaders represents A2A protocol headers from Kafka message
type KafkaHeaders struct {
	SourceAgent      string `header:"x-source-agent"`
	DestTopic        string `header:"x-dest-topic"`
	TargetAgent      string `header:"x-target-agent"`
	InteractionType  string `header:"x-interaction-type"`
	GovernanceStatus string `header:"x-governance-status"`
	CorrelationID    string `header:"x-correlation-id"`
	ConversationID   string `header:"x-conversation-id"`
}

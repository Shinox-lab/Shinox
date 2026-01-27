package db

import (
	"context"
	"encoding/json"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/rs/zerolog/log"
	"github.com/shinox-lab/kafka2db/internal/models"
)

type Repository struct {
	pool *pgxpool.Pool
}

func NewRepository(pool *pgxpool.Pool) *Repository {
	return &Repository{pool: pool}
}

// InsertMessage inserts a message into the messages table
func (r *Repository) InsertMessage(ctx context.Context, msg *models.AgentMessage) error {
	metadataJSON, _ := json.Marshal(msg.Metadata)
	toolInputJSON, _ := json.Marshal(msg.ToolInput)
	toolOutputJSON, _ := json.Marshal(msg.ToolOutput)

	// group_chat_name references squad_name in group_chat table
	// ConversationID is used as the FK link

	_, err := r.pool.Exec(ctx, `
		INSERT INTO messages (
			message_id, group_chat_name, source_agent_id, target_agent_id, conversation_id,
			interaction_type, message_type, content, tool_name, tool_input, tool_output,
			governance_status, governance_reason, metadata, kafka_topic, kafka_partition, kafka_offset, created_at
		) VALUES (
			$1, (SELECT squad_name FROM group_chat WHERE squad_name = $4 LIMIT 1), $2, $3, $4,
			$5, $6, $7, $8, $9, $10,
			$11, $12, $13, $14, $15, $16, $17
		)
		ON CONFLICT (message_id) DO NOTHING
	`,
		msg.ID, msg.SourceAgentID, msg.TargetAgentID, msg.ConversationID,
		msg.InteractionType, msg.MessageType, msg.Content,
		msg.ToolName, toolInputJSON, toolOutputJSON,
		msg.GovernanceStatus, msg.GovernanceReason, metadataJSON,
		msg.KafkaTopic, msg.KafkaPartition, msg.KafkaOffset, msg.Timestamp.Time,
	)
	return err
}

// InsertGlobalEvent inserts an event from mesh.global.events
// NOTE: global_events table is currently commented out in schema
// This function will fail until the table is enabled
func (r *Repository) InsertGlobalEvent(ctx context.Context, event *models.GlobalEvent) error {
	log.Warn().Msg("InsertGlobalEvent called but global_events table is not enabled in schema")
	// Return nil to allow processing to continue - events are logged but not persisted
	return nil
}

// InsertGovernanceAlert inserts an alert from sys.governance.alerts
// NOTE: governance_alerts table is currently commented out in schema
// This function will fail until the table is enabled
func (r *Repository) InsertGovernanceAlert(ctx context.Context, alert *models.GovernanceAlert) error {
	log.Warn().Msg("InsertGovernanceAlert called but governance_alerts table is not enabled in schema")
	// Return nil to allow processing to continue - alerts are logged but not persisted
	return nil
}

// UpsertGroupChat creates or updates a group_chat (squad)
func (r *Repository) UpsertGroupChat(ctx context.Context, chat *models.GroupChat) error {
	triggerPayloadJSON, _ := json.Marshal(chat.TriggerPayload)
	metadataJSON, _ := json.Marshal(chat.Metadata)

	_, err := r.pool.Exec(ctx, `
		INSERT INTO group_chat (
			squad_name, goal, priority, status, kafka_inbox_topic,
			trigger_source, trigger_payload, metadata, created_at
		) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
		ON CONFLICT (squad_name) DO UPDATE SET
			status = EXCLUDED.status,
			updated_at = NOW()
	`,
		chat.SquadName, chat.Goal, chat.Priority, chat.Status,
		chat.KafkaInboxTopic, chat.TriggerSource, triggerPayloadJSON,
		metadataJSON, chat.CreatedAt.Time,
	)
	return err
}

// UpsertAgent creates or updates an agent
func (r *Repository) UpsertAgent(ctx context.Context, agent *models.Agent) error {
	metadataJSON, _ := json.Marshal(agent.Metadata)
	skillsJSON, _ := json.Marshal(agent.Skills)

	_, err := r.pool.Exec(ctx, `
		INSERT INTO agents (
			agent_id, name, description, skills,
			kafka_inbox_topic, a2a_endpoint, status, metadata
		) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
		ON CONFLICT (agent_id) DO UPDATE SET
			status = EXCLUDED.status,
			last_heartbeat_at = NOW(),
			updated_at = NOW()
	`,
		agent.AgentID, agent.Name, agent.Description, skillsJSON,
		agent.KafkaInboxTopic, agent.A2AEndpoint, agent.Status, metadataJSON,
	)
	return err
}

// BatchInsertMessages inserts multiple messages in a single transaction
func (r *Repository) BatchInsertMessages(ctx context.Context, messages []*models.AgentMessage) error {
	if len(messages) == 0 {
		return nil
	}

	batch := &pgx.Batch{}
	for _, msg := range messages {
		metadataJSON, _ := json.Marshal(msg.Metadata)
		toolInputJSON, _ := json.Marshal(msg.ToolInput)
		toolOutputJSON, _ := json.Marshal(msg.ToolOutput)

		// group_chat_name references squad_name in group_chat table
		batch.Queue(`
			INSERT INTO messages (
				message_id, group_chat_name, source_agent_id, target_agent_id, conversation_id,
				interaction_type, message_type, content, tool_name, tool_input, tool_output,
				governance_status, governance_reason, metadata, kafka_topic, kafka_partition, kafka_offset, created_at
			) VALUES (
				$1, (SELECT squad_name FROM group_chat WHERE squad_name = $4 LIMIT 1), $2, $3, $4,
				$5, $6, $7, $8, $9, $10,
				$11, $12, $13, $14, $15, $16, $17
			)
			ON CONFLICT (message_id) DO NOTHING
		`,
			msg.ID, msg.SourceAgentID, msg.TargetAgentID, msg.ConversationID,
			msg.InteractionType, msg.MessageType, msg.Content,
			msg.ToolName, toolInputJSON, toolOutputJSON,
			msg.GovernanceStatus, msg.GovernanceReason, metadataJSON,
			msg.KafkaTopic, msg.KafkaPartition, msg.KafkaOffset, msg.Timestamp.Time,
		)
	}

	br := r.pool.SendBatch(ctx, batch)
	defer br.Close()

	for i := 0; i < len(messages); i++ {
		if _, err := br.Exec(); err != nil {
			log.Error().Err(err).Int("index", i).Msg("Batch insert error")
		}
	}

	return nil
}

// UpdateKafkaOffset stores the latest processed offset for exactly-once semantics
// NOTE: kafka_offsets table is currently commented out in schema
// This function is disabled until the table is enabled
func (r *Repository) UpdateKafkaOffset(ctx context.Context, consumerGroup, topic string, partition int32, offset int64) error {
	log.Debug().Str("consumer_group", consumerGroup).Str("topic", topic).Int32("partition", partition).Int64("offset", offset).Msg("UpdateKafkaOffset called but kafka_offsets table is not enabled in schema")
	// Return nil - offset tracking is disabled
	return nil
}

// GetKafkaOffset retrieves the last committed offset
// NOTE: kafka_offsets table is currently commented out in schema
// This function returns -1 (start from beginning) until the table is enabled
func (r *Repository) GetKafkaOffset(ctx context.Context, consumerGroup, topic string, partition int32) (int64, error) {
	log.Debug().Str("consumer_group", consumerGroup).Str("topic", topic).Int32("partition", partition).Msg("GetKafkaOffset called but kafka_offsets table is not enabled in schema")
	// Return -1 to indicate no stored offset
	return -1, nil
}

// InsertGroupChatMember adds an agent to a group_chat
func (r *Repository) InsertGroupChatMember(ctx context.Context, groupChatName string, agentID string, role string) error {
	_, err := r.pool.Exec(ctx, `
		INSERT INTO group_chat_members (group_chat_name, agent_id, role)
		VALUES ($1, $2, $3)
		ON CONFLICT (group_chat_name, agent_id) DO UPDATE SET
			status = 'ACTIVE',
			left_at = NULL
	`, groupChatName, agentID, role)
	return err
}

// AddGroupChatMemberBySessionID adds an agent to a group_chat using the session_id (squad_name) and agent_id string
func (r *Repository) AddGroupChatMemberBySessionID(ctx context.Context, sessionID string, agentID string, role string) error {
	// First, ensure the agent exists in the agents table (upsert with minimal info)
	_, err := r.pool.Exec(ctx, `
		INSERT INTO agents (agent_id, name, status)
		VALUES ($1, $1, 'ONLINE')
		ON CONFLICT (agent_id) DO UPDATE SET
			status = 'ONLINE',
			last_heartbeat_at = NOW()
	`, agentID)
	if err != nil {
		log.Error().Err(err).Str("agent_id", agentID).Msg("Failed to upsert agent")
		return err
	}

	// Now insert the group_chat member using values directly (sessionID is squad_name PK)
	_, err = r.pool.Exec(ctx, `
		INSERT INTO group_chat_members (group_chat_name, agent_id, role, status)
		VALUES ($1, $2, $3, 'ACTIVE')
		ON CONFLICT (group_chat_name, agent_id) DO UPDATE SET
			status = 'ACTIVE',
			left_at = NULL,
			role = EXCLUDED.role
	`, sessionID, agentID, role)

	if err != nil {
		log.Error().Err(err).Str("session_id", sessionID).Str("agent_id", agentID).Msg("Failed to add group chat member")
	}
	return err
}

package consumer

import (
	"context"
	"encoding/json"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/IBM/sarama"
	"github.com/rs/zerolog/log"
	"github.com/shinox-lab/kafka2db/internal/config"
	"github.com/shinox-lab/kafka2db/internal/db"
	"github.com/shinox-lab/kafka2db/internal/models"
)

type Group struct {
	cfg           *config.Config
	repo          *db.Repository
	consumerGroup sarama.ConsumerGroup
	adminClient   sarama.ClusterAdmin
	ready         chan bool
	wg            sync.WaitGroup
	topicsMu      sync.RWMutex
	currentTopics []string
	stopDiscovery chan struct{}
	// cancelConsume cancels the current Consume() call to force resubscription
	cancelConsume context.CancelFunc
	cancelMu      sync.Mutex
}

func NewGroup(cfg *config.Config, repo *db.Repository) (*Group, error) {
	saramaConfig := sarama.NewConfig()
	saramaConfig.Version = sarama.V3_6_0_0
	saramaConfig.Consumer.Group.Rebalance.GroupStrategies = []sarama.BalanceStrategy{sarama.NewBalanceStrategyRoundRobin()}
	saramaConfig.Consumer.Offsets.Initial = sarama.OffsetOldest
	saramaConfig.Consumer.Return.Errors = true

	// Performance tuning
	saramaConfig.Consumer.Fetch.Min = 1
	saramaConfig.Consumer.Fetch.Default = 1024 * 1024 // 1MB
	saramaConfig.Consumer.MaxWaitTime = 250 * time.Millisecond
	saramaConfig.ChannelBufferSize = 256

	// Enable metadata refresh for dynamic topic discovery
	saramaConfig.Metadata.RefreshFrequency = 7 * time.Second

	brokers := strings.Split(cfg.KafkaBrokers, ",")
	consumerGroup, err := sarama.NewConsumerGroup(brokers, cfg.ConsumerGroup, saramaConfig)
	if err != nil {
		return nil, err
	}

	// Create admin client for topic discovery
	adminClient, err := sarama.NewClusterAdmin(brokers, saramaConfig)
	if err != nil {
		consumerGroup.Close()
		return nil, err
	}

	return &Group{
		cfg:           cfg,
		repo:          repo,
		consumerGroup: consumerGroup,
		adminClient:   adminClient,
		ready:         make(chan bool),
		currentTopics: cfg.Topics,
		stopDiscovery: make(chan struct{}),
	}, nil
}

func (g *Group) Start(ctx context.Context) error {
	handler := &consumerHandler{
		repo:         g.repo,
		batchSize:    g.cfg.BatchSize,
		batchTimeout: g.cfg.BatchTimeout,
		ready:        g.ready,
	}

	// Start dynamic topic discovery
	go g.discoverTopics(ctx)

	g.wg.Add(1)
	go func() {
		defer g.wg.Done()
		for {
			// Get current topics (thread-safe)
			g.topicsMu.RLock()
			topics := make([]string, len(g.currentTopics))
			copy(topics, g.currentTopics)
			g.topicsMu.RUnlock()

			if len(topics) == 0 {
				log.Info().Msg("No topics found yet, waiting for discovery...")
				select {
				case <-ctx.Done():
					return
				case <-time.After(1 * time.Second):
					continue
				}
			}

			log.Info().Strs("topics", topics).Msg("Subscribing to topics")

			// Create a cancellable context for this consume session
			// This allows discoverTopics to cancel and force resubscription
			consumeCtx, cancelConsume := context.WithCancel(ctx)
			g.cancelMu.Lock()
			g.cancelConsume = cancelConsume
			g.cancelMu.Unlock()

			if err := g.consumerGroup.Consume(consumeCtx, topics, handler); err != nil {
				if consumeCtx.Err() == context.Canceled && ctx.Err() == nil {
					// Cancelled for resubscription, not shutdown
					log.Info().Msg("Resubscribing to updated topics")
				} else {
					log.Error().Err(err).Msg("Consumer group error")
				}
			}
			if ctx.Err() != nil {
				return
			}
			handler.ready = make(chan bool)
		}
	}()

	// Handle errors
	go func() {
		for err := range g.consumerGroup.Errors() {
			log.Error().Err(err).Msg("Consumer error")
		}
	}()

	<-g.ready
	log.Info().Msg("Consumer group is ready")

	return nil
}

func (g *Group) Close() error {
	close(g.stopDiscovery)
	if g.adminClient != nil {
		g.adminClient.Close()
	}
	return g.consumerGroup.Close()
}

// discoverTopics periodically checks for new session.* topics
func (g *Group) discoverTopics(ctx context.Context) {
	// Pattern to match session topics (e.g., session.my_session_abc123)
	sessionPattern := regexp.MustCompile(`^session\..*$`)

	scan := func() {
		// Fetch all topics from Kafka
		topicsMeta, err := g.adminClient.ListTopics()
		if err != nil {
			log.Error().Err(err).Msg("Failed to list topics")
			return
		}

		// Build new topic list: base topics + session.* topics
		newTopics := make([]string, 0, len(g.cfg.Topics)+10)
		newTopics = append(newTopics, g.cfg.Topics...)

		for topicName := range topicsMeta {
			if sessionPattern.MatchString(topicName) {
				// Check if not already in base topics
				alreadyIncluded := false
				for _, t := range g.cfg.Topics {
					if t == topicName {
						alreadyIncluded = true
						break
					}
				}
				if !alreadyIncluded {
					newTopics = append(newTopics, topicName)
				}
			}
		}

		// Check if topics changed
		g.topicsMu.RLock()
		changed := len(newTopics) != len(g.currentTopics)
		if !changed {
			// Check if any topic is different
			existing := make(map[string]bool)
			for _, t := range g.currentTopics {
				existing[t] = true
			}
			for _, t := range newTopics {
				if !existing[t] {
					changed = true
					break
				}
			}
		}
		g.topicsMu.RUnlock()

		if changed {
			g.topicsMu.Lock()
			g.currentTopics = newTopics
			g.topicsMu.Unlock()

			log.Info().Strs("topics", newTopics).Msg("Discovered new topics, triggering resubscription")

			// Cancel current Consume() to force resubscription with new topics
			g.cancelMu.Lock()
			if g.cancelConsume != nil {
				g.cancelConsume()
			}
			g.cancelMu.Unlock()
		}
	}

	// Initial scan
	scan()

	ticker := time.NewTicker(10 * time.Second) // Check every 10 seconds
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-g.stopDiscovery:
			return
		case <-ticker.C:
			scan()
		}
	}
}

// consumerHandler implements sarama.ConsumerGroupHandler
type consumerHandler struct {
	repo         *db.Repository
	batchSize    int
	batchTimeout time.Duration
	ready        chan bool
}

func (h *consumerHandler) Setup(sarama.ConsumerGroupSession) error {
	close(h.ready)
	return nil
}

func (h *consumerHandler) Cleanup(sarama.ConsumerGroupSession) error {
	return nil
}

func (h *consumerHandler) ConsumeClaim(session sarama.ConsumerGroupSession, claim sarama.ConsumerGroupClaim) error {
	ctx := session.Context()
	topic := claim.Topic()

	// Batch processor
	messageBatch := make([]*models.AgentMessage, 0, h.batchSize)
	eventBatch := make([]*models.GlobalEvent, 0, h.batchSize)
	alertBatch := make([]*models.GovernanceAlert, 0, h.batchSize)

	ticker := time.NewTicker(h.batchTimeout)
	defer ticker.Stop()

	flushBatches := func() {
		if len(messageBatch) > 0 {
			if err := h.repo.BatchInsertMessages(ctx, messageBatch); err != nil {
				log.Error().Err(err).Msg("Failed to batch insert messages")
			} else {
				log.Debug().Int("count", len(messageBatch)).Str("topic", topic).Msg("Batch inserted messages")
			}
			messageBatch = messageBatch[:0]
		}

		for _, event := range eventBatch {
			if err := h.repo.InsertGlobalEvent(ctx, event); err != nil {
				log.Error().Err(err).Msg("Failed to insert global event")
			}
		}
		if len(eventBatch) > 0 {
			log.Debug().Int("count", len(eventBatch)).Msg("Inserted global events")
			eventBatch = eventBatch[:0]
		}

		for _, alert := range alertBatch {
			if err := h.repo.InsertGovernanceAlert(ctx, alert); err != nil {
				log.Error().Err(err).Msg("Failed to insert governance alert")
			}
		}
		if len(alertBatch) > 0 {
			log.Debug().Int("count", len(alertBatch)).Msg("Inserted governance alerts")
			alertBatch = alertBatch[:0]
		}
	}

	for {
		select {
		case <-ctx.Done():
			flushBatches()
			return nil

		case <-ticker.C:
			flushBatches()

		case msg, ok := <-claim.Messages():
			if !ok {
				flushBatches()
				return nil
			}

			// Parse headers
			headers := parseHeaders(msg.Headers)

			switch {
			case strings.HasPrefix(topic, "session."):
				// Handle session.* topics - create squad if needed and insert messages
				agentMsg, err := parseAgentMessage(msg, headers)
				if err != nil {
					log.Warn().Err(err).Str("topic", topic).Msg("Failed to parse agent message")
					session.MarkMessage(msg, "")
					continue
				}

				// Ensure squad exists for this session topic
				if err := h.ensureSquadExists(ctx, topic, agentMsg); err != nil {
					log.Error().Err(err).Str("topic", topic).Msg("Failed to ensure squad exists")
				}

				// Add to message batch for insertion
				messageBatch = append(messageBatch, agentMsg)

				// Handle AGENT_JOINED - add to squad_members
				if agentMsg.InteractionType == "AGENT_JOINED" {
					if err := h.handleAgentJoined(ctx, topic, agentMsg); err != nil {
						log.Error().Err(err).Str("agent", agentMsg.SourceAgentID).Msg("Failed to add agent to squad members")
					}
				}

			case topic == "mesh.responses.pending":
				agentMsg, err := parseAgentMessage(msg, headers)
				if err != nil {
					log.Warn().Err(err).Str("topic", topic).Msg("Failed to parse agent message")
					session.MarkMessage(msg, "")
					continue
				}
				messageBatch = append(messageBatch, agentMsg)

			case topic == "mesh.global.events":
				event, err := parseGlobalEvent(msg)
				if err != nil {
					log.Warn().Err(err).Msg("Failed to parse global event")
					session.MarkMessage(msg, "")
					continue
				}
				eventBatch = append(eventBatch, event)

				// Handle session creation events to create squads
				if err := h.handleSessionCreation(ctx, event); err != nil {
					log.Error().Err(err).Msg("Failed to handle session creation")
				}

			case topic == "sys.governance.alerts":
				alert, err := parseGovernanceAlert(msg)
				if err != nil {
					log.Warn().Err(err).Msg("Failed to parse governance alert")
					session.MarkMessage(msg, "")
					continue
				}
				alertBatch = append(alertBatch, alert)

			case topic == "sys.governance.signals":
				// Handle governance signals (STOP, PAUSE, RESUME)
				signal, err := parseGovernanceSignal(msg)
				if err != nil {
					log.Warn().Err(err).Msg("Failed to parse governance signal")
				} else {
					log.Info().
						Str("action", signal.Action).
						Str("target", signal.TargetAgentID).
						Msg("Received governance signal")
				}
			}

			session.MarkMessage(msg, "")

			// Flush if batch is full
			if len(messageBatch) >= h.batchSize || len(eventBatch) >= h.batchSize {
				flushBatches()
			}
		}
	}
}

func parseHeaders(headers []*sarama.RecordHeader) *models.KafkaHeaders {
	h := &models.KafkaHeaders{}
	for _, header := range headers {
		switch string(header.Key) {
		case "x-source-agent":
			h.SourceAgent = string(header.Value)
		case "x-dest-topic":
			h.DestTopic = string(header.Value)
		case "x-target-agent":
			h.TargetAgent = string(header.Value)
		case "x-interaction-type":
			h.InteractionType = string(header.Value)
		case "x-governance-status":
			h.GovernanceStatus = string(header.Value)
		case "x-correlation-id":
			h.CorrelationID = string(header.Value)
		case "x-conversation-id":
			h.ConversationID = string(header.Value)
		}
	}
	return h
}

func parseAgentMessage(msg *sarama.ConsumerMessage, headers *models.KafkaHeaders) (*models.AgentMessage, error) {
	var agentMsg models.AgentMessage
	if err := json.Unmarshal(msg.Value, &agentMsg); err != nil {
		return nil, err
	}

	// Enrich with Kafka metadata
	agentMsg.KafkaTopic = msg.Topic
	agentMsg.KafkaPartition = msg.Partition
	agentMsg.KafkaOffset = msg.Offset

	// Flatten headers from nested structure into top-level fields for DB insertion
	agentMsg.SourceAgentID = agentMsg.Headers.SourceAgentID
	agentMsg.TargetAgentID = agentMsg.Headers.TargetAgentID
	agentMsg.ConversationID = agentMsg.Headers.ConversationID
	agentMsg.InteractionType = agentMsg.Headers.InteractionType
	agentMsg.GovernanceStatus = agentMsg.Headers.GovernanceStatus

	// Use the interaction_type as message_type for now
	agentMsg.MessageType = agentMsg.Headers.InteractionType

	// Fallback to Kafka headers if nested headers are empty
	if agentMsg.SourceAgentID == "" && headers.SourceAgent != "" {
		agentMsg.SourceAgentID = headers.SourceAgent
	}
	if agentMsg.ConversationID == "" && headers.ConversationID != "" {
		agentMsg.ConversationID = headers.ConversationID
	}
	if agentMsg.InteractionType == "" && headers.InteractionType != "" {
		agentMsg.InteractionType = headers.InteractionType
		agentMsg.MessageType = headers.InteractionType
	}
	if agentMsg.GovernanceStatus == "" {
		agentMsg.GovernanceStatus = "PENDING"
		if headers.GovernanceStatus != "" {
			agentMsg.GovernanceStatus = headers.GovernanceStatus
		}
	}

	// If ConversationID is still empty, use the topic name (for session.* topics)
	if agentMsg.ConversationID == "" && strings.HasPrefix(msg.Topic, "session.") {
		agentMsg.ConversationID = msg.Topic
	}

	// Extract Tool Use Details
	if len(agentMsg.ToolCalls) > 0 {
		tool := agentMsg.ToolCalls[0]
		if name, ok := tool["function"].(map[string]interface{}); ok {
			// standard OpenAI format
			if n, ok := name["name"].(string); ok {
				agentMsg.ToolName = &n
			}
			if args, ok := name["arguments"].(string); ok {
				// arguments as string JSON
				var input map[string]interface{}
				if err := json.Unmarshal([]byte(args), &input); err == nil {
					agentMsg.ToolInput = input
				}
			} else if args, ok := name["arguments"].(map[string]interface{}); ok {
				agentMsg.ToolInput = args
			}
		} else if name, ok := tool["name"].(string); ok {
			// Simple format
			agentMsg.ToolName = &name
			if args, ok := tool["input"].(map[string]interface{}); ok {
				agentMsg.ToolInput = args
			} else if args, ok := tool["arguments"].(map[string]interface{}); ok {
				agentMsg.ToolInput = args
			}
		}
		// Capture output if present (usually in a separate message, but if bundled)
		if output, ok := tool["output"].(map[string]interface{}); ok {
			agentMsg.ToolOutput = output
		}
	}

	return &agentMsg, nil
}

func parseGlobalEvent(msg *sarama.ConsumerMessage) (*models.GlobalEvent, error) {
	var event models.GlobalEvent
	if err := json.Unmarshal(msg.Value, &event); err != nil {
		return nil, err
	}

	event.KafkaPartition = msg.Partition
	event.KafkaOffset = msg.Offset

	// If Timestamp wasn't parsed from JSON, try to extract from payload
	if event.Timestamp.IsZero() {
		if ts, ok := event.Payload["timestamp"].(string); ok {
			event.Timestamp.UnmarshalJSON([]byte("\"" + ts + "\""))
		}
	}

	return &event, nil
}

func parseGovernanceAlert(msg *sarama.ConsumerMessage) (*models.GovernanceAlert, error) {
	var alert models.GovernanceAlert
	if err := json.Unmarshal(msg.Value, &alert); err != nil {
		return nil, err
	}
	return &alert, nil
}

func parseGovernanceSignal(msg *sarama.ConsumerMessage) (*models.GovernanceSignal, error) {
	var signal models.GovernanceSignal
	if err := json.Unmarshal(msg.Value, &signal); err != nil {
		return nil, err
	}
	return &signal, nil
}

// handleSessionCreation creates a group_chat record when a session_created event is detected
func (h *consumerHandler) handleSessionCreation(ctx context.Context, event *models.GlobalEvent) error {
	// Check if this is a session_created event
	metadata, ok := event.Payload["metadata"].(map[string]interface{})
	if !ok {
		return nil
	}

	eventType, ok := metadata["type"].(string)
	if !ok || eventType != "session_created" {
		return nil
	}

	// Extract session details from metadata
	sessionID, _ := metadata["session_id"].(string)
	sessionTitle, _ := metadata["session_title"].(string)
	triggerSource, _ := metadata["trigger_source"].(string)
	priority, _ := metadata["priority"].(string)

	if sessionID == "" {
		return nil
	}

	// Extract briefing note from root payload content if available
	goalContent := ""
	if content, ok := event.Payload["content"].(string); ok {
		goalContent = content
	}

	if priority == "" {
		priority = "NORMAL"
	}

	// Store title in metadata since schema only has squad_name (ID)
	metadata["title"] = sessionTitle

	// Create group_chat record
	chat := &models.GroupChat{
		SquadName:       sessionID,
		Goal:            goalContent,
		Priority:        priority,
		Status:          "ACTIVE",
		KafkaInboxTopic: sessionID,
		TriggerSource:   &triggerSource,
		Metadata:        metadata,
		CreatedAt:       event.Timestamp,
	}

	if err := h.repo.UpsertGroupChat(ctx, chat); err != nil {
		return err
	}

	log.Info().Str("session_id", sessionID).Str("title", sessionTitle).Msg("GroupChat created from session_created event")
	return nil
}

func strPtr(s string) *string {
	return &s
}

// ensureGroupChatExists creates a group_chat record if it doesn't exist for the session topic
func (h *consumerHandler) ensureSquadExists(ctx context.Context, sessionTopic string, msg *models.AgentMessage) error {
	// Extract session name from topic (e.g., "session.ops_incident_1234" -> "ops_incident_1234")
	sessionID := sessionTopic

	// Create default goal if missing
	goal := "Auto-created session from topic discovery"
	if msg.InteractionType == "SESSION_BRIEFING" || msg.InteractionType == "INFO_UPDATE" {
		goal = msg.Content
	}

	chat := &models.GroupChat{
		SquadName:       sessionID,
		Goal:            goal,
		Priority:        "NORMAL",
		Status:          "ACTIVE",
		KafkaInboxTopic: sessionTopic,
		CreatedAt:       msg.Timestamp,
	}

	if err := h.repo.UpsertGroupChat(ctx, chat); err != nil {
		return err
	}

	log.Debug().Str("session_id", sessionID).Str("topic", sessionTopic).Msg("GroupChat ensured")
	return nil
}

// handleAgentJoined adds an agent to group_chat_members when AGENT_JOINED message is received
func (h *consumerHandler) handleAgentJoined(ctx context.Context, sessionTopic string, msg *models.AgentMessage) error {
	agentID := msg.SourceAgentID
	if agentID == "" {
		return nil
	}

	// Determine role - check if "Lead" is mentioned in content
	role := "MEMBER"
	if strings.Contains(strings.ToLower(msg.Content), "lead") || strings.Contains(agentID, "squad-lead") {
		role = "LEAD"
	}

	if err := h.repo.AddGroupChatMemberBySessionID(ctx, sessionTopic, agentID, role); err != nil {
		return err
	}

	log.Info().Str("agent_id", agentID).Str("session", sessionTopic).Str("role", role).Msg("Agent joined group chat")
	return nil
}

package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
	"github.com/shinox-lab/kafka2db/internal/config"
	"github.com/shinox-lab/kafka2db/internal/consumer"
	"github.com/shinox-lab/kafka2db/internal/db"
)

func main() {
	// Configure zerolog
	zerolog.TimeFieldFormat = zerolog.TimeFormatUnix
	log.Logger = log.Output(zerolog.ConsoleWriter{Out: os.Stderr})

	log.Info().Msg("Starting Kafka2DB Ingestion Service")

	// Load configuration
	cfg, err := config.Load()
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to load configuration")
	}

	// Initialize database connection pool
	dbPool, err := db.NewPool(context.Background(), cfg.DatabaseURL)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to connect to database")
	}
	defer dbPool.Close()

	log.Info().Msg("Connected to PostgreSQL")

	// Create message repository
	repo := db.NewRepository(dbPool)

	// Create and start consumer group
	consumerGroup, err := consumer.NewGroup(cfg, repo)
	if err != nil {
		log.Fatal().Err(err).Msg("Failed to create consumer group")
	}

	// Start consuming in background
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		if err := consumerGroup.Start(ctx); err != nil {
			log.Error().Err(err).Msg("Consumer group error")
		}
	}()

	log.Info().
		Strs("topics", cfg.Topics).
		Str("brokers", cfg.KafkaBrokers).
		Msg("Consumer started")

	// Wait for shutdown signal
	sigchan := make(chan os.Signal, 1)
	signal.Notify(sigchan, syscall.SIGINT, syscall.SIGTERM)
	<-sigchan

	log.Info().Msg("Shutting down...")
	cancel()
	consumerGroup.Close()
	log.Info().Msg("Shutdown complete")
}

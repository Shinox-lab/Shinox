package config

import (
	"os"
	"time"
)

type Config struct {
	// Kafka settings
	KafkaBrokers  string
	ConsumerGroup string
	Topics        []string

	// Database settings
	DatabaseURL string

	// Processing settings
	BatchSize    int
	BatchTimeout time.Duration

	// Worker settings
	NumWorkers int
}

func Load() (*Config, error) {
	cfg := &Config{
		KafkaBrokers:  getEnv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092"),
		ConsumerGroup: getEnv("KAFKA_CONSUMER_GROUP", "kafka2db-ingester"),
		// Only consume session.* topics (discovered dynamically) - no base topics needed
		Topics:       []string{},
		DatabaseURL:  getEnv("DATABASE_URL", "postgres://admin:adminpassword@localhost:5432/agentsquaddb?sslmode=disable"),
		BatchSize:    getEnvInt("BATCH_SIZE", 100),
		BatchTimeout: getEnvDuration("BATCH_TIMEOUT", 500*time.Millisecond),
		NumWorkers:   getEnvInt("NUM_WORKERS", 4),
	}

	return cfg, nil
}

func getEnv(key, fallback string) string {
	if value, ok := os.LookupEnv(key); ok {
		return value
	}
	return fallback
}

func getEnvInt(key string, fallback int) int {
	if value, ok := os.LookupEnv(key); ok {
		var result int
		if _, err := parseIntFromString(value, &result); err == nil {
			return result
		}
	}
	return fallback
}

func getEnvDuration(key string, fallback time.Duration) time.Duration {
	if value, ok := os.LookupEnv(key); ok {
		if d, err := time.ParseDuration(value); err == nil {
			return d
		}
	}
	return fallback
}

func parseIntFromString(s string, result *int) (bool, error) {
	var n int
	for _, c := range s {
		if c < '0' || c > '9' {
			return false, nil
		}
		n = n*10 + int(c-'0')
	}
	*result = n
	return true, nil
}

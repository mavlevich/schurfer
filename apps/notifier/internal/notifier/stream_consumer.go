package notifier

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
	"golang.org/x/time/rate"
)

const (
	StreamOutboxV1 = "notifications:outbox:v1"
	StreamDLQV1    = "notifications:outbox:v1:dlq"
	ConsumerGroup  = "notifier-delivery-v1"
	ConsumerName   = "notifier-worker-1" // Normally hostname or UUID

	MaxRetries = 3
)

// Envelope represents the notification-envelope-v1.schema.json
type Envelope struct {
	SchemaVersion  int       `json:"schema_version"`
	NotificationID string    `json:"notification_id"`
	DedupKey       string    `json:"dedup_key"`
	Producer       string    `json:"producer"`
	Kind           string    `json:"kind"`
	Severity       string    `json:"severity"`
	CreatedAt      time.Time `json:"created_at"`
	Payload        Payload   `json:"payload"`
}

type Payload struct {
	Text     string         `json:"text"`
	Metadata map[string]any `json:"metadata,omitempty"`
}

// payloadHash returns SHA256 of the normalized JSON payload
func payloadHash(p Payload) []byte {
	b, _ := json.Marshal(p)
	h := sha256.Sum256(b)
	return h[:]
}

// severityWeight helps sort severities
func severityWeight(s string) int {
	switch s {
	case "critical":
		return 4
	case "trade":
		return 3
	case "research":
		return 2
	case "info":
		return 1
	default:
		return 0
	}
}

type StreamConsumer struct {
	rdb         *redis.Client
	db          *pgxpool.Pool
	botToken    string
	chatID      string
	rateLimiter *rate.Limiter
}

func NewStreamConsumer(rdb *redis.Client, db *pgxpool.Pool, botToken, chatID string) *StreamConsumer {
	// 20 messages per minute rate limiting
	limit := rate.Every(time.Minute / 20)
	return &StreamConsumer{
		rdb:         rdb,
		db:          db,
		botToken:    botToken,
		chatID:      chatID,
		rateLimiter: rate.NewLimiter(limit, 1),
	}
}

func (c *StreamConsumer) setupGroup(ctx context.Context) error {
	err := c.rdb.XGroupCreateMkStream(ctx, StreamOutboxV1, ConsumerGroup, "0").Err()
	if err != nil && !redis.HasErrorPrefix(err, "BUSYGROUP") {
		return fmt.Errorf("create group: %w", err)
	}
	return nil
}

func (c *StreamConsumer) Run(ctx context.Context) {
	if err := c.setupGroup(ctx); err != nil {
		slog.Error("consumer.group_setup_failed", "error", err)
		return
	}

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		// Read up to 50 messages
		streams, err := c.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group:    ConsumerGroup,
			Consumer: ConsumerName,
			Streams:  []string{StreamOutboxV1, ">"},
			Count:    50,
			Block:    2 * time.Second,
		}).Result()

		if err != nil {
			if errors.Is(err, redis.Nil) {
				continue // timeout, loop again
			}
			slog.Error("consumer.read_failed", "error", err)
			time.Sleep(1 * time.Second)
			continue
		}

		for _, stream := range streams {
			msgs := stream.Messages
			// Priority sorting: critical > trade > research > info
			sort.SliceStable(msgs, func(i, j int) bool {
				envI := c.parseEnvelope(msgs[i])
				envJ := c.parseEnvelope(msgs[j])
				if envI == nil || envJ == nil {
					return false
				}
				return severityWeight(envI.Severity) > severityWeight(envJ.Severity)
			})

			for _, msg := range msgs {
				c.processMessage(ctx, msg)
			}
		}
	}
}

func (c *StreamConsumer) parseEnvelope(msg redis.XMessage) *Envelope {
	dataRaw, ok := msg.Values["data"].(string)
	if !ok {
		return nil
	}
	var env Envelope
	if err := json.Unmarshal([]byte(dataRaw), &env); err != nil {
		return nil
	}
	return &env
}

func (c *StreamConsumer) processMessage(ctx context.Context, msg redis.XMessage) {
	env := c.parseEnvelope(msg)
	if env == nil {
		slog.Warn("consumer.invalid_envelope", "id", msg.ID)
		c.moveToDLQ(ctx, msg, "invalid envelope data format")
		return
	}

	hash := payloadHash(env.Payload)

	// DB transaction to check deduplication & state
	tx, err := c.db.Begin(ctx)
	if err != nil {
		slog.Error("consumer.db_begin_failed", "error", err)
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()

	var (
		status       string
		attemptCount int
		storedHash   []byte
	)

	// Upsert to app.notification_deliveries (returning needed fields)
	err = tx.QueryRow(ctx, `
		INSERT INTO app.notification_deliveries (
			notification_id, envelope_version, producer, kind, severity,
			dedup_key, channel, stream_entry_id, status, attempt_count,
			first_enqueued_at, payload_hash
		) VALUES (
			$1, $2, $3, $4, $5, $6, 'telegram', $7, 'pending', 0, $8, $9
		)
		ON CONFLICT (producer, dedup_key) DO UPDATE SET updated_at = now()
		RETURNING status, attempt_count, payload_hash
	`, env.NotificationID, env.SchemaVersion, env.Producer, env.Kind, env.Severity,
		env.DedupKey, msg.ID, env.CreatedAt, hash).Scan(&status, &attemptCount, &storedHash)

	if err != nil {
		slog.Error("consumer.db_upsert_failed", "error", err)
		return
	}

	// 2. Treat mismatched payload hash as conflict
	if string(storedHash) != string(hash) {
		slog.Warn("consumer.hash_conflict", "dedup_key", env.DedupKey)
		_ = tx.Commit(ctx)
		c.moveToDLQ(ctx, msg, "mismatched payload hash for same dedup_key")
		return
	}

	// 3. Skip if already delivered
	if status == "delivered" {
		_ = tx.Commit(ctx)
		c.ackAndDel(ctx, msg.ID)
		return
	}

	// Enforce rate limits before hitting Telegram
	if err := c.rateLimiter.Wait(ctx); err != nil {
		slog.Error("consumer.rate_limit_failed", "error", err)
		return
	}

	// 4. Bounded retries (update attempt_count)
	attemptCount++
	_, err = tx.Exec(ctx, `
		UPDATE app.notification_deliveries
		SET attempt_count = $1, last_attempted_at = now()
		WHERE producer = $2 AND dedup_key = $3
	`, attemptCount, env.Producer, env.DedupKey)

	if err != nil {
		slog.Error("consumer.db_update_attempt_failed", "error", err)
		return
	}

	// Attempt delivery
	parseMode, _ := env.Payload.Metadata["parse_mode"].(string)
	sendErr := sendMessage(ctx, env.Payload.Text, c.botToken, c.chatID, parseMode)

	if sendErr == nil {
		// 5. On success, mark delivered in PG, XACK + XDEL
		_, _ = tx.Exec(ctx, `
			UPDATE app.notification_deliveries
			SET status = 'delivered', delivered_at = now()
			WHERE producer = $1 AND dedup_key = $2
		`, env.Producer, env.DedupKey)
		_ = tx.Commit(ctx)
		c.ackAndDel(ctx, msg.ID)
		return
	}

	// Send failed
	slog.Warn("consumer.delivery_failed", "error", sendErr, "attempt", attemptCount)

	if attemptCount >= MaxRetries {
		// 6. On fail out of retries, mark failed in PG, move to DLQ, XACK + XDEL
		_, _ = tx.Exec(ctx, `
			UPDATE app.notification_deliveries
			SET status = 'failed', last_error = $1
			WHERE producer = $2 AND dedup_key = $3
		`, sendErr.Error(), env.Producer, env.DedupKey)
		_ = tx.Commit(ctx)
		c.moveToDLQ(ctx, msg, sendErr.Error())
	} else {
		_ = tx.Commit(ctx)
	}
}

func (c *StreamConsumer) moveToDLQ(ctx context.Context, msg redis.XMessage, reason string) {
	dataRaw, _ := msg.Values["data"].(string)
	err := c.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: StreamDLQV1,
		Values: map[string]interface{}{
			"data":   dataRaw,
			"reason": reason,
		},
	}).Err()
	if err != nil {
		slog.Error("consumer.dlq_add_failed", "error", err)
		return
	}
	c.ackAndDel(ctx, msg.ID)
}

func (c *StreamConsumer) ackAndDel(ctx context.Context, id string) {
	c.rdb.XAck(ctx, StreamOutboxV1, ConsumerGroup, id)
	c.rdb.XDel(ctx, StreamOutboxV1, id)
}

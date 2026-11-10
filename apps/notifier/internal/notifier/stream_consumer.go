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

	"github.com/jackc/pgx/v5"
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

	// Retries. A message whose send fails stays in this consumer's pending list (it is
	// not acknowledged), and the loop claims it back with XAUTOCLAIM once it has been
	// idle for reclaimMinIdle, a small batch every reclaimInterval, between reads of new
	// messages. Each claim is one more attempt, counted in PostgreSQL against MaxRetries.
	reclaimInterval = 30 * time.Second
	reclaimMinIdle  = 60 * time.Second
	reclaimBatch    = 10
	// A retry of an envelope older than this is not sent: it is recorded as failed and
	// moved to the dead-letter stream. A stale alert is noise, and this keeps a backlog
	// left by the old consumer (which never retried) from being sent on rollout.
	retryMaxAge = 24 * time.Hour
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
		return 5
	case "warning":
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

	minIdle       time.Duration // reclaimMinIdle; tests shorten it
	reclaimCursor string
	now           func() time.Time
}

func NewStreamConsumer(rdb *redis.Client, db *pgxpool.Pool, botToken, chatID string) *StreamConsumer {
	// 20 messages per minute rate limiting
	limit := rate.Every(time.Minute / 20)
	return &StreamConsumer{
		rdb:           rdb,
		db:            db,
		botToken:      botToken,
		chatID:        chatID,
		rateLimiter:   rate.NewLimiter(limit, 1),
		minIdle:       reclaimMinIdle,
		reclaimCursor: "0-0",
		now:           time.Now,
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

	lastReclaim := time.Time{}
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		// One small batch of retries per interval, then back to new messages.
		if c.now().Sub(lastReclaim) >= reclaimInterval {
			c.reclaimOnce(ctx)
			lastReclaim = c.now()
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
			// Priority sorting: critical > warning > trade > research > info
			sort.SliceStable(msgs, func(i, j int) bool {
				envI := c.parseEnvelope(msgs[i])
				envJ := c.parseEnvelope(msgs[j])
				if envI == nil || envJ == nil {
					return false
				}
				return severityWeight(envI.Severity) > severityWeight(envJ.Severity)
			})

			for _, msg := range msgs {
				c.processMessage(ctx, msg, false)
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

// processMessage handles one message; reclaimed is true when it came back through
// XAUTOCLAIM rather than as a new entry.
func (c *StreamConsumer) processMessage(ctx context.Context, msg redis.XMessage, reclaimed bool) {
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
		if !c.commit(ctx, tx) {
			return
		}
		c.moveToDLQ(ctx, msg, "mismatched payload hash for same dedup_key")
		return
	}

	// 3. Terminal rows: finish what is left in Redis, never send again. A failed row
	// whose move to the dead-letter stream did not complete is finished here.
	switch status {
	case "delivered":
		if c.commit(ctx, tx) {
			c.ackAndDel(ctx, msg.ID)
		}
		return
	case "failed":
		if c.commit(ctx, tx) {
			c.moveToDLQ(ctx, msg, "already failed; finishing the dead-letter move")
		}
		return
	}

	// 4. A retry that is out of attempts or too old is recorded as failed, not sent.
	if attemptCount >= MaxRetries {
		c.fail(ctx, tx, msg, env, "retry limit reached before this attempt")
		return
	}
	// The age limit applies to every reclaimed message, even one whose first attempt
	// never reached PostgreSQL (attempt count still 0), and to any recorded retry.
	if (reclaimed || attemptCount > 0) && !env.CreatedAt.IsZero() &&
		c.now().Sub(env.CreatedAt) > retryMaxAge {
		c.fail(ctx, tx, msg, env, fmt.Sprintf("retry expired: created %s, older than %s",
			env.CreatedAt.UTC().Format(time.RFC3339), retryMaxAge))
		return
	}

	// Enforce rate limits before hitting Telegram
	if err := c.rateLimiter.Wait(ctx); err != nil {
		slog.Error("consumer.rate_limit_failed", "error", err)
		return
	}

	// 5. Bounded retries (update attempt_count)
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
		// 6. On success, mark delivered in PG; acknowledge only once that is committed.
		// If the commit fails the message stays pending and a later claim sends it again
		// (at least once, never silently lost).
		_, err = tx.Exec(ctx, `
			UPDATE app.notification_deliveries
			SET status = 'delivered', delivered_at = now()
			WHERE producer = $1 AND dedup_key = $2
		`, env.Producer, env.DedupKey)
		if err != nil {
			slog.Error("consumer.db_mark_delivered_failed", "error", err)
			return
		}
		if c.commit(ctx, tx) {
			c.ackAndDel(ctx, msg.ID)
		}
		return
	}

	// Send failed
	slog.Warn("consumer.delivery_failed", "error", sendErr, "attempt", attemptCount)

	if attemptCount >= MaxRetries {
		// 7. Out of retries: record failed, then move to the dead-letter stream.
		c.fail(ctx, tx, msg, env, sendErr.Error())
		return
	}
	// Not acknowledged: the message stays pending and is claimed back later.
	c.commit(ctx, tx)
}

// commit reports whether the transaction committed; on failure the caller leaves the
// message unacknowledged, so it is claimed again later.
func (c *StreamConsumer) commit(ctx context.Context, tx pgx.Tx) bool {
	if err := tx.Commit(ctx); err != nil {
		slog.Error("consumer.db_commit_failed", "error", err)
		return false
	}
	return true
}

// fail records the final failure and only then moves the message to the dead-letter
// stream (which acknowledges it). If the record does not commit, nothing is acknowledged.
func (c *StreamConsumer) fail(ctx context.Context, tx pgx.Tx, msg redis.XMessage, env *Envelope, reason string) {
	_, err := tx.Exec(ctx, `
		UPDATE app.notification_deliveries
		SET status = 'failed', last_error = $1
		WHERE producer = $2 AND dedup_key = $3
	`, reason, env.Producer, env.DedupKey)
	if err != nil {
		slog.Error("consumer.db_mark_failed_failed", "error", err)
		return
	}
	if c.commit(ctx, tx) {
		c.moveToDLQ(ctx, msg, reason)
	}
}

// reclaimOnce claims one batch of this group's messages idle for at least minIdle and
// processes each as one more attempt.
func (c *StreamConsumer) reclaimOnce(ctx context.Context) {
	msgs, next, err := c.rdb.XAutoClaim(ctx, &redis.XAutoClaimArgs{
		Stream:   StreamOutboxV1,
		Group:    ConsumerGroup,
		Consumer: ConsumerName,
		MinIdle:  c.minIdle,
		Start:    c.reclaimCursor,
		Count:    reclaimBatch,
	}).Result()
	if err != nil {
		slog.Error("consumer.reclaim_failed", "error", err)
		return
	}
	c.reclaimCursor = next
	for _, msg := range msgs {
		c.processMessage(ctx, msg, true)
	}
}

// moveToDLQScript moves one outbox entry to the dead-letter stream: the dead letter is
// added once per source ID, and only then is the source acknowledged and deleted.
//
// A Lua script is not rolled back when a command inside it fails: writes made before
// the error stay. So the marker is written only after XADD has succeeded, and it means
// "the dead letter exists". If XADD fails, the script stops with no marker and the
// source still pending, and the next attempt adds the dead letter. If the marker write
// itself fails after XADD, the next attempt adds a second dead letter (a duplicate,
// never a loss). A repeated move after success adds nothing.
var moveToDLQScript = redis.NewScript(`
if redis.call('EXISTS', KEYS[3]) == 0 then
    redis.call('XADD', KEYS[2], '*', 'data', ARGV[3], 'reason', ARGV[4], 'source_id', ARGV[2])
    redis.call('SET', KEYS[3], ARGV[2], 'PX', ARGV[5])
end
local acked = redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[1], ARGV[2])
return acked
`)

// dlqMarkerTTL keeps a moved source ID's marker for longer than any retry can live.
const dlqMarkerTTL = 30 * 24 * time.Hour

func dlqMarkerKey(sourceID string) string {
	return StreamDLQV1 + ":source:" + sourceID
}

func (c *StreamConsumer) moveToDLQ(ctx context.Context, msg redis.XMessage, reason string) {
	dataRaw, _ := msg.Values["data"].(string)
	err := moveToDLQScript.Run(ctx, c.rdb,
		[]string{StreamOutboxV1, StreamDLQV1, dlqMarkerKey(msg.ID)},
		ConsumerGroup, msg.ID, dataRaw, reason, dlqMarkerTTL.Milliseconds(),
	).Err()
	if err != nil {
		// Nothing was changed: the message stays pending and is claimed again.
		slog.Error("consumer.dlq_move_failed", "error", err)
	}
}

// ackAndDel deletes the source entry only once it is acknowledged; after a failed
// XACK the entry stays, so it is claimed again rather than lost.
func (c *StreamConsumer) ackAndDel(ctx context.Context, id string) {
	if err := c.rdb.XAck(ctx, StreamOutboxV1, ConsumerGroup, id).Err(); err != nil {
		slog.Error("consumer.ack_failed", "error", err)
		return
	}
	if err := c.rdb.XDel(ctx, StreamOutboxV1, id).Err(); err != nil {
		slog.Error("consumer.delete_failed", "error", err)
	}
}

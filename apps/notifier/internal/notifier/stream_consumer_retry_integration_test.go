package notifier

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
	"golang.org/x/time/rate"
)

// retryHarness runs the consumer against the disposable Postgres, a miniredis stream
// and a local Telegram stand-in whose answer each test controls.
type retryHarness struct {
	t      *testing.T
	ctx    context.Context
	pool   *pgxpool.Pool
	rdb    *redis.Client
	sent   atomic.Int64
	failOK atomic.Bool // true: the stand-in answers 500
	ids    []string
}

func newRetryHarness(t *testing.T) *retryHarness {
	t.Helper()
	pool := notificationTestPool(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	t.Cleanup(cancel)
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	h := &retryHarness{t: t, ctx: ctx, pool: pool, rdb: rdb}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		h.sent.Add(1)
		if h.failOK.Load() {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(server.Close)
	previousAPI := _telegramAPI
	_telegramAPI = server.URL + "/bot%s/sendMessage"
	t.Cleanup(func() { _telegramAPI = previousAPI })
	t.Cleanup(func() {
		_, err := pool.Exec(context.Background(), `DELETE FROM app.notification_deliveries
			WHERE notification_id::text = ANY($1::text[])`, h.ids)
		if err != nil {
			t.Error("failed to clean notification test rows")
		}
	})
	return h
}

// consumer is a fresh consumer process: a restart keeps only what Redis and Postgres hold.
func (h *retryHarness) consumer(pool *pgxpool.Pool) *StreamConsumer {
	c := NewStreamConsumer(h.rdb, pool, "test-token", "test-chat")
	c.rateLimiter = rate.NewLimiter(rate.Inf, 1)
	c.minIdle = 0
	if err := c.setupGroup(h.ctx); err != nil {
		h.t.Fatal(err)
	}
	return c
}

func (h *retryHarness) publish(createdAt time.Time) Envelope {
	h.t.Helper()
	env := newEnvelope("test", "operational_health", "warning", "retry-"+uuid.NewString(), "retry test", nil)
	env.CreatedAt = createdAt
	raw, err := json.Marshal(env)
	if err != nil {
		h.t.Fatal(err)
	}
	if err := h.rdb.XAdd(h.ctx, &redis.XAddArgs{Stream: StreamOutboxV1,
		Values: map[string]any{"data": string(raw)}}).Err(); err != nil {
		h.t.Fatal(err)
	}
	h.ids = append(h.ids, env.NotificationID)
	return env
}

// readNew processes the messages never delivered to the group, as the main loop does.
func (h *retryHarness) readNew(c *StreamConsumer) {
	h.t.Helper()
	streams, err := h.rdb.XReadGroup(h.ctx, &redis.XReadGroupArgs{
		Group: ConsumerGroup, Consumer: ConsumerName,
		Streams: []string{StreamOutboxV1, ">"}, Count: 50, Block: -1,
	}).Result()
	if err != nil && !errors.Is(err, redis.Nil) {
		h.t.Fatal(err)
	}
	for _, stream := range streams {
		for _, msg := range stream.Messages {
			c.processMessage(h.ctx, msg)
		}
	}
}

func (h *retryHarness) row(env Envelope) (status string, attempts int, lastError string) {
	h.t.Helper()
	err := h.pool.QueryRow(h.ctx, `SELECT status, attempt_count, coalesce(last_error, '')
		FROM app.notification_deliveries WHERE producer = $1 AND dedup_key = $2`,
		env.Producer, env.DedupKey).Scan(&status, &attempts, &lastError)
	if err != nil {
		h.t.Fatal(err)
	}
	return status, attempts, lastError
}

func (h *retryHarness) pending() int64 {
	h.t.Helper()
	summary, err := h.rdb.XPending(h.ctx, StreamOutboxV1, ConsumerGroup).Result()
	if err != nil {
		h.t.Fatal(err)
	}
	return summary.Count
}

func TestRetryAfterRestartDeliversATransientFailure(t *testing.T) {
	h := newRetryHarness(t)
	env := h.publish(time.Now())
	h.failOK.Store(true)
	h.readNew(h.consumer(h.pool))
	if status, attempts, _ := h.row(env); status != "pending" || attempts != 1 || h.pending() != 1 {
		t.Fatalf("after a failed send: status %s attempts %d pending %d", status, attempts, h.pending())
	}
	h.failOK.Store(false)
	h.consumer(h.pool).reclaimOnce(h.ctx) // a restarted process claims it back
	if status, attempts, _ := h.row(env); status != "delivered" || attempts != 2 {
		t.Fatalf("after the retry: status %s attempts %d", status, attempts)
	}
	if h.pending() != 0 || h.rdb.XLen(h.ctx, StreamOutboxV1).Val() != 0 || h.sent.Load() != 2 {
		t.Fatalf("pending %d, stream %d, Telegram requests %d", h.pending(),
			h.rdb.XLen(h.ctx, StreamOutboxV1).Val(), h.sent.Load())
	}
}

func TestExhaustedRetriesEndFailedInTheDeadLetterStream(t *testing.T) {
	h := newRetryHarness(t)
	env := h.publish(time.Now())
	h.failOK.Store(true)
	c := h.consumer(h.pool)
	h.readNew(c)
	c.reclaimOnce(h.ctx)
	c.reclaimOnce(h.ctx)
	status, attempts, lastError := h.row(env)
	if status != "failed" || attempts != MaxRetries || lastError == "" {
		t.Fatalf("status %s attempts %d last_error %q", status, attempts, lastError)
	}
	if h.pending() != 0 || h.rdb.XLen(h.ctx, StreamDLQV1).Val() != 1 || h.sent.Load() != MaxRetries {
		t.Fatalf("pending %d, dead letters %d, Telegram requests %d", h.pending(),
			h.rdb.XLen(h.ctx, StreamDLQV1).Val(), h.sent.Load())
	}
	c.reclaimOnce(h.ctx)
	if h.sent.Load() != MaxRetries {
		t.Fatalf("a failed message was sent again: %d requests", h.sent.Load())
	}
}

func TestPostgresFailureLeavesTheMessageUnacknowledged(t *testing.T) {
	h := newRetryHarness(t)
	env := h.publish(time.Now())
	broken, err := pgxpool.NewWithConfig(h.ctx, h.pool.Config())
	if err != nil {
		t.Fatal(err)
	}
	broken.Close() // every query fails, as a lost database connection would
	h.readNew(h.consumer(broken))
	if h.pending() != 1 || h.sent.Load() != 0 {
		t.Fatalf("pending %d, Telegram requests %d", h.pending(), h.sent.Load())
	}
	h.consumer(h.pool).reclaimOnce(h.ctx)
	if status, _, _ := h.row(env); status != "delivered" || h.pending() != 0 {
		t.Fatalf("after recovery: status %s pending %d", status, h.pending())
	}
}

func TestOldBacklogIsRetiredWithoutTelegramRequests(t *testing.T) {
	h := newRetryHarness(t)
	old := h.publish(time.Now().Add(-48 * time.Hour)) // stuck under the old consumer
	h.failOK.Store(true)
	h.readNew(h.consumer(h.pool)) // its one recorded attempt
	stuckFailed := h.publish(time.Now())
	h.readNew(h.consumer(h.pool))
	// A row already failed in Postgres whose dead-letter move never happened.
	if _, err := h.pool.Exec(h.ctx, `UPDATE app.notification_deliveries
		SET status = 'failed', last_error = 'set by test'
		WHERE producer = $1 AND dedup_key = $2`, stuckFailed.Producer, stuckFailed.DedupKey); err != nil {
		t.Fatal(err)
	}
	before := h.sent.Load()
	h.failOK.Store(false)
	h.consumer(h.pool).reclaimOnce(h.ctx)
	if h.sent.Load() != before {
		t.Fatalf("backlog produced %d Telegram requests", h.sent.Load()-before)
	}
	if status, _, lastError := h.row(old); status != "failed" || lastError == "" {
		t.Fatalf("old backlog row: status %s last_error %q", status, lastError)
	}
	if status, _, lastError := h.row(stuckFailed); status != "failed" || lastError != "set by test" {
		t.Fatalf("failed row: status %s last_error %q", status, lastError)
	}
	if h.pending() != 0 || h.rdb.XLen(h.ctx, StreamDLQV1).Val() != 2 {
		t.Fatalf("pending %d, dead letters %d", h.pending(), h.rdb.XLen(h.ctx, StreamDLQV1).Val())
	}
}

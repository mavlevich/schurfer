package notifier

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"regexp"
	"sync/atomic"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
	"golang.org/x/time/rate"
)

func notificationTestPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	raw := os.Getenv("SCHURFER_TEST_DATABASE_URL")
	switch {
	case raw == "" && os.Getenv("GITHUB_ACTIONS") == "true":
		raw = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"
	case raw == "":
		if os.Getenv("REQUIRE_INTEGRATION_DB") == "1" {
			t.Fatal("SCHURFER_TEST_DATABASE_URL must name a disposable test database")
		}
		t.Skip("set SCHURFER_TEST_DATABASE_URL to a migrated disposable loopback database")
	default:
		parsed, err := url.Parse(raw)
		if err != nil || parsed.Hostname() != "127.0.0.1" || parsed.User == nil ||
			parsed.User.Username() != "schurfer" || parsed.RawQuery != "" || parsed.Fragment != "" ||
			(parsed.Scheme != "postgres" && parsed.Scheme != "postgresql") ||
			!regexp.MustCompile(`^/schurfer_verify_[0-9a-f]{12}$`).MatchString(parsed.Path) {
			t.Fatal("refusing a non-disposable test database")
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, raw)
	if err != nil {
		t.Fatal("cannot configure test database")
	}
	t.Cleanup(pool.Close)
	if err := pool.Ping(ctx); err != nil {
		t.Fatal("disposable test database unavailable")
	}
	return pool
}

// Exercise the actual consumer SQL and unique (producer, dedup_key) constraint:
// counting stream envelopes alone hid this regression in the previous tests.
func TestHealthIncidentsDeliveredAgainstRealPostgres(t *testing.T) {
	pool := notificationTestPool(t)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	n := newTestNotifier(t, miniredis.RunT(t), "test-token", "test-chat")
	var sent atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		sent.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(server.Close)
	previousAPI := _telegramAPI
	_telegramAPI = server.URL + "/bot%s/sendMessage"
	t.Cleanup(func() { _telegramAPI = previousAPI })
	consumer := NewStreamConsumer(n.rdb, pool, "test-token", "test-chat")
	consumer.rateLimiter = rate.NewLimiter(rate.Inf, 1)
	if err := consumer.setupGroup(ctx); err != nil {
		t.Fatal(err)
	}
	var notificationIDs []string
	t.Cleanup(func() {
		_, err := pool.Exec(context.Background(), `DELETE FROM app.notification_deliveries
			WHERE notification_id::text = ANY($1::text[])`, notificationIDs)
		if err != nil {
			t.Error("failed to clean notification test rows")
		}
	})
	drain := func() {
		t.Helper()
		streams, err := n.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group: ConsumerGroup, Consumer: ConsumerName,
			Streams: []string{StreamOutboxV1, ">"}, Count: 50, Block: -1,
		}).Result()
		if err != nil {
			t.Fatal(err)
		}
		for _, stream := range streams {
			for _, message := range stream.Messages {
				envelope := consumer.parseEnvelope(message)
				notificationIDs = append(notificationIDs, envelope.NotificationID)
				consumer.processMessage(ctx, message)
				consumer.processMessage(ctx, message) // duplicate stream delivery
			}
		}
	}
	// Old, already-delivered static keys must not swallow new incidents on rollout.
	for _, key := range []string{"scanner_stale", "source_unhealthy"} {
		if err := n.publishEnvelope(ctx, "scanner", "scanner.stale", "critical", key, "legacy alert", nil); err != nil {
			t.Fatal(err)
		}
	}
	drain()
	for range 2 {
		n.reportStaleness(ctx, true, "stale")
		n.reportStaleness(ctx, true, "updated reason")
		n.reportStaleness(ctx, false, "")
		n.reportSourceLeadStaleHealth(ctx, 1)
		n.reportSourceLeadStaleHealth(ctx, 2)
		n.reportSourceLeadStaleHealth(ctx, 0)
	}
	for _, id := range []int64{42, 43, 42} {
		n.reportSourceLeadCriticalFailure(ctx, id)
	}
	drain()
	// A capture's immutable identity also deduplicates after its Redis marker is lost.
	if err := n.rdb.Del(ctx, redisKeySourceLeadFailureSeen+"42").Err(); err != nil {
		t.Fatal(err)
	}
	n.reportSourceLeadCriticalFailure(ctx, 42)
	drain()
	if got := sent.Load(); got != 12 {
		t.Fatalf("local Telegram requests = %d, want 12", got)
	}
	var delivered int
	err := pool.QueryRow(ctx, `SELECT count(*) FROM app.notification_deliveries
		WHERE notification_id::text = ANY($1::text[]) AND status = 'delivered'`, notificationIDs).Scan(&delivered)
	if err != nil || delivered != 12 {
		t.Fatalf("delivered audit rows = %d, want 12; error: %v", delivered, err)
	}
	if got := n.rdb.XLen(ctx, StreamDLQV1).Val(); got != 0 {
		t.Fatalf("unexpected dead-letter messages: %d", got)
	}
}

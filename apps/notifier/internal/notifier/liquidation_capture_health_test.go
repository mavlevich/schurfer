package notifier

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func newTestLiquidationMonitor(
	t *testing.T,
	rdb *redis.Client,
	now time.Time,
) *liquidationCaptureMonitor {
	t.Helper()
	return &liquidationCaptureMonitor{
		notifier: &Notifier{rdb: rdb}, exchanges: []string{"bybit"},
		missingGrace: 90 * time.Second, now: func() time.Time { return now },
	}
}

func TestParseMonitoredExchangesNormalizesAndDeduplicates(t *testing.T) {
	got := parseMonitoredExchanges(" BYBIT,binance,bybit,unknown ")
	if len(got) != 2 || got[0] != "bybit" || got[1] != "binance" {
		t.Fatalf("exchanges = %v", got)
	}
}

func TestLiquidationCaptureMonitorMissingHealthUsesPersistentGrace(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)

	first := newTestLiquidationMonitor(t, rdb, now)
	first.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 0 {
		t.Fatalf("cold-start grace emitted %d messages", len(got))
	}
	missingSince := mr.HGet(liquidationMonitorStateKey("bybit"), "missing_since_ms")
	if missingSince == "" {
		t.Fatal("missing_since_ms was not persisted")
	}

	// A notifier restart must not reset the grace window.
	restarted := newTestLiquidationMonitor(t, rdb, now.Add(91*time.Second))
	restarted.checkExchange(ctx, "bybit")
	messages := outboxMessages(t, rdb)
	if len(messages) != 1 {
		t.Fatalf("messages after persistent grace = %d", len(messages))
	}
	if env := decodeEnvelope(t, messages[0]); env.Severity != "critical" {
		t.Fatalf("missing health severity = %s", env.Severity)
	}
}

func TestLiquidationCaptureMonitorRecoveryAndRestartAreIdempotent(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	if err := rdb.HSet(ctx, liquidationMonitorStateKey("bybit"), map[string]any{
		"state": "critical", "transition_id": "missing:1",
	}).Err(); err != nil {
		t.Fatal(err)
	}
	setLiquidationHealth(t, rdb, "ok", "", "session-2", now.UnixMilli())
	monitor.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("recovery messages = %d", len(got))
	}

	// Recreating the monitor simulates a notifier restart. Redis state and the
	// transition claim prevent a duplicate recovery.
	restarted := newTestLiquidationMonitor(t, rdb, now.Add(time.Second))
	restarted.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("restart duplicated recovery: %d messages", len(got))
	}
}

func TestLiquidationCaptureMonitorAlertsEveryFatalSessionExactlyOnce(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	addFatalIncident(t, rdb, "bybit", "session-1", now, "queue_drop_critical")
	monitor.checkFatalIncidents(ctx, "bybit")
	addFatalIncident(t, rdb, "bybit", "session-2", now.Add(time.Second), "fatal_payload_mismatch")
	monitor.checkFatalIncidents(ctx, "bybit")
	monitor.checkFatalIncidents(ctx, "bybit")

	messages := outboxMessages(t, rdb)
	if len(messages) != 2 {
		t.Fatalf("fatal incident messages = %d, want one per session", len(messages))
	}
	for _, message := range messages {
		if env := decodeEnvelope(t, message); env.Severity != "critical" {
			t.Fatalf("fatal severity = %s", env.Severity)
		}
	}
}

func TestLiquidationCaptureMonitorFatalRestartRecoversOnlyAfterOk(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	addFatalIncident(t, rdb, "bybit", "session-failed", now, "queue_drop_critical")
	setLiquidationHealth(t, rdb, "starting", "awaiting_first_complete_minute", "session-new", now.Add(time.Second).UnixMilli())
	monitor.checkHealth(ctx)
	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("fatal plus starting produced %d messages", len(got))
	}

	setLiquidationHealth(t, rdb, "ok", "", "session-new", now.Add(time.Minute).UnixMilli())
	monitor.checkHealth(ctx)
	messages := outboxMessages(t, rdb)
	if len(messages) != 2 {
		t.Fatalf("fatal recovery produced %d messages", len(messages))
	}
	if env := decodeEnvelope(t, messages[1]); env.Severity != "info" {
		t.Fatalf("recovery severity = %s", env.Severity)
	}
}

func TestLiquidationCaptureMonitorSameSeverityNewTransitionAlertsAgain(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealth(t, rdb, "degraded", "disconnected_streams", "session-1", now.UnixMilli())
	monitor.checkExchange(ctx, "bybit")
	setLiquidationHealth(t, rdb, "degraded", "persist_error", "session-1", now.Add(time.Minute).UnixMilli())
	monitor.checkExchange(ctx, "bybit")

	if got := outboxMessages(t, rdb); len(got) != 2 {
		t.Fatalf("distinct degraded transitions produced %d messages", len(got))
	}
}

func setLiquidationHealth(
	t *testing.T,
	rdb *redis.Client,
	status string,
	reason string,
	sessionID string,
	changedAtMs int64,
) {
	t.Helper()
	if err := rdb.HSet(context.Background(), liquidationHealthKey("bybit"), map[string]any{
		"status": status, "reason_codes": reason, "process_session_id": sessionID,
		"status_changed_at_ms": changedAtMs, "updated_at_ms": changedAtMs,
	}).Err(); err != nil {
		t.Fatal(err)
	}
}

func addFatalIncident(
	t *testing.T,
	rdb *redis.Client,
	exchange string,
	sessionID string,
	when time.Time,
	reason string,
) {
	t.Helper()
	ctx := context.Background()
	if err := rdb.HSet(ctx, liquidationIncidentKey(exchange, sessionID), map[string]any{
		"exchange": exchange, "process_session_id": sessionID,
		"occurred_at_ms": when.UnixMilli(), "reason_codes": reason,
	}).Err(); err != nil {
		t.Fatal(err)
	}
	if err := rdb.ZAdd(ctx, liquidationIncidentIndexKey(exchange), redis.Z{
		Score: float64(when.UnixMilli()), Member: sessionID,
	}).Err(); err != nil {
		t.Fatal(err)
	}
}

func outboxMessages(t *testing.T, rdb *redis.Client) []redis.XMessage {
	t.Helper()
	messages, err := rdb.XRange(context.Background(), StreamOutboxV1, "-", "+").Result()
	if err != nil {
		t.Fatal(err)
	}
	return messages
}

func decodeEnvelope(t *testing.T, message redis.XMessage) Envelope {
	t.Helper()
	var env Envelope
	if err := json.Unmarshal([]byte(message.Values["data"].(string)), &env); err != nil {
		t.Fatal(err)
	}
	return env
}

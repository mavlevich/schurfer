package notifier

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestLiquidationCaptureMonitor(t *testing.T) {
	mr, err := miniredis.Run()
	if err != nil {
		t.Fatalf("miniredis: %v", err)
	}
	defer mr.Close()

	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	notifier := &Notifier{rdb: rdb}

	monitor := &liquidationCaptureMonitor{
		notifier:     notifier,
		exchanges:    []string{"bybit"},
		lastAlerts:   make(map[string]string),
		missingSince: make(map[string]time.Time),
	}
	ctx := context.Background()

	// 1. Initially missing, should wait during grace period
	monitor.checkExchange(ctx, "bybit")
	if len(monitor.missingSince) == 0 {
		t.Errorf("expected missingSince to be set")
	}
	if monitor.lastAlerts["bybit"] != "" {
		t.Errorf("expected no alert during grace, got %s", monitor.lastAlerts["bybit"])
	}

	// 2. Exceed grace period -> should alert critical
	monitor.missingSince["bybit"] = time.Now().Add(-100 * time.Second)
	monitor.checkExchange(ctx, "bybit")
	if monitor.lastAlerts["bybit"] != "critical" {
		t.Errorf("expected critical after grace, got %s", monitor.lastAlerts["bybit"])
	}

	// Check outbox
	messages, err := rdb.XRange(ctx, StreamOutboxV1, "-", "+").Result()
	if err != nil {
		t.Fatalf("xrange: %v", err)
	}
	if len(messages) != 1 {
		t.Fatalf("expected 1 message in outbox, got %d", len(messages))
	}

	var env Envelope
	if err := json.Unmarshal([]byte(messages[0].Values["data"].(string)), &env); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if env.Severity != "critical" {
		t.Errorf("expected severity critical, got %s", env.Severity)
	}

	// 3. Recover to OK
	rdb.HSet(ctx, "market:liquidationcapture:health:bybit", map[string]interface{}{
		"status":             "ok",
		"reason_codes":       "",
		"process_session_id": "s1",
	})

	monitor.checkExchange(ctx, "bybit")
	if monitor.lastAlerts["bybit"] != "ok" {
		t.Errorf("expected ok, got %s", monitor.lastAlerts["bybit"])
	}

	messages, err = rdb.XRange(ctx, StreamOutboxV1, "-", "+").Result()
	if len(messages) != 2 {
		t.Fatalf("expected 2 messages in outbox, got %d", len(messages))
	}
	if err := json.Unmarshal([]byte(messages[1].Values["data"].(string)), &env); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if env.Severity != "info" {
		t.Errorf("expected recovery severity info, got %s", env.Severity)
	}

	// 4. Degraded state
	rdb.HSet(ctx, "market:liquidationcapture:health:bybit", map[string]interface{}{
		"status":             "degraded",
		"reason_codes":       "reconnect_storm",
		"process_session_id": "s1",
	})
	monitor.checkExchange(ctx, "bybit")
	if monitor.lastAlerts["bybit"] != "warning" {
		t.Errorf("expected warning, got %s", monitor.lastAlerts["bybit"])
	}

	messages, err = rdb.XRange(ctx, StreamOutboxV1, "-", "+").Result()
	if len(messages) != 3 {
		t.Fatalf("expected 3 messages in outbox, got %d", len(messages))
	}
	if err := json.Unmarshal([]byte(messages[2].Values["data"].(string)), &env); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if env.Severity != "info" { // fallback for warning
		t.Errorf("expected fallback severity info, got %s", env.Severity)
	}
}

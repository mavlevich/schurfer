package main

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestHeartbeatBucketsDueMarksCatchUpIntervalsLate(t *testing.T) {
	next := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	now := time.Date(2026, 8, 25, 12, 2, 3, 0, time.UTC)
	due := heartbeatBucketsDue(next, now)
	if len(due) != 2 {
		t.Fatalf("due buckets = %+v, want two", due)
	}
	if !due[0].Late {
		t.Fatal("12:00 bucket must be incomplete after a >1 minute scheduler delay")
	}
	if due[1].Late {
		t.Fatal("12:01 bucket recorded three seconds after close is within tolerance")
	}
}

func TestHeartbeatBucketsDueDoesNotEmitCurrentOpenMinute(t *testing.T) {
	next := time.Date(2026, 8, 25, 12, 5, 0, 0, time.UTC)
	now := next.Add(59 * time.Second)
	if due := heartbeatBucketsDue(next, now); len(due) != 0 {
		t.Fatalf("open minute unexpectedly due: %+v", due)
	}
}

func TestRunHealthcheckRejectsFailedAndStaleHealth(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Setenv("LIQUIDATION_CAPTURE_EXCHANGE", "bybit")
	t.Setenv("DATABASE_URL", "postgresql://unused")
	t.Setenv("REDIS_ADDR", mr.Addr())
	key := "market:liquidationcapture:health:bybit"

	if err := rdb.HSet(context.Background(), key, map[string]any{
		"status": "failed", "reason_codes": "queue_drop_critical",
		"updated_at_ms": time.Now().UnixMilli(),
	}).Err(); err != nil {
		t.Fatal(err)
	}
	if err := runHealthcheck(); err == nil {
		t.Fatal("failed status passed healthcheck")
	}
	if err := rdb.HSet(context.Background(), key, map[string]any{
		"status": "mystery", "updated_at_ms": time.Now().UnixMilli(),
	}).Err(); err != nil {
		t.Fatal(err)
	}
	if err := runHealthcheck(); err == nil {
		t.Fatal("unknown status passed healthcheck")
	}

	if err := rdb.HSet(context.Background(), key, map[string]any{
		"status": "ok", "updated_at_ms": time.Now().Add(-31 * time.Second).UnixMilli(),
	}).Err(); err != nil {
		t.Fatal(err)
	}
	if err := runHealthcheck(); err == nil {
		t.Fatal("stale status passed healthcheck")
	}

	if err := rdb.HSet(context.Background(), key, map[string]any{
		"status": "ok", "updated_at_ms": time.Now().UnixMilli(),
	}).Err(); err != nil {
		t.Fatal(err)
	}
	if err := runHealthcheck(); err != nil {
		t.Fatalf("fresh ok health failed: %v", err)
	}
}

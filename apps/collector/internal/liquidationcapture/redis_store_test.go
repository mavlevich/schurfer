package liquidationcapture

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestRedisStorePersistsEvaluatedHealthFields(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	store, err := NewRedisStore(rdb)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	health := Health{
		Exchange:             "bybit",
		CoverageKind:         CoverageCompleteStream,
		ProcessSessionID:     "session-1",
		StartedAt:            now.Add(-time.Minute),
		UpdatedAt:            now,
		ConnectedConnections: 2,
		ExpectedConnections:  3,
		Evaluated: EvaluatedHealth{
			Status:            StatusDegraded,
			ReasonCodes:       "disconnected_streams",
			StatusChangedAtMs: now.Add(-30 * time.Second).UnixMilli(),
		},
	}
	if err := store.StoreHealth(context.Background(), health); err != nil {
		t.Fatal(err)
	}

	values, err := rdb.HGetAll(context.Background(), HealthKey("bybit")).Result()
	if err != nil {
		t.Fatal(err)
	}
	if values["connected_connections"] != "2" || values["expected_connections"] != "3" {
		t.Fatalf("connection counts = %+v", values)
	}
	if values["status_changed_at_ms"] != "1787745570000" {
		t.Fatalf("status_changed_at_ms = %q", values["status_changed_at_ms"])
	}
	if ttl := mr.TTL(HealthKey("bybit")); ttl != healthTTL {
		t.Fatalf("health TTL = %s, want %s", ttl, healthTTL)
	}
}

func TestRedisStorePreservesFatalIncidentOutsideMutableHealth(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	store, err := NewRedisStore(rdb)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	incident := Incident{
		Exchange: "binance", ProcessSessionID: "session-failed",
		OccurredAt: now, ReasonCodes: "queue_drop_critical",
	}
	if err := store.StoreIncident(context.Background(), incident); err != nil {
		t.Fatal(err)
	}

	key := IncidentKey("binance", "session-failed")
	values, err := rdb.HGetAll(context.Background(), key).Result()
	if err != nil {
		t.Fatal(err)
	}
	if values["reason_codes"] != "queue_drop_critical" {
		t.Fatalf("incident = %+v", values)
	}
	members, err := rdb.ZRange(context.Background(), IncidentIndexKey("binance"), 0, -1).Result()
	if err != nil {
		t.Fatal(err)
	}
	if len(members) != 1 || members[0] != "session-failed" {
		t.Fatalf("incident index = %v", members)
	}

	// A new process overwrites current health but cannot erase the prior
	// session's durable fatal incident.
	if err := store.StoreHealth(context.Background(), Health{
		Exchange: "binance", ProcessSessionID: "session-restarted", UpdatedAt: now.Add(time.Second),
		Evaluated: EvaluatedHealth{Status: StatusStarting},
	}); err != nil {
		t.Fatal(err)
	}
	if !mr.Exists(key) {
		t.Fatal("new process health erased the prior fatal incident")
	}
}

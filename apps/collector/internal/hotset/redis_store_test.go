package hotset

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestRedisStoreLoadsOnlyExplicitBybitActivations(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := NewRedisStore(client, 100, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	payload := map[string]any{
		"pumps": []any{
			map[string]any{
				"base":          "AKE",
				"pump_event_id": 885,
				"exchanges": []any{
					map[string]any{"exchange": "bybit", "market_id": "AKEUSDT"},
				},
			},
			map[string]any{
				"base":          "EUL",
				"pump_event_id": 886,
				"exchanges":     []any{},
			},
			map[string]any{"base": "", "pump_event_id": 0},
		},
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := client.Set(context.Background(), MeasurementKey, raw, time.Minute).Err(); err != nil {
		t.Fatal(err)
	}

	snapshot, err := store.Activations(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Status != "ok" || len(snapshot.Activations) != 1 {
		t.Fatalf("activations = %+v, want 1 explicit Bybit mapping", snapshot)
	}
	if snapshot.Candidates != 2 || snapshot.Unmapped != 1 {
		t.Fatalf("coverage = (%d, %d), want (2, 1)", snapshot.Candidates, snapshot.Unmapped)
	}
	if snapshot.Activations[0].Symbol != "AKEUSDT" {
		t.Fatalf("unexpected symbols: %+v", snapshot.Activations)
	}
}

func TestRedisStoreReportsMissingAndInvalidFeed(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := NewRedisStore(client, 100, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.Activations(context.Background())
	if err != nil || snapshot.Status != "missing" {
		t.Fatalf("missing feed = (%s, %v)", snapshot.Status, err)
	}
	if err := server.Set(MeasurementKey, "{"); err != nil {
		t.Fatal(err)
	}
	snapshot, err = store.Activations(context.Background())
	if err == nil || snapshot.Status != "invalid" {
		t.Fatalf("invalid feed = (%s, %v)", snapshot.Status, err)
	}
}

func TestRedisStoreRestoresAndExpiresActivationRegistry(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := NewRedisStore(client, 100, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Unix(2_000, 0).UTC()
	expiresAt := now.Add(time.Hour)
	registered := []Activation{{
		Symbol:      "AKEUSDT",
		Base:        "AKE",
		PumpEventID: 885,
		Reason:      "measurement_feed",
	}}
	if err := store.RefreshActivations(context.Background(), registered, expiresAt); err != nil {
		t.Fatal(err)
	}

	active, err := store.ActiveActivations(context.Background(), now.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if len(active) != 1 || active[0].Symbol != "AKEUSDT" ||
		!active[0].ExpiresAt.Equal(expiresAt) {
		t.Fatalf("restored activations = %+v", active)
	}

	active, err = store.ActiveActivations(context.Background(), expiresAt)
	if err != nil {
		t.Fatal(err)
	}
	if len(active) != 0 {
		t.Fatalf("expired activations = %+v, want none", active)
	}
	got, err := client.HLen(context.Background(), watchMetadataKey).Result()
	if err != nil {
		t.Fatal(err)
	}
	if got != 0 {
		t.Fatalf("expired metadata fields = %d, want 0", got)
	}
}

func TestRedisStoreRejectsIncompleteActivationRegistryEntry(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := NewRedisStore(client, 100, time.Hour)
	if err != nil {
		t.Fatal(err)
	}

	err = store.RefreshActivations(
		context.Background(),
		[]Activation{{Symbol: "AKEUSDT", PumpEventID: 885, Reason: "measurement_feed"}},
		time.Now().Add(time.Hour),
	)
	if err == nil {
		t.Fatal("incomplete activation unexpectedly accepted")
	}
	if server.Exists(WatchKey) {
		t.Fatal("invalid activation changed the registry")
	}
}

func TestRedisStorePersistsBoundedBarsAndHealth(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := NewRedisStore(client, 2, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	start := time.Unix(1000, 0).UTC()
	bars := make([]Bar, 0, 3)
	for i := range 3 {
		value := float64(i)
		bars = append(bars, Bar{
			SchemaVersion:  1,
			Exchange:       "bybit",
			Symbol:         "AKEUSDT",
			Base:           "AKE",
			PumpEventID:    885,
			Activation:     "measurement_feed",
			BucketStart:    start.Add(time.Duration(i*5) * time.Second),
			FirstEventAt:   start,
			LastEventAt:    start,
			LastReceivedAt: start,
			Open:           1,
			High:           2,
			Low:            1,
			Close:          2,
			Bid:            &value,
			EventCount:     2,
			MaxLag:         20 * time.Millisecond,
		})
	}
	if err := store.StoreBars(context.Background(), bars); err != nil {
		t.Fatal(err)
	}
	key := "market:hot:bars:bybit:AKEUSDT"
	rows, err := client.XRange(context.Background(), key, "-", "+").Result()
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) == 0 || len(rows) > 3 {
		t.Fatalf("stream rows = %d, want bounded non-empty stream", len(rows))
	}
	if server.TTL(key) <= 0 {
		t.Fatal("stream TTL was not set")
	}

	health := Health{
		UpdatedAt:             start,
		LastEventAt:           start,
		EventsTotal:           10,
		BarsPersistedTotal:    3,
		ObservedSymbols:       696,
		HotSymbols:            1,
		EventRate:             2.5,
		LastLag:               20 * time.Millisecond,
		MaxLag:                40 * time.Millisecond,
		WindowMaxLag:          30 * time.Millisecond,
		PumpFeedStatus:        "ok",
		MeasurementCandidates: 2,
		UnmappedCandidates:    1,
	}
	if err := store.StoreHealth(context.Background(), health); err != nil {
		t.Fatal(err)
	}
	if got := server.HGet(HealthKey, "events_total"); got != "10" {
		t.Fatalf("events_total = %q, want 10", got)
	}
	if got := server.HGet(HealthKey, "unmapped_candidates"); got != "1" {
		t.Fatalf("unmapped_candidates = %q, want 1", got)
	}
	if got := server.HGet(HealthKey, "observed_symbols"); got != "696" {
		t.Fatalf("observed_symbols = %q, want 696", got)
	}
	if got := server.HGet(HealthKey, "window_max_lag_ms"); got != "30" {
		t.Fatalf("window_max_lag_ms = %q, want 30", got)
	}
	if server.TTL(HealthKey) <= 0 {
		t.Fatal("health TTL was not set")
	}
}

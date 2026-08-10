package momentumcapture

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestNewRedisStoreRejectsNilClient(t *testing.T) {
	t.Parallel()
	if _, err := NewRedisStore(nil); err == nil {
		t.Fatal("expected an error for a nil redis client")
	}
}

func TestRedisStoreStoresHealthWithTTLAndSampledSymbolLists(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := NewRedisStore(client)
	if err != nil {
		t.Fatal(err)
	}

	missing := make([]string, 0, missingSymbolsSample+5)
	for i := 0; i < missingSymbolsSample+5; i++ {
		missing = append(missing, "SYM"+string(rune('A'+i))+"USDT")
	}
	now := time.Unix(2_000, 0).UTC()
	health := Health{
		Status:                   "ok",
		StartedAt:                time.Unix(1_000, 0).UTC(),
		UpdatedAt:                now,
		SubscribedSymbols:        735,
		ReadySymbols:             400,
		SymbolsMissingTicker:     missing,
		BarsCompletedTotal:       727,
		BarsPersistedTotal:       727,
		WriterQueueDepth:         3,
		PayloadHashMismatchTotal: 1,
	}

	if err := store.StoreHealth(context.Background(), health); err != nil {
		t.Fatal(err)
	}

	if ttl := server.TTL(HealthKey); ttl <= 0 || ttl > healthTTL {
		t.Fatalf("health key TTL = %v, want (0, %v]", ttl, healthTTL)
	}

	fields, err := client.HGetAll(context.Background(), HealthKey).Result()
	if err != nil {
		t.Fatal(err)
	}
	if fields["status"] != "ok" {
		t.Fatalf("status = %q, want ok", fields["status"])
	}
	if fields["subscribed_symbols"] != "735" || fields["ready_symbols"] != "400" {
		t.Fatalf("universe fields wrong: %+v", fields)
	}
	if fields["bars_completed_total"] != "727" || fields["bars_persisted_total"] != "727" {
		t.Fatalf("bar counters wrong: %+v", fields)
	}
	if fields["payload_hash_mismatch_total"] != "1" {
		t.Fatalf("payload_hash_mismatch_total = %q, want 1", fields["payload_hash_mismatch_total"])
	}
	if fields["symbols_missing_ticker_count"] != "25" {
		t.Fatalf("symbols_missing_ticker_count = %q, want the TRUE count (25), not the sampled length", fields["symbols_missing_ticker_count"])
	}
	sampleCount := len(splitNonEmpty(fields["symbols_missing_ticker_sample"]))
	if sampleCount != missingSymbolsSample {
		t.Fatalf("symbols_missing_ticker_sample has %d entries, want the capped %d", sampleCount, missingSymbolsSample)
	}
	if fields["updated_at_ms"] != "2000000" {
		t.Fatalf("updated_at_ms = %q, want 2000000", fields["updated_at_ms"])
	}
}

func TestSampleJoinCapsWithoutMutatingTheInputSlice(t *testing.T) {
	t.Parallel()
	items := []string{"A", "B", "C", "D"}
	got := sampleJoin(items, 2)
	if got != "A,B" {
		t.Fatalf("sampleJoin = %q, want A,B", got)
	}
	if len(items) != 4 {
		t.Fatal("sampleJoin must not mutate the caller's slice")
	}
}

func TestUnixMilliOrZero(t *testing.T) {
	t.Parallel()
	if got := unixMilliOrZero(time.Time{}); got != 0 {
		t.Fatalf("zero time = %d, want 0", got)
	}
	want := time.Unix(5, 0).UTC()
	if got := unixMilliOrZero(want); got != want.UnixMilli() {
		t.Fatalf("unixMilliOrZero = %d, want %d", got, want.UnixMilli())
	}
}

func splitNonEmpty(s string) []string {
	if s == "" {
		return nil
	}
	out := []string{}
	start := 0
	for i := 0; i <= len(s); i++ {
		if i == len(s) || s[i] == ',' {
			out = append(out, s[start:i])
			start = i + 1
		}
	}
	return out
}

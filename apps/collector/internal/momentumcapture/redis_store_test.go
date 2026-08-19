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
		Exchange:                 "bybit",
		Status:                   "ok",
		StartedAt:                time.Unix(1_000, 0).UTC(),
		UpdatedAt:                now,
		SubscribedSymbols:        735,
		ReadySymbols:             400,
		CatalogItemsTotal:        775,
		CryptoPerpetualsIncluded: 551,
		DatedFuturesExcluded:     40,
		StockPerpetualsExcluded:  180,
		SymbolsMissingTicker:     missing,
		BarsCompletedTotal:       727,
		BarsPersistedTotal:       727,
		WriterQueueDepth:         3,
		PayloadHashMismatchTotal: 1,
		TradeHandlerCount:        100,
		TradeHandlerP95Us:        250,
		TradeHandlerP99Us:        500,
		TradeHandlerMaxUs:        900,
		FlushCount:               20,
		FlushP99Us:               2_500,
	}

	if err := store.StoreHealth(context.Background(), health); err != nil {
		t.Fatal(err)
	}

	key := HealthKey("bybit")
	if ttl := server.TTL(key); ttl <= 0 || ttl > healthTTL {
		t.Fatalf("health key TTL = %v, want (0, %v]", ttl, healthTTL)
	}

	fields, err := client.HGetAll(context.Background(), key).Result()
	if err != nil {
		t.Fatal(err)
	}
	if fields["exchange"] != "bybit" {
		t.Fatalf("exchange = %q, want bybit", fields["exchange"])
	}
	if fields["status"] != "ok" {
		t.Fatalf("status = %q, want ok", fields["status"])
	}
	if fields["schema_version"] != "2" {
		t.Fatalf("schema_version = %q, want 2", fields["schema_version"])
	}
	if fields["subscribed_symbols"] != "735" || fields["ready_symbols"] != "400" {
		t.Fatalf("universe fields wrong: %+v", fields)
	}
	if fields["catalog_items_total"] != "775" ||
		fields["crypto_perpetuals_included"] != "551" ||
		fields["dated_futures_excluded"] != "40" ||
		fields["stock_perpetuals_excluded"] != "180" {
		t.Fatalf("catalog scope fields wrong: %+v", fields)
	}
	if fields["bars_completed_total"] != "727" || fields["bars_persisted_total"] != "727" {
		t.Fatalf("bar counters wrong: %+v", fields)
	}
	if fields["payload_hash_mismatch_total"] != "1" {
		t.Fatalf("payload_hash_mismatch_total = %q, want 1", fields["payload_hash_mismatch_total"])
	}
	if fields["trade_handler_count"] != "100" || fields["trade_handler_p99_us"] != "500" {
		t.Fatalf("trade handler latency fields wrong: %+v", fields)
	}
	if fields["flush_count"] != "20" || fields["flush_p99_us"] != "2500" {
		t.Fatalf("flush latency fields wrong: %+v", fields)
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

func TestStoreHealthRejectsUnsetExchange(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := NewRedisStore(client)
	if err != nil {
		t.Fatal(err)
	}

	// Regression: an unset Exchange used to silently publish to the one
	// shared HealthKey constant. Failing closed here, rather than falling
	// back to some default venue, is what makes it impossible for a second
	// venue's process to mask the first's health snapshot by omission.
	if err := store.StoreHealth(context.Background(), Health{Status: "ok"}); err == nil {
		t.Fatal("expected an error for a Health with no Exchange set")
	}
}

func TestStoreHealthKeysEachExchangeSeparately(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := NewRedisStore(client)
	if err != nil {
		t.Fatal(err)
	}

	if err := store.StoreHealth(context.Background(), Health{Exchange: "bybit", Status: "ok", SubscribedSymbols: 735}); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreHealth(context.Background(), Health{Exchange: "binance", Status: "ok", SubscribedSymbols: 210}); err != nil {
		t.Fatal(err)
	}

	// Regression: this is the actual "no shared/masking counters" case --
	// two venues publishing health at the same moment must never collide
	// on one Redis key, or the second StoreHealth call would silently
	// erase the first venue's snapshot.
	bybitFields, err := client.HGetAll(context.Background(), HealthKey("bybit")).Result()
	if err != nil {
		t.Fatal(err)
	}
	binanceFields, err := client.HGetAll(context.Background(), HealthKey("binance")).Result()
	if err != nil {
		t.Fatal(err)
	}
	if bybitFields["subscribed_symbols"] != "735" {
		t.Fatalf("bybit subscribed_symbols = %q, want 735 (unaffected by the binance write)", bybitFields["subscribed_symbols"])
	}
	if binanceFields["subscribed_symbols"] != "210" {
		t.Fatalf("binance subscribed_symbols = %q, want 210", binanceFields["subscribed_symbols"])
	}
}

func TestStoreHealthEncodesExclusionCountsAsDeterministicJSON(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := NewRedisStore(client)
	if err != nil {
		t.Fatal(err)
	}

	health := Health{
		Exchange: "binance",
		Status:   "ok",
		ExclusionCounts: map[string]int{
			"underlying_index":        2,
			"non_perpetual_contract":  14,
			"unknown_underlying_type": 0,
		},
	}
	if err := store.StoreHealth(context.Background(), health); err != nil {
		t.Fatal(err)
	}

	fields, err := client.HGetAll(context.Background(), HealthKey("binance")).Result()
	if err != nil {
		t.Fatal(err)
	}
	// Regression: map iteration order is randomized in Go, but
	// json.Marshal on a map always sorts keys -- this asserts the exact
	// serialized string, not just that it round-trips, so an accidental
	// switch to a non-deterministic encoding would fail this test.
	want := `{"non_perpetual_contract":14,"underlying_index":2,"unknown_underlying_type":0}`
	if fields["exclusion_counts_json"] != want {
		t.Fatalf("exclusion_counts_json = %q, want %q", fields["exclusion_counts_json"], want)
	}
}

func TestStoreHealthEncodesNilExclusionCountsAsEmptyObject(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := NewRedisStore(client)
	if err != nil {
		t.Fatal(err)
	}

	if err := store.StoreHealth(context.Background(), Health{Exchange: "bybit", Status: "ok"}); err != nil {
		t.Fatal(err)
	}

	fields, err := client.HGetAll(context.Background(), HealthKey("bybit")).Result()
	if err != nil {
		t.Fatal(err)
	}
	if fields["exclusion_counts_json"] != "{}" {
		t.Fatalf("exclusion_counts_json = %q, want {} for a venue with no such exclusions", fields["exclusion_counts_json"])
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

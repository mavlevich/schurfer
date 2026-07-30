package orderflow

import (
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/bybit"
)

func TestEngineCapturesUncensoredEventControlsAndFutureBuckets(t *testing.T) {
	t.Parallel()
	startedAt := time.Unix(1_700_000_000, 0).UTC()
	engine := mustEngine(t, Config{
		BucketSize:      time.Second,
		Prebuffer:       3 * time.Second,
		CaptureAfter:    10 * time.Second,
		Controls:        1,
		MaxSymbols:      10,
		MaxActiveEvents: 4,
		RecentTradeIDs:  16,
	}, startedAt)

	for second := range 4 {
		observeTrade(t, engine, trade("BTCUSDT", second, startedAt, 100+float64(second), 10))
		observeTrade(t, engine, trade("ETHUSDT", second, startedAt, 50+float64(second), 20))
	}

	activationAt := startedAt.Add(4 * time.Second)
	records, controls, err := engine.Activate(Activation{
		PumpEventID:     42,
		Base:            "BTC",
		Symbol:          "BTCUSDT",
		FirstObservedAt: activationAt,
	})
	if err != nil {
		t.Fatalf("activate: %v", err)
	}
	if len(controls) != 1 || controls[0] != "ETHUSDT" {
		t.Fatalf("unexpected controls: %#v", controls)
	}
	// Only complete buckets wholly inside the prebuffer are eligible. The
	// bucket active at activation time stays excluded to avoid look-ahead.
	if len(records) != 4 {
		t.Fatalf("expected 4 complete prebuffer records, got %d", len(records))
	}
	roles := map[string]int{}
	for _, record := range records {
		roles[record.Role]++
		if record.PumpEventID != 42 || record.EventSymbol != "BTCUSDT" {
			t.Fatalf("unexpected record identity: %#v", record)
		}
		if !time.UnixMilli(record.Bucket.BucketStartMS).Before(activationAt) {
			t.Fatalf("prebuffer leaked a post-activation bucket: %#v", record.Bucket)
		}
	}
	if roles["event"] != 2 || roles["control"] != 2 {
		t.Fatalf("unexpected role counts: %#v", roles)
	}

	observeTrade(t, engine, trade("BTCUSDT", 5, startedAt, 105, 10))
	future := observeTrade(t, engine, trade("BTCUSDT", 6, startedAt, 106, 10))
	if len(future) != 1 {
		t.Fatalf("expected one future event record, got %d", len(future))
	}
	if got := time.UnixMilli(future[0].Bucket.BucketStartMS); !got.Equal(startedAt.Add(5 * time.Second)) {
		t.Fatalf("future bucket mismatch: %s", got)
	}
}

func TestEngineRejectsLeftCensoredActivation(t *testing.T) {
	t.Parallel()
	startedAt := time.Unix(1_700_000_000, 0).UTC()
	engine := mustEngine(t, Config{
		BucketSize:      time.Second,
		Prebuffer:       30 * time.Minute,
		CaptureAfter:    time.Hour,
		Controls:        0,
		MaxSymbols:      10,
		MaxActiveEvents: 4,
		RecentTradeIDs:  16,
	}, startedAt)
	observeTrade(t, engine, trade("BTCUSDT", 0, startedAt, 100, 1))
	observeTrade(t, engine, trade("BTCUSDT", 1, startedAt, 101, 1))

	_, _, err := engine.Activate(Activation{
		PumpEventID:     1,
		Base:            "BTC",
		Symbol:          "BTCUSDT",
		FirstObservedAt: startedAt.Add(time.Minute),
	})
	if !errors.Is(err, ErrPrebufferNotReady) {
		t.Fatalf("expected prebuffer error, got %v", err)
	}
}

func TestEngineRejectsDuplicateAndOutOfOrderTrades(t *testing.T) {
	t.Parallel()
	startedAt := time.Unix(1_700_000_000, 0).UTC()
	engine := mustEngine(t, Config{
		BucketSize:      time.Second,
		Prebuffer:       time.Second,
		CaptureAfter:    time.Minute,
		Controls:        0,
		MaxSymbols:      10,
		MaxActiveEvents: 4,
		RecentTradeIDs:  16,
	}, startedAt)
	first := trade("BTCUSDT", 1, startedAt, 100, 1)
	observeTrade(t, engine, first)
	if _, err := engine.Observe(first); !errors.Is(err, ErrDuplicateTrade) {
		t.Fatalf("expected duplicate error, got %v", err)
	}
	older := trade("BTCUSDT", 0, startedAt, 99, 1)
	if _, err := engine.Observe(older); !errors.Is(err, ErrOutOfOrderTrade) {
		t.Fatalf("expected out-of-order error, got %v", err)
	}
	if _, err := engine.Observe(older); !errors.Is(err, ErrOutOfOrderTrade) {
		t.Fatalf("rejected trade id was incorrectly remembered: %v", err)
	}
}

func TestEngineAcceptsOutOfOrderEventsWithinOneBucket(t *testing.T) {
	t.Parallel()
	startedAt := time.Unix(1_700_000_000, 0).UTC()
	engine := mustEngine(t, Config{
		BucketSize:      time.Second,
		Prebuffer:       time.Second,
		CaptureAfter:    time.Minute,
		Controls:        0,
		MaxSymbols:      10,
		MaxActiveEvents: 4,
		RecentTradeIDs:  16,
	}, startedAt)
	later := trade("BTCUSDT", 0, startedAt, 101, 1)
	later.EventAt = later.EventAt.Add(800 * time.Millisecond)
	later.TradeID = "later"
	observeTrade(t, engine, later)
	earlier := trade("BTCUSDT", 0, startedAt, 100, 1)
	earlier.EventAt = earlier.EventAt.Add(200 * time.Millisecond)
	earlier.TradeID = "earlier"
	observeTrade(t, engine, earlier)
	records := observeTrade(t, engine, trade("BTCUSDT", 1, startedAt, 102, 1))
	if len(records) != 0 {
		t.Fatalf("unexpected uncaptured records: %d", len(records))
	}
	bucket := engine.states["BTCUSDT"].prebuffer[0]
	if bucket.Open != 100 || bucket.Close != 101 {
		t.Fatalf("event-time OHLC mismatch: open=%f close=%f", bucket.Open, bucket.Close)
	}
}

func TestEngineDoesNotSelectAnotherActivePumpAsControl(t *testing.T) {
	t.Parallel()
	startedAt := time.Unix(1_700_000_000, 0).UTC()
	engine := mustEngine(t, Config{
		BucketSize:      time.Second,
		Prebuffer:       3 * time.Second,
		CaptureAfter:    time.Minute,
		Controls:        1,
		MaxSymbols:      10,
		MaxActiveEvents: 4,
		RecentTradeIDs:  16,
	}, startedAt)
	for second := range 4 {
		observeTrade(t, engine, trade("BTCUSDT", second, startedAt, 100, 10))
		observeTrade(t, engine, trade("PUMPUSDT", second, startedAt, 100, 10))
		observeTrade(t, engine, trade("ETHUSDT", second, startedAt, 100, 9))
	}
	_, controls, err := engine.Activate(Activation{
		PumpEventID:      42,
		Base:             "BTC",
		Symbol:           "BTCUSDT",
		FirstObservedAt:  startedAt.Add(4 * time.Second),
		ExcludedControls: []string{"PUMPUSDT"},
	})
	if err != nil {
		t.Fatalf("activate: %v", err)
	}
	if len(controls) != 1 || controls[0] != "ETHUSDT" {
		t.Fatalf("active pump leaked into controls: %#v", controls)
	}
}

func mustEngine(t *testing.T, config Config, startedAt time.Time) *Engine {
	t.Helper()
	engine, err := New(config, startedAt)
	if err != nil {
		t.Fatalf("new engine: %v", err)
	}
	return engine
}

func trade(
	symbol string,
	second int,
	startedAt time.Time,
	price float64,
	size float64,
) bybit.PublicTrade {
	eventAt := startedAt.Add(time.Duration(second) * time.Second)
	return bybit.PublicTrade{
		Symbol:     symbol,
		TradeID:    fmt.Sprintf("%s-%d", symbol, second),
		Side:       "buy",
		EventAt:    eventAt,
		ReceivedAt: eventAt.Add(10 * time.Millisecond),
		Price:      price,
		Size:       size,
	}
}

func observeTrade(t *testing.T, engine *Engine, trade bybit.PublicTrade) []Record {
	t.Helper()
	records, err := engine.Observe(trade)
	if err != nil {
		t.Fatalf("observe: %v", err)
	}
	return records
}

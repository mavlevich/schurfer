package hotset

import (
	"errors"
	"testing"
	"time"
)

func TestEngineFlushesPrebufferAndContinuesHotBars(t *testing.T) {
	t.Parallel()
	engine := newTestEngine(t, Config{
		BucketSize: 5 * time.Second,
		Prebuffer:  15 * time.Second,
		HotTTL:     time.Hour,
		MaxSymbols: 2,
	})
	start := time.Unix(100, 0).UTC()
	observe(t, engine, tick("AKEUSDT", start, 1.00, 100, 1000))
	observe(t, engine, tick("AKEUSDT", start.Add(2*time.Second), 1.20, 120, 1300))
	observe(t, engine, tick("AKEUSDT", start.Add(5*time.Second), 1.10, 130, 1500))
	observe(t, engine, tick("AKEUSDT", start.Add(10*time.Second), 1.30, 160, 2000))

	prebuffer, ok := activate(engine, "AKEUSDT", "AKE", 885, start.Add(11*time.Second))
	if !ok {
		t.Fatal("activation rejected")
	}
	if len(prebuffer) != 2 {
		t.Fatalf("prebuffer bars = %d, want 2", len(prebuffer))
	}
	first := prebuffer[0]
	if first.Open != 1.00 || first.High != 1.20 || first.Low != 1.00 || first.Close != 1.20 {
		t.Fatalf("unexpected OHLC: %+v", first)
	}
	if first.EventCount != 2 {
		t.Fatalf("event count = %d, want 2", first.EventCount)
	}
	if first.PumpEventID != 885 || first.Base != "AKE" || first.Activation != "measurement_feed" {
		t.Fatalf("missing activation context: %+v", first)
	}
	requireFloat(t, first.VolumeDelta24h, 20)
	requireFloat(t, first.TurnoverDelta24h, 300)

	bars := observe(t, engine, tick("AKEUSDT", start.Add(15*time.Second), 1.25, 170, 2100))
	if len(bars) != 1 || !bars[0].BucketStart.Equal(start.Add(10*time.Second)) {
		t.Fatalf("unexpected hot bars: %+v", bars)
	}
	requireFloat(t, bars[0].VolumeDelta24h, 30)
	requireFloat(t, bars[0].TurnoverDelta24h, 500)
}

func TestEngineBoundsPrebufferAndHotCapacity(t *testing.T) {
	t.Parallel()
	engine := newTestEngine(t, Config{
		BucketSize: 5 * time.Second,
		Prebuffer:  10 * time.Second,
		HotTTL:     time.Minute,
		MaxSymbols: 1,
	})
	start := time.Unix(200, 0).UTC()
	for i := range 4 {
		observe(t, engine, tick(
			"ONEUSDT",
			start.Add(time.Duration(i*5)*time.Second),
			float64(i+1),
			float64(i),
			float64(i),
		))
	}
	bars, ok := activate(engine, "ONEUSDT", "ONE", 1, start.Add(16*time.Second))
	if !ok {
		t.Fatal("first activation rejected")
	}
	if len(bars) != 2 {
		t.Fatalf("bounded prebuffer bars = %d, want 2", len(bars))
	}
	if _, ok := activate(engine, "TWOUSDT", "TWO", 2, start.Add(16*time.Second)); ok {
		t.Fatal("activation above capacity unexpectedly accepted")
	}
	if engine.HotCount(start.Add(2*time.Minute)) != 0 {
		t.Fatal("expired activation was not removed")
	}
	observe(t, engine, tick("TWOUSDT", start.Add(2*time.Minute), 1, 1, 1))
	if _, ok := activate(engine, "TWOUSDT", "TWO", 2, start.Add(2*time.Minute)); !ok {
		t.Fatal("capacity was not released after expiry")
	}
}

func TestEngineRejectsUnknownSymbolWithoutConsumingCapacity(t *testing.T) {
	t.Parallel()
	engine := newTestEngine(t, Config{
		BucketSize: 5 * time.Second,
		Prebuffer:  10 * time.Second,
		HotTTL:     time.Minute,
		MaxSymbols: 1,
	})
	now := time.Unix(250, 0).UTC()
	if _, ok := activate(engine, "MISSINGUSDT", "MISSING", 1, now); ok {
		t.Fatal("unknown symbol unexpectedly activated")
	}
	observe(t, engine, tick("AKEUSDT", now, 1, 1, 1))
	if _, ok := activate(engine, "AKEUSDT", "AKE", 2, now); !ok {
		t.Fatal("unknown symbol consumed hot-set capacity")
	}
}

func TestEngineRefreshDoesNotReplayPrebuffer(t *testing.T) {
	t.Parallel()
	engine := newTestEngine(t, Config{
		BucketSize: 5 * time.Second,
		Prebuffer:  10 * time.Second,
		HotTTL:     time.Minute,
		MaxSymbols: 1,
	})
	start := time.Unix(300, 0).UTC()
	observe(t, engine, tick("AKEUSDT", start, 1, 1, 1))
	observe(t, engine, tick("AKEUSDT", start.Add(5*time.Second), 2, 2, 2))
	first, ok := activate(engine, "AKEUSDT", "AKE", 3, start.Add(6*time.Second))
	if !ok || len(first) != 1 {
		t.Fatalf("first activation = (%d, %v), want (1, true)", len(first), ok)
	}
	second, ok := activate(engine, "AKEUSDT", "AKE", 3, start.Add(10*time.Second))
	if !ok || len(second) != 0 {
		t.Fatalf("refresh activation = (%d, %v), want (0, true)", len(second), ok)
	}
}

func TestEngineRejectsInvalidAndOutOfOrderTicks(t *testing.T) {
	t.Parallel()
	engine := newTestEngine(t, Config{
		BucketSize: 5 * time.Second,
		Prebuffer:  10 * time.Second,
		HotTTL:     time.Minute,
		MaxSymbols: 1,
	})
	start := time.Unix(400, 0).UTC()
	_, err := engine.Observe(Tick{Symbol: "AKEUSDT", EventAt: start, ReceivedAt: start, LastPrice: 0})
	if !errors.Is(err, ErrInvalidTick) {
		t.Fatalf("invalid error = %v", err)
	}
	observe(t, engine, tick("AKEUSDT", start.Add(10*time.Second), 1, 1, 1))
	_, err = engine.Observe(tick("AKEUSDT", start, 1, 1, 1))
	if !errors.Is(err, ErrOutOfOrderTick) {
		t.Fatalf("out-of-order error = %v", err)
	}
}

func TestCounterResetProducesUnavailableDelta(t *testing.T) {
	t.Parallel()
	engine := newTestEngine(t, Config{
		BucketSize: 5 * time.Second,
		Prebuffer:  10 * time.Second,
		HotTTL:     time.Minute,
		MaxSymbols: 1,
	})
	start := time.Unix(500, 0).UTC()
	observe(t, engine, tick("AKEUSDT", start, 1, 100, 1000))
	observe(t, engine, tick("AKEUSDT", start.Add(time.Second), 1.1, 90, 900))
	observe(t, engine, tick("AKEUSDT", start.Add(5*time.Second), 1.2, 95, 950))
	bars, ok := activate(engine, "AKEUSDT", "AKE", 5, start.Add(6*time.Second))
	if !ok || len(bars) != 1 {
		t.Fatalf("activation = (%d, %v), want (1, true)", len(bars), ok)
	}
	if bars[0].VolumeDelta24h != nil || bars[0].TurnoverDelta24h != nil {
		t.Fatalf("reset counters produced deltas: %+v", bars[0])
	}
}

func newTestEngine(t *testing.T, cfg Config) *Engine {
	t.Helper()
	engine, err := New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return engine
}

func tick(symbol string, at time.Time, price, volume, turnover float64) Tick {
	bid := price - 0.01
	ask := price + 0.01
	return Tick{
		Symbol:      symbol,
		EventAt:     at,
		ReceivedAt:  at.Add(20 * time.Millisecond),
		LastPrice:   price,
		Bid:         &bid,
		Ask:         &ask,
		Volume24h:   &volume,
		Turnover24h: &turnover,
	}
}

func observe(t *testing.T, engine *Engine, tick Tick) []Bar {
	t.Helper()
	bars, err := engine.Observe(tick)
	if err != nil {
		t.Fatal(err)
	}
	return bars
}

func activate(engine *Engine, symbol, base string, eventID int64, now time.Time) ([]Bar, bool) {
	return engine.Activate(Activation{
		Symbol:      symbol,
		Base:        base,
		PumpEventID: eventID,
		Reason:      "measurement_feed",
		ExpiresAt:   now.Add(time.Hour),
	}, now)
}

func requireFloat(t *testing.T, value *float64, want float64) {
	t.Helper()
	if value == nil || *value != want {
		t.Fatalf("float = %v, want %v", value, want)
	}
}

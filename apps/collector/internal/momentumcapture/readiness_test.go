package momentumcapture

import (
	"testing"
	"time"
)

func testUniverse() Universe {
	return NewUniverse([]string{"BTCUSDT", "ETHUSDT", "AKEUSDT"}, time.Unix(0, 0))
}

func TestReadinessTrackerStartsWithNothingReady(t *testing.T) {
	t.Parallel()
	r := NewReadinessTracker(testUniverse())
	for _, symbol := range []string{"BTCUSDT", "ETHUSDT", "AKEUSDT"} {
		if r.Ready(symbol) {
			t.Fatalf("%s must not be ready before any observation", symbol)
		}
	}
	if len(r.MissingTicker()) != 3 || len(r.MissingTrades()) != 3 {
		t.Fatalf("expected all 3 symbols missing both, got ticker=%v trades=%v", r.MissingTicker(), r.MissingTrades())
	}
}

func TestReadinessTrackerRequiresBothTickerAndTrade(t *testing.T) {
	t.Parallel()
	r := NewReadinessTracker(testUniverse())
	r.ObserveTicker("BTCUSDT")
	if r.Ready("BTCUSDT") {
		t.Fatal("ticker alone must not make a symbol ready")
	}
	r.ObserveTrade("BTCUSDT")
	if !r.Ready("BTCUSDT") {
		t.Fatal("BTCUSDT should be ready once both ticker and trade have been observed")
	}
	if r.Ready("ETHUSDT") {
		t.Fatal("observing BTCUSDT must not affect ETHUSDT's readiness")
	}
}

func TestReadinessTrackerMissingListsShrinkAsObservationsArrive(t *testing.T) {
	t.Parallel()
	r := NewReadinessTracker(testUniverse())
	r.ObserveTicker("BTCUSDT")
	r.ObserveTicker("ETHUSDT")
	missing := r.MissingTicker()
	if len(missing) != 1 || missing[0] != "AKEUSDT" {
		t.Fatalf("missing ticker = %v, want [AKEUSDT]", missing)
	}
	if len(r.MissingTrades()) != 3 {
		t.Fatalf("no trades observed yet, expected all 3 missing, got %v", r.MissingTrades())
	}
}

func TestReadinessTrackerIgnoresSymbolsOutsideTheFrozenUniverse(t *testing.T) {
	t.Parallel()
	r := NewReadinessTracker(testUniverse())
	r.ObserveTicker("SOMEOTHERUSDT")
	r.ObserveTrade("SOMEOTHERUSDT")
	if r.Ready("SOMEOTHERUSDT") {
		t.Fatal("a symbol outside the frozen universe must never be ready")
	}
	if len(r.MissingTicker()) != 3 || len(r.MissingTrades()) != 3 {
		t.Fatal("an out-of-universe observation must not affect the frozen universe's own missing counts")
	}
}

package momentumcapture

import (
	"testing"
	"time"
)

func TestBuildUniverseHealthReflectsDriftAndReadiness(t *testing.T) {
	t.Parallel()
	capturedAt := time.Unix(0, 0)
	universe := NewUniverse([]string{"BTCUSDT", "ETHUSDT", "AKEUSDT"}, capturedAt)

	readiness := NewReadinessTracker(universe)
	readiness.ObserveTicker("BTCUSDT")
	readiness.ObserveTrade("BTCUSDT")
	readiness.ObserveTicker("ETHUSDT")
	// AKEUSDT: neither observed yet.

	now := capturedAt.Add(10 * time.Minute)
	drift := universe.CheckDrift([]string{"BTCUSDT", "ETHUSDT", "AKEUSDT", "NEWLISTINGUSDT"}, now)

	health := BuildUniverseHealth(universe, drift, readiness, now)

	if health.UniverseSnapshotAt != capturedAt {
		t.Fatalf("universe snapshot at = %v, want %v", health.UniverseSnapshotAt, capturedAt)
	}
	if health.UniverseAgeSeconds != 600 {
		t.Fatalf("universe age seconds = %v, want 600", health.UniverseAgeSeconds)
	}
	if health.SubscribedSymbols != 3 {
		t.Fatalf("subscribed symbols = %d, want 3", health.SubscribedSymbols)
	}
	if health.CurrentExchangeSymbols != 4 {
		t.Fatalf("current exchange symbols = %d, want 4", health.CurrentExchangeSymbols)
	}
	if len(health.AddedSinceStart) != 1 || health.AddedSinceStart[0] != "NEWLISTINGUSDT" {
		t.Fatalf("added since start = %v, want [NEWLISTINGUSDT]", health.AddedSinceStart)
	}
	if !health.UniverseStale {
		t.Fatal("expected UniverseStale=true given a new listing appeared")
	}
	if health.ReadySymbols != 1 {
		t.Fatalf("ready symbols = %d, want 1 (only BTCUSDT has both)", health.ReadySymbols)
	}
	if len(health.SymbolsMissingTicker) != 1 || health.SymbolsMissingTicker[0] != "AKEUSDT" {
		t.Fatalf("symbols missing ticker = %v, want [AKEUSDT]", health.SymbolsMissingTicker)
	}
	if len(health.SymbolsMissingTrades) != 2 {
		t.Fatalf("symbols missing trades = %v, want 2 entries (ETHUSDT, AKEUSDT)", health.SymbolsMissingTrades)
	}
}

func TestBuildUniverseHealthWithNoDriftIsNotStale(t *testing.T) {
	t.Parallel()
	capturedAt := time.Unix(0, 0)
	universe := NewUniverse([]string{"BTCUSDT"}, capturedAt)
	readiness := NewReadinessTracker(universe)
	readiness.ObserveTicker("BTCUSDT")
	readiness.ObserveTrade("BTCUSDT")

	now := capturedAt.Add(time.Minute)
	drift := universe.CheckDrift([]string{"BTCUSDT"}, now)
	health := BuildUniverseHealth(universe, drift, readiness, now)

	if health.UniverseStale {
		t.Fatal("expected UniverseStale=false when the live catalog matches exactly")
	}
	if health.ReadySymbols != 1 {
		t.Fatalf("ready symbols = %d, want 1", health.ReadySymbols)
	}
	if len(health.SymbolsMissingTicker) != 0 || len(health.SymbolsMissingTrades) != 0 {
		t.Fatal("expected nothing missing once both feeds observed the only symbol")
	}
}

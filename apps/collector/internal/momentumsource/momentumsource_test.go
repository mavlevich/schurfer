package momentumsource

import (
	"strings"
	"testing"
)

func TestUniverseSnapshotValidateAcceptsExactAccounting(t *testing.T) {
	snapshot := UniverseSnapshot{
		Exchange:          "bybit",
		MarketType:        "linear_usdt_perpetual",
		IncludedSymbols:   []string{"BTCUSDT", "ETHUSDT"},
		TotalCatalogItems: 5,
		ExclusionCounts: map[string]int{
			"stock_perpetual": 2,
			"dated_future":    1,
		},
	}
	if err := snapshot.Validate(); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
}

func TestUniverseSnapshotValidateRejectsUnclassifiedRemainder(t *testing.T) {
	// Regression for the exact bug class the Bybit universe remediation
	// already fixed once in production (docs/research/momentum-venue-
	// capability-matrix-v1.md): a catalog item that is neither included nor
	// counted under a named exclusion reason must fail loud, not vanish.
	snapshot := UniverseSnapshot{
		Exchange:          "bybit",
		MarketType:        "linear_usdt_perpetual",
		IncludedSymbols:   []string{"BTCUSDT"},
		TotalCatalogItems: 5,
		ExclusionCounts:   map[string]int{"stock_perpetual": 2},
	}
	err := snapshot.Validate()
	if err == nil || !strings.Contains(err.Error(), "classification mismatch") {
		t.Fatalf("Validate() error = %v, want classification mismatch", err)
	}
}

func TestUniverseSnapshotValidateRejectsDuplicateIncludedSymbol(t *testing.T) {
	snapshot := UniverseSnapshot{
		Exchange:          "bybit",
		MarketType:        "linear_usdt_perpetual",
		IncludedSymbols:   []string{"BTCUSDT", "BTCUSDT"},
		TotalCatalogItems: 2,
	}
	err := snapshot.Validate()
	if err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("Validate() error = %v, want duplicate-symbol failure", err)
	}
}

func TestUniverseSnapshotValidateRejectsMissingVenueIdentity(t *testing.T) {
	snapshot := UniverseSnapshot{TotalCatalogItems: 0}
	err := snapshot.Validate()
	if err == nil || !strings.Contains(err.Error(), "exchange and market type") {
		t.Fatalf("Validate() error = %v, want exchange/market-type failure", err)
	}
}

func TestUniverseSnapshotValidateRejectsEmptySymbol(t *testing.T) {
	snapshot := UniverseSnapshot{
		Exchange:          "bybit",
		MarketType:        "linear_usdt_perpetual",
		IncludedSymbols:   []string{""},
		TotalCatalogItems: 1,
	}
	err := snapshot.Validate()
	if err == nil || !strings.Contains(err.Error(), "must not be empty") {
		t.Fatalf("Validate() error = %v, want empty-symbol failure", err)
	}
}

// Package momentumsource holds the venue-agnostic universe contract of the
// momentum-capture line: UniverseSnapshot, its fail-closed accounting check,
// and UniverseSource. Live trades and tickers go through streamrt's venue
// codecs instead. A missing capability is never zero, neutral, or silently
// substituted from another venue (see apps/collector/internal/momentumvenue
// for the capability matrix).
package momentumsource

import (
	"context"
	"fmt"
)

// UniverseSnapshot is one point-in-time classification of a venue's
// instrument catalog, restricted to whatever this capture line's own scope
// requires (e.g. Bybit's crypto-linear-perpetual filter). ExclusionCounts
// is a reason -> count map, deliberately not a single number: dropping the
// per-reason breakdown was the original Bybit universe bug this project
// already fixed once (see docs/research/momentum-venue-capability-matrix-
// v1.md's "Bybit universe remediation" section) -- every future venue must
// keep the same visibility, not silently regress to a single opaque count.
type UniverseSnapshot struct {
	Exchange          string
	MarketType        string
	IncludedSymbols   []string
	TotalCatalogItems int
	ExclusionCounts   map[string]int
}

// Validate enforces the same fail-closed accounting bybit.validateCatalog
// already checks privately: every catalog item is accounted for exactly
// once, either included or excluded under a named reason. An unclassified
// remainder is a bug in the adapter's own classification, not a acceptable
// gap -- see momentumvenue's "missing capability is never... neutral"
// principle applied to universe accounting specifically.
func (s UniverseSnapshot) Validate() error {
	if s.Exchange == "" || s.MarketType == "" {
		return fmt.Errorf("universe snapshot: exchange and market type are required")
	}
	classified := len(s.IncludedSymbols)
	for _, count := range s.ExclusionCounts {
		classified += count
	}
	if classified != s.TotalCatalogItems {
		return fmt.Errorf(
			"universe snapshot classification mismatch: total=%d classified=%d",
			s.TotalCatalogItems, classified,
		)
	}
	seen := make(map[string]struct{}, len(s.IncludedSymbols))
	for _, symbol := range s.IncludedSymbols {
		if symbol == "" {
			return fmt.Errorf("universe snapshot: included symbol must not be empty")
		}
		if _, exists := seen[symbol]; exists {
			return fmt.Errorf("universe snapshot: duplicate included symbol %q", symbol)
		}
		seen[symbol] = struct{}{}
	}
	return nil
}

type UniverseSource interface {
	FetchUniverse(ctx context.Context) (UniverseSnapshot, error)
}

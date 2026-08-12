// Package momentumcapture holds the capture-service-specific logic that
// sits between the raw Bybit decoders (package bybit) and the pure
// aggregation engine (package momentum): universe freezing and drift
// detection, per-symbol readiness tracking, and (in later steps of this
// PR) the bounded event loop and health snapshot that tie everything
// together. Kept as ordinary, network-free Go types wherever possible, on
// the same principle as package momentum: logic that can be unit tested
// without a WebSocket, NATS, or a database should be.
package momentumcapture

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strings"
	"time"
)

// Universe is an immutable snapshot of the symbol set momentum-capture
// commits to for one process lifetime.
//
// This is a deliberate v1 boundary, not an oversight: a genuinely dynamic
// universe (adding a newly-listed symbol's WebSocket subscription mid-flight,
// with its own gap marking and left-censoring) is real, separate scope,
// planned as its own follow-up PR (feat/momentum-dynamic-universe-v1) after
// this PR's 48-72h canary passes. Until then, new listings are only picked
// up on the next process restart. That limitation must stay visible, never
// silently implied to be "continuous full-universe capture": see
// DriftReport and the health snapshot fields built from it.
type Universe struct {
	Symbols    []string
	Hash       string
	CapturedAt time.Time
}

// NewUniverse freezes symbols (deduplicated, sorted) as of capturedAt.
// Sorting before hashing makes Hash independent of whatever order the
// exchange happened to return symbols in.
func NewUniverse(symbols []string, capturedAt time.Time) Universe {
	unique := dedupeSorted(symbols)
	return Universe{
		Symbols:    unique,
		Hash:       hashSymbols(unique),
		CapturedAt: capturedAt,
	}
}

// Count returns the number of distinct symbols in the frozen universe.
func (u Universe) Count() int { return len(u.Symbols) }

// Contains reports whether symbol is part of the frozen universe. Symbols
// is sorted by construction (see NewUniverse), so this is a binary search,
// not a linear scan. Callers that receive symbols from a source outside
// their own frozen subscription (e.g. a NATS ticker wildcard subject fed by
// a different process's independently frozen universe) must check this
// before folding an observation into the engine or persisting it: an
// unchecked symbol would be written with this process's universe_version
// despite never having been part of what that version actually covers.
func (u Universe) Contains(symbol string) bool {
	i := sort.SearchStrings(u.Symbols, symbol)
	return i < len(u.Symbols) && u.Symbols[i] == symbol
}

// DriftReport compares the frozen Universe against a freshly, independently
// fetched live symbol list (for example, the strict crypto-perpetual list from
// a periodic read-only FetchSymbolCatalog call). It never mutates anything and
// never triggers a resubscribe:
// detecting drift and acting on it are deliberately kept separate for v1
// (see the Universe doc comment).
type DriftReport struct {
	FrozenHash        string
	LiveHash          string
	FrozenCount       int
	LiveCount         int
	AddedSinceStart   []string // in the live catalog, not in the frozen universe
	RemovedSinceStart []string // in the frozen universe, not in the live catalog
	Stale             bool     // true iff FrozenHash != LiveHash
	CheckedAt         time.Time
}

// CheckDrift reports how liveSymbols (freshly, independently fetched)
// differs from the frozen universe, as of checkedAt. Pure and read-only:
// calling it never changes u or any subscription.
func (u Universe) CheckDrift(liveSymbols []string, checkedAt time.Time) DriftReport {
	live := dedupeSorted(liveSymbols)
	liveSet := toSet(live)
	frozenSet := toSet(u.Symbols)

	var added, removed []string
	for _, symbol := range live {
		if _, ok := frozenSet[symbol]; !ok {
			added = append(added, symbol)
		}
	}
	for _, symbol := range u.Symbols {
		if _, ok := liveSet[symbol]; !ok {
			removed = append(removed, symbol)
		}
	}

	liveHash := hashSymbols(live)
	return DriftReport{
		FrozenHash:        u.Hash,
		LiveHash:          liveHash,
		FrozenCount:       len(u.Symbols),
		LiveCount:         len(live),
		AddedSinceStart:   added,
		RemovedSinceStart: removed,
		Stale:             liveHash != u.Hash,
		CheckedAt:         checkedAt,
	}
}

func dedupeSorted(symbols []string) []string {
	seen := make(map[string]struct{}, len(symbols))
	out := make([]string, 0, len(symbols))
	for _, symbol := range symbols {
		if _, ok := seen[symbol]; ok {
			continue
		}
		seen[symbol] = struct{}{}
		out = append(out, symbol)
	}
	sort.Strings(out)
	return out
}

func toSet(symbols []string) map[string]struct{} {
	set := make(map[string]struct{}, len(symbols))
	for _, symbol := range symbols {
		set[symbol] = struct{}{}
	}
	return set
}

func hashSymbols(sortedUnique []string) string {
	sum := sha256.Sum256([]byte(strings.Join(sortedUnique, "\n")))
	return hex.EncodeToString(sum[:])
}

package momentumcapture

import (
	"testing"
	"time"
)

func TestNewUniverseDedupesAndSorts(t *testing.T) {
	t.Parallel()
	u := NewUniverse([]string{"ETHUSDT", "BTCUSDT", "ETHUSDT", "AKEUSDT"}, time.Unix(0, 0))
	want := []string{"AKEUSDT", "BTCUSDT", "ETHUSDT"}
	if len(u.Symbols) != len(want) {
		t.Fatalf("symbols = %v, want %v", u.Symbols, want)
	}
	for i := range want {
		if u.Symbols[i] != want[i] {
			t.Fatalf("symbols = %v, want %v", u.Symbols, want)
		}
	}
	if u.Count() != 3 {
		t.Fatalf("count = %d, want 3", u.Count())
	}
}

func TestNewUniverseHashIsOrderIndependent(t *testing.T) {
	t.Parallel()
	a := NewUniverse([]string{"BTCUSDT", "ETHUSDT"}, time.Unix(0, 0))
	b := NewUniverse([]string{"ETHUSDT", "BTCUSDT"}, time.Unix(100, 0))
	if a.Hash != b.Hash {
		t.Fatalf("hash depends on input order or capture time: %q vs %q", a.Hash, b.Hash)
	}
}

func TestNewUniverseHashChangesWithMembership(t *testing.T) {
	t.Parallel()
	a := NewUniverse([]string{"BTCUSDT", "ETHUSDT"}, time.Unix(0, 0))
	b := NewUniverse([]string{"BTCUSDT", "ETHUSDT", "AKEUSDT"}, time.Unix(0, 0))
	if a.Hash == b.Hash {
		t.Fatal("hash must change when membership changes")
	}
}

func TestCheckDriftReportsNoDriftForAnIdenticalCatalog(t *testing.T) {
	t.Parallel()
	u := NewUniverse([]string{"BTCUSDT", "ETHUSDT"}, time.Unix(0, 0))
	report := u.CheckDrift([]string{"ETHUSDT", "BTCUSDT"}, time.Unix(600, 0))
	if report.Stale {
		t.Fatalf("expected no drift for the same set in a different order: %+v", report)
	}
	if len(report.AddedSinceStart) != 0 || len(report.RemovedSinceStart) != 0 {
		t.Fatalf("expected no added/removed symbols: %+v", report)
	}
	if report.FrozenCount != 2 || report.LiveCount != 2 {
		t.Fatalf("counts wrong: %+v", report)
	}
}

func TestCheckDriftDetectsAddedAndRemovedSymbols(t *testing.T) {
	t.Parallel()
	u := NewUniverse([]string{"BTCUSDT", "ETHUSDT", "DELISTEDUSDT"}, time.Unix(0, 0))
	report := u.CheckDrift([]string{"BTCUSDT", "ETHUSDT", "NEWLISTINGUSDT"}, time.Unix(600, 0))
	if !report.Stale {
		t.Fatal("expected drift to be detected")
	}
	if len(report.AddedSinceStart) != 1 || report.AddedSinceStart[0] != "NEWLISTINGUSDT" {
		t.Fatalf("added since start = %v, want [NEWLISTINGUSDT]", report.AddedSinceStart)
	}
	if len(report.RemovedSinceStart) != 1 || report.RemovedSinceStart[0] != "DELISTEDUSDT" {
		t.Fatalf("removed since start = %v, want [DELISTEDUSDT]", report.RemovedSinceStart)
	}
	if report.FrozenHash == report.LiveHash {
		t.Fatal("frozen and live hashes must differ when drift is detected")
	}
}

func TestUniverseContains(t *testing.T) {
	t.Parallel()
	u := NewUniverse([]string{"ETHUSDT", "BTCUSDT", "AKEUSDT"}, time.Unix(0, 0))
	for _, symbol := range []string{"AKEUSDT", "BTCUSDT", "ETHUSDT"} {
		if !u.Contains(symbol) {
			t.Fatalf("%s should be contained in %v", symbol, u.Symbols)
		}
	}
	for _, symbol := range []string{"NEWLISTINGUSDT", "", "ZZZUSDT"} {
		if u.Contains(symbol) {
			t.Fatalf("%s should not be contained in %v", symbol, u.Symbols)
		}
	}
}

func TestCheckDriftDoesNotMutateTheFrozenUniverse(t *testing.T) {
	t.Parallel()
	u := NewUniverse([]string{"BTCUSDT"}, time.Unix(0, 0))
	before := u.Hash
	_ = u.CheckDrift([]string{"BTCUSDT", "ETHUSDT", "AKEUSDT"}, time.Unix(600, 0))
	if u.Hash != before {
		t.Fatal("CheckDrift must never mutate the receiver")
	}
	if u.Count() != 1 {
		t.Fatal("CheckDrift must never mutate the receiver's symbol list")
	}
}

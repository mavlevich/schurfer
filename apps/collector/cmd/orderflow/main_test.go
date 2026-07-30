package main

import (
	"testing"
	"time"
)

func TestPruneSeenEventsBoundsInactiveHistoryAndKeepsActiveEvents(t *testing.T) {
	t.Parallel()
	now := time.Unix(1_700_000_000, 0).UTC()
	seen := map[int64]time.Time{
		1: now.Add(-25 * time.Hour),
		2: now.Add(-25 * time.Hour),
		3: now.Add(-time.Hour),
	}
	active := map[int64]struct{}{2: {}}

	pruneSeenEvents(seen, active, now, 24*time.Hour)

	if _, exists := seen[1]; exists {
		t.Fatal("expired inactive event was retained")
	}
	if _, exists := seen[2]; !exists {
		t.Fatal("active event was evicted")
	}
	if _, exists := seen[3]; !exists {
		t.Fatal("recent inactive event was evicted")
	}
}

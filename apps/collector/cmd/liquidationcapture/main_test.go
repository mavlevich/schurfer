package main

import (
	"testing"
	"time"
)

func TestHeartbeatBucketsDueMarksCatchUpIntervalsLate(t *testing.T) {
	next := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	now := time.Date(2026, 8, 25, 12, 2, 3, 0, time.UTC)
	due := heartbeatBucketsDue(next, now)
	if len(due) != 2 {
		t.Fatalf("due buckets = %+v, want two", due)
	}
	if !due[0].Late {
		t.Fatal("12:00 bucket must be incomplete after a >1 minute scheduler delay")
	}
	if due[1].Late {
		t.Fatal("12:01 bucket recorded three seconds after close is within tolerance")
	}
}

func TestHeartbeatBucketsDueDoesNotEmitCurrentOpenMinute(t *testing.T) {
	next := time.Date(2026, 8, 25, 12, 5, 0, 0, time.UTC)
	now := next.Add(59 * time.Second)
	if due := heartbeatBucketsDue(next, now); len(due) != 0 {
		t.Fatalf("open minute unexpectedly due: %+v", due)
	}
}

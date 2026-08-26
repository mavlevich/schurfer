package liquidationcapture

import (
	"testing"
	"time"
)

func TestEvaluateHealth(t *testing.T) {
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	state := &EvaluatorState{
		StartedAt:           now,
		LastHeartbeatBucket: now.Add(-time.Minute),
	}

	sourceStats := SourceStats{}
	writerStats := WriterStats{}

	// Test starting state
	health := EvaluateHealth(state, now, sourceStats, writerStats, 1, 1, false, 100)
	if health.Status != StatusStarting {
		t.Errorf("expected starting, got %s", health.Status)
	}

	state.LastCompleteBucket = now.Add(-time.Minute)
	// Test ok state
	health = EvaluateHealth(state, now, sourceStats, writerStats, 1, 1, false, 100)
	if health.Status != StatusOk {
		t.Errorf("expected ok, got %s", health.Status)
	}

	// Test mismatch
	writerStats.PayloadHashMismatchTotal = 1
	health = EvaluateHealth(state, now, sourceStats, writerStats, 1, 1, false, 100)
	if health.Status != StatusFailed || !health.ShouldExit {
		t.Errorf("expected fatal, got %s", health.Status)
	}
	writerStats.PayloadHashMismatchTotal = 0

	// Test degraded on reconnect storm
	sourceStats.ReconnectTotal = 3
	health = EvaluateHealth(state, now, sourceStats, writerStats, 1, 1, false, 100)
	if health.Status != StatusDegraded {
		t.Errorf("expected degraded, got %s", health.Status)
	}
}

func TestEvaluateHealthFailsWhenStartupNeverProducesCompleteMinute(t *testing.T) {
	now := time.Date(2026, 8, 26, 12, 20, 0, 0, time.UTC)
	state := &EvaluatorState{
		StartedAt:           now.Add(-ProlongedIncompleteAfter - time.Second),
		LastHeartbeatBucket: now.Add(-time.Minute),
	}
	health := EvaluateHealth(state, now, SourceStats{}, WriterStats{}, 0, 1, true, MaxPendingEvents)
	if health.Status != StatusFailed || !health.ShouldExit {
		t.Fatalf("never-complete startup did not fail closed: %+v", health)
	}
}

func TestEvaluatorStateOnlyAdvancesCompleteHeartbeatForCompleteMinute(t *testing.T) {
	state := &EvaluatorState{}
	first := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)

	state.ObserveHeartbeat(first, false)
	if !state.LastCompleteBucket.IsZero() || state.ConsecutiveIncomplete != 1 {
		t.Fatalf("incomplete heartbeat changed complete state: %+v", state)
	}

	state.ObserveHeartbeat(first.Add(time.Minute), true)
	if !state.LastCompleteBucket.Equal(first.Add(time.Minute)) || state.ConsecutiveIncomplete != 0 {
		t.Fatalf("complete heartbeat did not recover state: %+v", state)
	}
}

func TestEvaluateHealthFailsAfterProlongedIncompleteCoverage(t *testing.T) {
	now := time.Date(2026, 8, 26, 12, 20, 0, 0, time.UTC)
	state := &EvaluatorState{
		LastCompleteBucket:    now.Add(-ProlongedIncompleteAfter - time.Second),
		LastHeartbeatBucket:   now.Add(-time.Minute),
		ConsecutiveIncomplete: 11,
	}

	health := EvaluateHealth(state, now, SourceStats{}, WriterStats{}, 1, 1, true, MaxPendingEvents)
	if health.Status != StatusFailed || !health.ShouldExit {
		t.Fatalf("prolonged incomplete coverage did not fail closed: %+v", health)
	}
	if health.ReasonCodes != "prolonged_incomplete,current_minute_incomplete" {
		t.Fatalf("reason codes = %q", health.ReasonCodes)
	}
}

func TestEvaluateHealthTracksActualStatusTransitions(t *testing.T) {
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	state := &EvaluatorState{LastCompleteBucket: now.Add(-time.Minute)}

	first := EvaluateHealth(state, now, SourceStats{}, WriterStats{}, 1, 1, false, MaxPendingEvents)
	second := EvaluateHealth(state, now.Add(5*time.Second), SourceStats{}, WriterStats{}, 1, 1, false, MaxPendingEvents)
	degraded := EvaluateHealth(state, now.Add(10*time.Second), SourceStats{}, WriterStats{}, 0, 1, true, MaxPendingEvents)

	if first.StatusChangedAtMs != now.UnixMilli() || second.StatusChangedAtMs != now.UnixMilli() {
		t.Fatalf("unchanged status moved transition timestamp: first=%d second=%d", first.StatusChangedAtMs, second.StatusChangedAtMs)
	}
	if degraded.StatusChangedAtMs != now.Add(10*time.Second).UnixMilli() {
		t.Fatalf("degraded transition timestamp = %d", degraded.StatusChangedAtMs)
	}
}

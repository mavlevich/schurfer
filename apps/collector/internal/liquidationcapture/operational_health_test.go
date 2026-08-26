package liquidationcapture

import (
	"testing"
	"time"
)

func TestEvaluateHealth(t *testing.T) {
	now := time.Now()
	state := &EvaluatorState{
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

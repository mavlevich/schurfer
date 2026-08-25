package liquidationcapture

import (
	"testing"
	"time"
)

func TestCoverageTrackerStartupMinuteIncompleteThenComplete(t *testing.T) {
	tracker, err := NewCoverageTracker(1)
	if err != nil {
		t.Fatal(err)
	}
	tracker.ObserveLifecycle(LifecycleEvent{SessionID: "a", ConnectedAt: time.Now()})
	first := tracker.SnapshotAndReset(time.Now(), "binance", "linear",
		CoverageLatestPerSymbol1000ms, "process", "universe", SourceStats{}, WriterStats{})
	if first.Complete || !first.DataLossDetected {
		t.Fatalf("startup partial minute must be incomplete: %+v", first)
	}
	second := tracker.SnapshotAndReset(time.Now().Add(time.Minute), "binance", "linear",
		CoverageLatestPerSymbol1000ms, "process", "universe", SourceStats{}, WriterStats{})
	if !second.Complete || second.DataLossDetected {
		t.Fatalf("fully connected next minute must be complete: %+v", second)
	}
}

func TestCoverageTrackerDisconnectAndQueueDropCannotHealWithinMinute(t *testing.T) {
	tracker, _ := NewCoverageTracker(1)
	tracker.ObserveLifecycle(LifecycleEvent{SessionID: "a", ConnectedAt: time.Now()})
	_ = tracker.SnapshotAndReset(time.Now(), "bybit", "linear", CoverageCompleteStream,
		"process", "universe", SourceStats{}, WriterStats{})
	tracker.ObserveLifecycle(LifecycleEvent{SessionID: "a", DisconnectedAt: time.Now()})
	tracker.ObserveLifecycle(LifecycleEvent{SessionID: "b", ConnectedAt: time.Now()})
	disconnected := tracker.SnapshotAndReset(time.Now(), "bybit", "linear", CoverageCompleteStream,
		"process", "universe", SourceStats{}, WriterStats{})
	if disconnected.Complete {
		t.Fatal("disconnect followed by reconnect incorrectly healed the same minute")
	}
	dropped := tracker.SnapshotAndReset(time.Now(), "bybit", "linear", CoverageCompleteStream,
		"process", "universe", SourceStats{}, WriterStats{QueueDropsTotal: 1})
	if dropped.Complete {
		t.Fatal("writer queue loss incorrectly produced a complete heartbeat")
	}
}

func TestCoverageTrackerPendingOrFailedPersistenceCannotProduceCompleteHeartbeat(t *testing.T) {
	tracker, _ := NewCoverageTracker(1)
	tracker.ObserveLifecycle(LifecycleEvent{SessionID: "a", ConnectedAt: time.Now()})
	_ = tracker.SnapshotAndReset(time.Now(), "bybit", "linear", CoverageCompleteStream,
		"process", "universe", SourceStats{}, WriterStats{})

	pending := tracker.SnapshotAndReset(time.Now().Add(time.Minute), "bybit", "linear",
		CoverageCompleteStream, "process", "universe", SourceStats{}, WriterStats{QueueDepth: 1})
	if pending.Complete {
		t.Fatal("pending event incorrectly produced a complete durable heartbeat")
	}

	persistFailed := tracker.SnapshotAndReset(time.Now().Add(2*time.Minute), "bybit", "linear",
		CoverageCompleteStream, "process", "universe", SourceStats{}, WriterStats{PersistErrorsTotal: 1})
	if persistFailed.Complete {
		t.Fatal("new persistence failure incorrectly produced a complete durable heartbeat")
	}

	recovered := tracker.SnapshotAndReset(time.Now().Add(3*time.Minute), "bybit", "linear",
		CoverageCompleteStream, "process", "universe", SourceStats{}, WriterStats{PersistErrorsTotal: 1})
	if !recovered.Complete {
		t.Fatal("a prior persistence incident incorrectly poisoned every later heartbeat")
	}
}

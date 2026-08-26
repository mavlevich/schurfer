package liquidationcapture

import (
	"strings"
	"time"
)

const (
	StatusStarting = "starting"
	StatusOk       = "ok"
	StatusDegraded = "degraded"
	StatusFailed   = "failed"
)

type Snapshot struct {
	Source SourceStats
	Writer WriterStats
}

type EvaluatorState struct {
	CurrentStatus         string
	StatusChangedAt       time.Time
	LastHeartbeatBucket   time.Time
	LastCompleteBucket    time.Time
	ConsecutiveIncomplete int
	LastSnapshot          Snapshot
	LastSnapshotTime      time.Time
}

type EvaluatedHealth struct {
	Status      string
	ReasonCodes string
	ShouldExit  bool
	ShouldAlert bool // Maybe not directly needed since Notifier handles alerting, but we can compute it

	LastHeartbeatBucketMs         int64
	LastCompleteHeartbeatBucketMs int64
	ConsecutiveIncompleteMinutes  int

	ConnectedConnections int
	ExpectedConnections  int

	ReconnectsWindow   uint64
	ReadTimeoutsWindow uint64

	WriterQueueDepth         int
	WriterQueueUtilization   float64
	WriterQueueDropsDelta    uint64
	PersistErrorsDelta       uint64
	PayloadHashMismatchTotal uint64

	LastEventAtMs int64
}

// Evaluate evaluates health based on V2 contract
func EvaluateHealth(
	state *EvaluatorState,
	now time.Time,
	sourceStats SourceStats,
	writerStats WriterStats,
	connected int,
	expected int,
	incompleteMinute bool,
	queueCapacity int,
) EvaluatedHealth {
	var reasons []string
	shouldExit := false

	// Calculate deltas
	dropsDelta := writerStats.QueueDropsTotal - state.LastSnapshot.Writer.QueueDropsTotal
	persistDelta := writerStats.PersistErrorsTotal - state.LastSnapshot.Writer.PersistErrorsTotal
	reconnectsDelta := sourceStats.ReconnectTotal - state.LastSnapshot.Source.ReconnectTotal
	timeoutsDelta := sourceStats.ReadTimeoutTotal - state.LastSnapshot.Source.ReadTimeoutTotal
	mismatchTotal := writerStats.PayloadHashMismatchTotal

	status := StatusOk

	if mismatchTotal > 0 {
		status = StatusFailed
		reasons = append(reasons, "fatal_payload_mismatch")
		shouldExit = true
	} else if dropsDelta > 0 {
		status = StatusFailed
		reasons = append(reasons, "queue_drop_critical")
		shouldExit = true
	} else if state.LastCompleteBucket.IsZero() {
		status = StatusStarting
		reasons = append(reasons, "awaiting_first_complete_minute")
	} else if time.Since(state.LastCompleteBucket) > 10*time.Minute {
		status = StatusFailed
		reasons = append(reasons, "prolonged_incomplete")
		shouldExit = true
	} else if state.ConsecutiveIncomplete > 0 {
		status = StatusDegraded
		reasons = append(reasons, "incomplete_minute")
	}

	if connected < expected {
		if status == StatusOk {
			status = StatusDegraded
		}
		reasons = append(reasons, "disconnected_streams")
	}

	if reconnectsDelta > 2 { // Reconnect storm
		if status == StatusOk {
			status = StatusDegraded
		}
		reasons = append(reasons, "reconnect_storm")
	}

	if persistDelta > 0 {
		if writerStats.QueueDepth > int(float64(queueCapacity)*0.9) {
			status = StatusFailed
			reasons = append(reasons, "sustained_persistence_failure")
			shouldExit = true
		} else {
			if status == StatusOk {
				status = StatusDegraded
			}
			reasons = append(reasons, "persist_error")
		}
	}

	queueUtil := float64(writerStats.QueueDepth) / float64(queueCapacity)
	if queueUtil > 0.8 {
		if status == StatusOk {
			status = StatusDegraded
		}
		reasons = append(reasons, "high_backlog")
	}

	reasonStr := strings.Join(reasons, ",")

	return EvaluatedHealth{
		Status:                        status,
		ReasonCodes:                   reasonStr,
		ShouldExit:                    shouldExit,
		LastHeartbeatBucketMs:         unixMilliOrZero(state.LastHeartbeatBucket),
		LastCompleteHeartbeatBucketMs: unixMilliOrZero(state.LastCompleteBucket),
		ConsecutiveIncompleteMinutes:  state.ConsecutiveIncomplete,
		ConnectedConnections:          connected,
		ExpectedConnections:           expected,
		ReconnectsWindow:              reconnectsDelta,
		ReadTimeoutsWindow:            timeoutsDelta,
		WriterQueueDepth:              writerStats.QueueDepth,
		WriterQueueUtilization:        queueUtil,
		WriterQueueDropsDelta:         dropsDelta,
		PersistErrorsDelta:            persistDelta,
		PayloadHashMismatchTotal:      mismatchTotal,
		LastEventAtMs:                 unixMilliOrZero(sourceStats.LastEventAt), // Computed in more complex tracking if needed
	}
}

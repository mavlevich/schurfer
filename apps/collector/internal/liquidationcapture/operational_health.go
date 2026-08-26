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

	ProlongedIncompleteAfter = 10 * time.Minute
)

type Snapshot struct {
	Source SourceStats
	Writer WriterStats
}

type EvaluatorState struct {
	StartedAt             time.Time
	CurrentStatus         string
	CurrentReasonCodes    string
	StatusChangedAt       time.Time
	LastHeartbeatBucket   time.Time
	LastCompleteBucket    time.Time
	ConsecutiveIncomplete int
	LastSnapshot          Snapshot
	LastSnapshotTime      time.Time
}

type EvaluatedHealth struct {
	Status            string
	ReasonCodes       string
	ShouldExit        bool
	StatusChangedAtMs int64

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

// ObserveHeartbeat updates the durable coverage window only after the
// heartbeat write has completed. A timely write is not sufficient: the
// heartbeat itself must also declare the minute complete.
func (state *EvaluatorState) ObserveHeartbeat(bucket time.Time, complete bool) {
	state.LastHeartbeatBucket = bucket
	if complete {
		state.LastCompleteBucket = bucket
		state.ConsecutiveIncomplete = 0
		return
	}
	state.ConsecutiveIncomplete++
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

	// Calculate deltas. These counters are process-local and monotonic for the
	// lifetime of one EvaluatorState.
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
		if !state.StartedAt.IsZero() && now.Sub(state.StartedAt) > ProlongedIncompleteAfter {
			status = StatusFailed
			reasons = append(reasons, "startup_never_complete")
			shouldExit = true
		} else {
			status = StatusStarting
			reasons = append(reasons, "awaiting_first_complete_minute")
		}
	} else if now.Sub(state.LastCompleteBucket) > ProlongedIncompleteAfter {
		status = StatusFailed
		reasons = append(reasons, "prolonged_incomplete")
		shouldExit = true
	} else if state.ConsecutiveIncomplete > 0 {
		status = StatusDegraded
		reasons = append(reasons, "incomplete_minute")
	}
	if incompleteMinute {
		if status == StatusOk {
			status = StatusDegraded
		}
		reasons = append(reasons, "current_minute_incomplete")
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

	queueUtil := 0.0
	if queueCapacity > 0 {
		queueUtil = float64(writerStats.QueueDepth) / float64(queueCapacity)
	}
	if queueUtil > 0.8 {
		if status == StatusOk {
			status = StatusDegraded
		}
		reasons = append(reasons, "high_backlog")
	}

	reasonStr := strings.Join(reasons, ",")
	if state.CurrentStatus != status || state.CurrentReasonCodes != reasonStr {
		state.CurrentStatus = status
		state.CurrentReasonCodes = reasonStr
		state.StatusChangedAt = now
	}

	return EvaluatedHealth{
		Status:                        status,
		ReasonCodes:                   reasonStr,
		ShouldExit:                    shouldExit,
		StatusChangedAtMs:             unixMilliOrZero(state.StatusChangedAt),
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

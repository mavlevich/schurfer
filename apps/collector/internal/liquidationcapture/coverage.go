package liquidationcapture

import (
	"errors"
	"sync"
	"time"
)

type Heartbeat struct {
	Exchange                     string
	MarketType                   string
	CoverageKind                 CoverageKind
	ProcessSessionID             string
	UniverseVersion              string
	BucketStart                  time.Time
	ExpectedConnections          int
	ConnectedConnections         int
	DataLossDetected             bool
	Complete                     bool
	EventsReceivedTotal          uint64
	EventsPersistedTotal         uint64
	DuplicateEventsTotal         uint64
	QueueDropsTotal              uint64
	InvalidEventsTotal           uint64
	OutOfScopeTotal              uint64
	ScopeTagMissingAcceptedTotal uint64
	ReconnectTotal               uint64
	ReadTimeoutTotal             uint64
}

// CoverageTracker records whether every expected WebSocket connection stayed
// ready throughout a heartbeat minute. Any disconnect, pending/failed
// persistence, or local queue loss makes that interval incomplete even if the
// feed recovered before the heartbeat was written.
type CoverageTracker struct {
	mu                sync.Mutex
	expected          int
	active            map[string]struct{}
	dataLossDetected  bool
	lastQueueDrops    uint64
	lastInvalidEvents uint64
	lastPersistErrors uint64
}

func NewCoverageTracker(expected int) (*CoverageTracker, error) {
	if expected <= 0 {
		return nil, errors.New("expected connections must be positive")
	}
	return &CoverageTracker{
		expected: expected,
		active:   make(map[string]struct{}, expected),
		// The startup partial minute is never claimed complete.
		dataLossDetected: true,
	}, nil
}

func (tracker *CoverageTracker) ObserveLifecycle(event LifecycleEvent) {
	tracker.mu.Lock()
	defer tracker.mu.Unlock()
	if event.DisconnectedAt.IsZero() {
		tracker.active[event.SessionID] = struct{}{}
		return
	}
	delete(tracker.active, event.SessionID)
	tracker.dataLossDetected = true
}

func (tracker *CoverageTracker) MarkDataLoss() {
	tracker.mu.Lock()
	tracker.dataLossDetected = true
	tracker.mu.Unlock()
}

func (tracker *CoverageTracker) SnapshotAndReset(
	bucketStart time.Time,
	exchange string,
	marketType string,
	coverageKind CoverageKind,
	processSessionID string,
	universeVersion string,
	source SourceStats,
	writer WriterStats,
) Heartbeat {
	tracker.mu.Lock()
	defer tracker.mu.Unlock()
	connected := len(tracker.active)
	loss := tracker.dataLossDetected || connected != tracker.expected ||
		writer.QueueDepth > 0 ||
		writer.QueueDropsTotal > tracker.lastQueueDrops ||
		source.EventsInvalidTotal > tracker.lastInvalidEvents ||
		writer.PersistErrorsTotal > tracker.lastPersistErrors ||
		writer.PayloadHashMismatchTotal > 0
	heartbeat := Heartbeat{
		Exchange: exchange, MarketType: marketType, CoverageKind: coverageKind,
		ProcessSessionID: processSessionID, UniverseVersion: universeVersion,
		BucketStart:         bucketStart.UTC().Truncate(time.Minute),
		ExpectedConnections: tracker.expected, ConnectedConnections: connected,
		DataLossDetected: loss, Complete: !loss,
		EventsReceivedTotal:          source.EventsAcceptedTotal,
		EventsPersistedTotal:         writer.EventsPersistedTotal,
		DuplicateEventsTotal:         writer.DuplicateEventsTotal,
		QueueDropsTotal:              writer.QueueDropsTotal,
		InvalidEventsTotal:           source.EventsInvalidTotal,
		OutOfScopeTotal:              source.EventsOutOfScopeTotal,
		ScopeTagMissingAcceptedTotal: source.ScopeTagMissingAcceptedTotal,
		ReconnectTotal:               source.ReconnectTotal,
		ReadTimeoutTotal:             source.ReadTimeoutTotal,
	}
	tracker.lastQueueDrops = writer.QueueDropsTotal
	tracker.lastInvalidEvents = source.EventsInvalidTotal
	tracker.lastPersistErrors = writer.PersistErrorsTotal
	tracker.dataLossDetected = connected != tracker.expected
	return heartbeat
}

func (tracker *CoverageTracker) Connected() (connected int, expected int, loss bool) {
	tracker.mu.Lock()
	defer tracker.mu.Unlock()
	return len(tracker.active), tracker.expected, tracker.dataLossDetected
}

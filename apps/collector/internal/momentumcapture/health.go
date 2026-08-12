package momentumcapture

import "time"

// Health is a machine-readable snapshot of momentum-capture's own state,
// published to Redis on the same pattern as hotset.RedisStore.StoreHealth
// (see apps/collector/internal/hotset/redis_store.go, already read by
// `make orderflow-health`/`make prod-orderflow-health`). This is the
// mechanism for evaluating the 48-72h resource-and-data-quality canary
// (ROADMAP "Active course" item 6) without grepping logs by hand.
//
// Fields are grouped by the part of the pipeline that owns them. Most
// non-universe fields are populated once the bounded event loop and
// writer exist (later steps of this PR); the struct is defined in full
// now so later steps extend it rather than redesign it.
type Health struct {
	Status        string // "ok" | "degraded_universe_stale" | "degraded_queue_pressure" | ...
	StartedAt     time.Time
	UpdatedAt     time.Time
	LastBarAt     time.Time
	LastPersistAt time.Time

	// Universe: see Universe/DriftReport/ReadinessTracker. Built by
	// BuildUniverseHealth.
	UniverseSnapshotAt     time.Time
	UniverseAgeSeconds     float64
	SubscribedSymbols      int
	CurrentExchangeSymbols int
	AddedSinceStart        []string
	RemovedSinceStart      []string
	FrozenUniverseHash     string
	LiveUniverseHash       string
	UniverseStale          bool
	ReadySymbols           int
	SymbolsMissingTicker   []string
	SymbolsMissingTrades   []string

	// Initial Bybit catalog scope. These counters prove which instrument
	// classes were deliberately included or excluded when the immutable
	// universe was frozen.
	CatalogItemsTotal           int
	CryptoPerpetualsIncluded    int
	StandardCryptoIncluded      int
	InnovationCryptoIncluded    int
	DatedFuturesExcluded        int
	StockPerpetualsExcluded     int
	CommodityPerpetualsExcluded int
	UnknownContractExcluded     int
	UnknownSymbolTypeExcluded   int
	InvalidInstrumentExcluded   int
	NonUSDTExcluded             int
	NonTradingExcluded          int

	// Ingestion (bounded event loop, see that step of this PR).
	InputQueueDepth      int
	InputQueuePeak       int
	InputQueueDropsTotal uint64
	BarsCompletedTotal   uint64
	LateEventsTotal      uint64
	TickerGapTotal       uint64 // proactive per-symbol silence detections
	LastDiscontinuityAt  time.Time
	LastDiscontinuityFor string // symbol or "*" for a feed-wide event

	// Trade WebSocket, per Source.StreamStats() plus per-shard lifecycle
	// (see trades.go's TradeLifecycleEvent).
	TradeReconnectTotal   uint64
	TradeReadTimeoutTotal uint64

	// NATS (ticker/OI feed), see the bounded event loop step.
	NATSDisconnectTotal   uint64
	NATSReconnectTotal    uint64
	NATSSlowConsumerTotal uint64
	NATSDroppedTotal      uint64

	// Writer (see writer.go).
	WriterQueueDepth         int
	WriterQueuePeak          int
	WriterQueueDropsTotal    uint64
	BarsPersistedTotal       uint64
	PersistErrorsTotal       uint64
	PersistRetriesTotal      uint64
	RowsWrittenTotal         uint64
	PayloadHashMismatchTotal uint64
	ProjectedBytesPerDay     float64

	// Lag, from momentum.Bar's own bounded diagnostics, aggregated across
	// the currently tracked universe.
	TradeLagMaxMs  int64
	TickerLagMaxMs int64
}

// BuildUniverseHealth fills in Health's universe-related fields from a
// frozen Universe, its latest drift check against the live exchange
// catalog, and the current ReadinessTracker state. It does not touch any
// other Health field, so callers compose it with the ingestion/writer
// sections built elsewhere.
func BuildUniverseHealth(universe Universe, drift DriftReport, readiness *ReadinessTracker, now time.Time) Health {
	ready := 0
	for _, symbol := range universe.Symbols {
		if readiness.Ready(symbol) {
			ready++
		}
	}
	return Health{
		UniverseSnapshotAt:     universe.CapturedAt,
		UniverseAgeSeconds:     now.Sub(universe.CapturedAt).Seconds(),
		SubscribedSymbols:      universe.Count(),
		CurrentExchangeSymbols: drift.LiveCount,
		AddedSinceStart:        drift.AddedSinceStart,
		RemovedSinceStart:      drift.RemovedSinceStart,
		FrozenUniverseHash:     universe.Hash,
		LiveUniverseHash:       drift.LiveHash,
		UniverseStale:          drift.Stale,
		ReadySymbols:           ready,
		SymbolsMissingTicker:   readiness.MissingTicker(),
		SymbolsMissingTrades:   readiness.MissingTrades(),
	}
}

// ApplyWriterStats copies a Writer's own counters into health's writer
// section, leaving every other field untouched (same composition pattern
// as BuildUniverseHealth). Returns the updated Health for chaining.
func ApplyWriterStats(health Health, stats WriterStats) Health {
	health.WriterQueueDepth = stats.QueueDepth
	health.WriterQueuePeak = stats.QueuePeak
	health.WriterQueueDropsTotal = stats.QueueDropsTotal
	health.LastPersistAt = stats.LastPersistAt
	health.BarsPersistedTotal = stats.BarsPersistedTotal
	health.PersistErrorsTotal = stats.PersistErrorsTotal
	health.PersistRetriesTotal = stats.PersistRetriesTotal
	health.RowsWrittenTotal = stats.RowsWrittenTotal
	health.PayloadHashMismatchTotal = stats.PayloadHashMismatchTotal
	return health
}

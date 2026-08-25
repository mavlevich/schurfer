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
	// Exchange identifies which venue's momentum-capture process produced
	// this snapshot ("bybit", "binance", ...). Required: RedisStore.
	// StoreHealth refuses to publish a Health with this unset, because an
	// empty Exchange used to mean every venue's process would key its
	// health snapshot identically (a single unparameterized HealthKey
	// constant) -- the second venue's process would silently overwrite the
	// first's, the same masking-counter failure mode as any other
	// unscoped shared key, just for observability instead of accounting.
	// See docs/research/momentum-canary-multivenue-v1.md.
	Exchange      string
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

	// Initial catalog scope. These counters prove which instrument classes
	// were deliberately included or excluded when the immutable universe
	// was frozen. CatalogItemsTotal/CryptoPerpetualsIncluded/
	// NonUSDTExcluded/NonTradingExcluded/InvalidInstrumentExcluded are
	// genuinely shared vocabulary across venues (both bybit.
	// SymbolCatalogCounts and binance.SymbolCatalogCounts carry the same
	// concept under the same name); the rest of this named-field block
	// (StandardCryptoIncluded..UnknownSymbolTypeExcluded) is Bybit's own
	// finer-grained classification and stays zero for every other venue,
	// which is honest, not a lossy compromise -- each venue's own
	// exclusion reasons that have no Bybit equivalent belong in
	// ExclusionCounts below instead of being force-fit into a Bybit-shaped
	// field.
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
	// ExclusionCounts is every catalog exclusion reason that has no
	// Bybit-shaped field above, keyed the same way momentumsource.
	// UniverseSnapshot.ExclusionCounts already keys them (see binance.
	// translateUniverse) -- "non_perpetual_contract", "underlying_index",
	// "unknown_underlying_type" for Binance today. nil/empty for a venue
	// whose whole taxonomy fit the named fields above.
	ExclusionCounts map[string]int

	// Ingestion (bounded event loop, see that step of this PR).
	InputQueueDepth      int
	InputQueuePeak       int
	InputQueueDropsTotal uint64
	BarsCompletedTotal   uint64
	LateEventsTotal      uint64
	TickerGapTotal       uint64 // proactive per-symbol silence detections

	// Additive mark/index/funding feed. These counters stay separate from
	// ticker/OI because Binance transports this state on its own stream.
	DerivativesAcceptedTotal    uint64
	DerivativesInvalidTotal     uint64
	DerivativesOutOfScopeTotal  uint64
	DerivativesReconnectTotal   uint64
	DerivativesReadTimeoutTotal uint64
	DerivativesGapTotal         uint64 // proactive per-symbol silence detections
	LastDerivativesAt           time.Time
	LastDiscontinuityAt         time.Time
	LastDiscontinuityFor        string // symbol or "*" for a feed-wide event

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

	// Processing latency, measured in-process using bounded histograms.
	// ReceiveToHandle starts at the feed adapter's local receive timestamp;
	// handler and maintenance timings isolate work owned by this process.
	TradeReceiveToHandleCount  uint64
	TradeReceiveToHandleP95Us  int64
	TradeReceiveToHandleP99Us  int64
	TradeReceiveToHandleMaxUs  int64
	TradeHandlerCount          uint64
	TradeHandlerP95Us          int64
	TradeHandlerP99Us          int64
	TradeHandlerMaxUs          int64
	TickerReceiveToHandleCount uint64
	TickerReceiveToHandleP95Us int64
	TickerReceiveToHandleP99Us int64
	TickerReceiveToHandleMaxUs int64
	TickerHandlerCount         uint64
	TickerHandlerP95Us         int64
	TickerHandlerP99Us         int64
	TickerHandlerMaxUs         int64
	FlushCount                 uint64
	FlushP95Us                 int64
	FlushP99Us                 int64
	FlushMaxUs                 int64
	HealthPublishCount         uint64
	HealthPublishP95Us         int64
	HealthPublishP99Us         int64
	HealthPublishMaxUs         int64
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

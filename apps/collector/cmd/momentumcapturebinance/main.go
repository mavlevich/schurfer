// Command momentumcapturebinance runs the Binance early-momentum-capture
// line (ROADMAP "Active course" item 7, feat/binance-momentum-capture-v1):
// a frozen universe of USDT-margined linear perpetuals, trades pulled
// directly from Binance's public combined-stream WebSocket, and open
// interest polled directly from Binance's own REST API -- all aggregated by
// the SAME momentum.Engine cmd/momentumcapture uses and persisted to the
// SAME timeseries.bybit_momentum_bars_1m hypertable (already exchange-
// scoped by its own primary key and compress_segmentby; see packages/
// journal/migrations/versions/0024_bybit_momentum_bars_1m.py).
//
// This is a deliberate FORK of cmd/momentumcapture, not a shared binary
// parameterized by venue, matching the roadmap's own "Bybit, Binance, and
// combined always stay separate books" rule: a bug in one venue's capture
// process must never be able to take the other down, and the two venues'
// actual data shapes differ enough (see below) that a shared abstraction
// would cost real clarity for no benefit yet. See docs/research/binance-
// momentum-capture-v1.md for the full design record, including exactly
// which pieces are copied verbatim from cmd/momentumcapture (documented
// duplication, not an oversight) versus genuinely different.
//
// The biggest structural difference from cmd/momentumcapture: Bybit's
// ticker/OI feed arrives over NATS from a SEPARATE process (cmd/collector)
// that this binary has no equivalent of. binance.Adapter deliberately does
// not implement momentumsource.TickerSource (see docs/research/binance-
// momentum-source-v1.md) -- Binance's OI is a REST poll this process runs
// itself, in-process, and this process's only TickerObservation calls
// carry OpenInterest with LastPrice/BidPrice/AskPrice left nil, which the
// engine's own TickerObservation contract already supports ("a delta can
// carry price with no OI, OI with no price change, or neither").
//
// This originally meant every bar had OpenPrice/HighPrice/LowPrice/
// ClosePrice permanently nil too (momentum.Engine only ever set those
// from AddTickerObservation, which this process never calls with a real
// price) -- see docs/research/binance-watch-input-readiness-v1.md for
// the incident that caused (momentum_flow_watch_binance producing zero
// decisions for 32+ hours while reporting healthy). Fixed by feat/
// momentum-trade-price-source-v1: this Engine is constructed with
// momentum.PriceSourceAggregateTrade (see main()'s own wiring below),
// so OHLC is derived from the same aggTrade prices already flowing for
// flow/notional accounting, with its own explicit provenance (see
// momentum.Bar's own PriceSource/FirstPriceEventAt doc comments) --
// never silently repurposing the ticker-specific fields. LastBidPrice/
// LastAskPrice remain permanently nil: those genuinely need a real
// bid/ask feed, which Binance still does not provide here (see
// docs/research/momentum-trade-price-source-v1.md's own "What this
// still cannot capture" section).
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mavlevich/schurfer/collector/internal/binance"
	"github.com/mavlevich/schurfer/collector/internal/momentum"
	"github.com/mavlevich/schurfer/collector/internal/momentumcapture"
	"github.com/redis/go-redis/v9"
)

const (
	exchange = "binance"
	// marketType is deliberately NOT binance.MarketType
	// ("linear_usdt_perpetual", 21 bytes): that constant is the
	// momentumsource/venue-capability-matrix domain's own identity label,
	// not this table's market_type column, which is VARCHAR(16)
	// (packages/journal/migrations/versions/0024_bybit_momentum_bars_1m.py)
	// and means the PRODUCT TYPE (linear vs inverse vs spot) -- genuinely
	// the same for both venues here. Passing binance.MarketType directly
	// into NewWriter would fail every single insert with "value too long
	// for type character varying(16)" (a code-review finding: no unit test
	// here catches it, since every test constructs Writer with a nil
	// pgxpool). "linear" also matches cmd/momentumcapture's own literal
	// exactly, so a future cross-venue query grouping by market_type
	// actually sees both venues, not just whichever one happened to match.
	marketType = "linear"

	tradeEventBuffer     = 8192
	tradeDropBuffer      = 256
	lifecycleEventBuffer = 64
	// openInterestEventBuffer is sized far below tradeEventBuffer: even
	// under fix/binance-oi-poll-scheduler-v1's own concurrent worker pool,
	// at most cfg.OpenInterestScheduler.Workers (default 8) readings can
	// land near-simultaneously -- a small, bounded burst, nothing like the
	// trade firehose's own rate.
	openInterestEventBuffer = 512
	writerInboxBuffer       = 64
	driftResultBuffer       = 2

	flushInterval       = 5 * time.Second
	driftCheckInterval  = 5 * time.Minute
	healthInterval      = 5 * time.Second
	writerFlushInterval = 5 * time.Second
	// shutdownTimeout mirrors cmd/momentumcapture's own reasoning: bounded
	// by the trade WS layer's own 60s read timeout (internal/binance/
	// trades.go's readTimeout = 3x its 20s ping interval, same convention
	// as bybit). The OI poll loop itself returns almost immediately on ctx
	// cancellation (no long blocking read to wait out), so it never drives
	// this budget.
	shutdownTimeout = 75 * time.Second

	// openInterestGapThresholdMultiple proactively marks a symbol's OI feed
	// interrupted after this many expected full-cycle durations pass with
	// no observation at all -- the direct analog of cmd/momentumcapture's
	// own tickerGapThreshold, and here it is the ONLY discontinuity-
	// detection mechanism for this feed (Bybit additionally gets
	// reconnect-based detection via a per-symbol StreamSessionID change; a
	// REST poll has no "session" to change). The actual threshold
	// (openInterestGapThreshold, an application field, not a const) is
	// computed at startup from the real universe size and the configured
	// scheduler's own rate limit -- see openInterestExpectedCycleDuration
	// -- instead of a single hardcoded interval, because fix/binance-oi-
	// poll-scheduler-v1's whole point is that per-symbol cadence is no
	// longer one fixed number independent of how many symbols exist.
	openInterestGapThresholdMultiple = 3

	// openInterestGapThresholdFloor keeps the computed threshold from
	// collapsing to near-zero for a small universe (a handful of symbols
	// still divides a generous per-minute budget into a tiny expected
	// cycle -- and this repo's own tests run against 1-2 symbol
	// fixtures): even one symbol's own single-request latency can jitter
	// by a meaningful fraction of a second under real network conditions,
	// so a threshold below this floor would false-positive on ordinary
	// variance, not a genuine interruption.
	openInterestGapThresholdFloor = 30 * time.Second

	// estimatedHotBytesPerRow reuses cmd/momentumcapture's own measured
	// figure: both venues write the identical row shape to the identical
	// table, so a real Bybit measurement is the right estimate here too,
	// not a guess. Revisit once Binance's own row width is independently
	// measured (its rows are hollow across the entire OHLC/bid/ask column
	// group, which trades bytes for compressibility in a way the estimate
	// does not distinguish).
	estimatedHotBytesPerRow   = 1143.6
	queuePressureWarnFraction = 0.5
)

type config struct {
	DatabaseURL string
	RedisAddr   string
	// OpenInterestScheduler overrides binance.DefaultOpenInterestSchedulerConfig
	// via OI_POLL_WORKERS/OI_POLL_RATE_LIMIT_PER_MINUTE -- operationally
	// tunable without a code change once a real measured per-request
	// latency distribution exists (see docs/research/
	// binance-oi-poll-scheduler-v1.md's own "What this PR does not do"),
	// rather than hardcoding a guess now.
	OpenInterestScheduler binance.OpenInterestSchedulerConfig
}

type counters struct {
	tradesAcceptedTotal       uint64
	tradesInvalidTotal        uint64
	tradeDropsTotal           uint64
	openInterestAcceptedTotal uint64
	openInterestInvalidTotal  uint64
	// openInterestOutOfScopeTotal is distinct from openInterestInvalidTotal
	// (a code-review finding): cmd/momentumcapture keeps tickersOutOfScopeTotal
	// separate from tickersInvalidTotal for exactly this reason -- "the
	// symbol isn't in scope" (universe/catalog drift) and "the payload
	// itself is malformed" (a Binance API data-quality issue) point an
	// operator at two different places to look, and collapsing them into
	// one counter erases that distinction.
	openInterestOutOfScopeTotal uint64
	openInterestGapTotal        uint64 // proactive per-symbol silence detections
	tradeLifecycleTotal         uint64
	// tradeReconnectTotal/tradeReadTimeoutTotal are tallied directly from
	// TradeLifecycleEvent here, unlike cmd/momentumcapture which reads
	// bybit.Source.StreamStats()'s own atomic counters: binance.Source has
	// no equivalent StreamStats method (nothing else needs it yet), and
	// adding one purely for this would touch already-merged, already-
	// tested internal/binance files for a single call site. Event-driven
	// tallying from the lifecycle callback this process already consumes
	// is equally accurate and keeps this PR additive-only.
	tradeReconnectTotal   uint64
	tradeReadTimeoutTotal uint64
	barsCompletedTotal    uint64
	lateEventsTotal       uint64
	writerInboxDropsTotal uint64
	inputQueuePeak        int
	lastDiscontinuityAt   time.Time
	lastDiscontinuityFor  string
	lastBarAt             time.Time
	tradeLagMaxMs         int64
	tickerLagMaxMs        int64
	tradeReceiveToHandle  latencyHistogram
	tradeHandler          latencyHistogram
	// openInterestReceiveToHandle is the OI-side equivalent of
	// tradeReceiveToHandle: how long a reading waited in openInterestEvents
	// before handleOpenInterest actually ran (a code-review finding --
	// an earlier version tracked handler duration but not queue wait,
	// unlike the trade path).
	openInterestReceiveToHandle latencyHistogram
	openInterestHandler         latencyHistogram
	flush                       latencyHistogram
	healthPublish               latencyHistogram
}

type application struct {
	engine      *momentum.Engine
	writer      *momentumcapture.Writer
	universe    momentumcapture.Universe
	readiness   *momentumcapture.ReadinessTracker
	source      *binance.Source
	healthStore *momentumcapture.RedisStore

	tradeEvents        chan binance.PublicTrade
	tradeDrops         chan binance.PublicTrade
	lifecycleEvents    chan binance.TradeLifecycleEvent
	openInterestEvents chan binance.OpenInterestReading

	writerInbox  chan []momentum.Bar
	driftResults chan momentumcapture.DriftReport

	openInterestLastSeenAt map[string]time.Time
	openInterestGapMarked  map[string]bool
	// openInterestGapThreshold is openInterestGapThresholdMultiple times
	// the real expected full-cycle duration for this run's own universe
	// size and configured scheduler rate -- computed once at startup (see
	// openInterestExpectedCycleDuration), not a package-level const,
	// because it genuinely depends on how many symbols this process is
	// actually subscribed to.
	openInterestGapThreshold time.Duration

	lastDrift momentumcapture.DriftReport
	catalog   binance.SymbolCatalogCounts

	// tradeDropsLost mirrors cmd/momentumcapture's own field exactly: the
	// absolute last resort when even tradeDrops is full, incremented from
	// a trade shard's own goroutine, so it alone must be atomic.
	tradeDropsLost atomic.Uint64
	// openInterestDropsLost is consumeOpenInterest's own equivalent
	// (a code-review finding: an earlier version dropped a full-queue
	// reading with only a log line, no counter at all, breaking the same
	// non-blocking-drop-and-count contract every other loss path here
	// honors). Incremented from PollOpenInterest's own goroutine, so it
	// alone must be atomic too, same reasoning as tradeDropsLost.
	openInterestDropsLost atomic.Uint64

	tradeVisibilityLost     lossLatch
	lifecycleVisibilityLost lossLatch

	stats counters
}

// lossLatch is copied verbatim from cmd/momentumcapture: see that
// package's own doc comment for why a latch, not a plain counter.
type lossLatch struct {
	triggered atomic.Bool
	atNanos   atomic.Int64
}

func (l *lossLatch) markOnce(at time.Time) {
	if l.triggered.CompareAndSwap(false, true) {
		l.atNanos.Store(at.UnixNano())
	}
}

func (l *lossLatch) consume() (time.Time, bool) {
	if !l.triggered.CompareAndSwap(true, false) {
		return time.Time{}, false
	}
	return time.Unix(0, l.atNanos.Load()), true
}

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	configureLogging()
	cfg := loadConfig()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	source := binance.NewSource()
	catalog, err := source.FetchSymbolCatalog(ctx)
	if err != nil {
		return fmt.Errorf("fetch initial universe: %w", err)
	}
	universe := momentumcapture.NewUniverse(catalog.CryptoPerpetualSymbols, time.Now())
	slog.Info("momentumcapturebinance.universe_frozen", "symbols", universe.Count(), "hash", universe.Hash)

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("database: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return fmt.Errorf("database ping: %w", err)
	}

	// Capture-startup invariant (feat/momentum-universe-identity-
	// foundation-v1, mirrors cmd/momentumcapture's own identical addition):
	// fetch venue catalog -> normalize/validate -> freeze the subscription
	// universe (universe.Hash/CapturedAt above -- needed as part of the
	// snapshot's own key, so it is computed before persisting) -> persist
	// that frozen snapshot atomically -> start capture. This process must
	// never subscribe to a universe with no matching identity catalog
	// durably recorded. See momentumcapture.PersistCaptureStartupSnapshot's
	// own doc comment (shared with cmd/momentumcapture, not duplicated
	// here).
	if err := momentumcapture.PersistCaptureStartupSnapshot(
		ctx, pool, exchange, universe.Hash, catalog.Instruments, universe.CapturedAt,
	); err != nil {
		return err
	}

	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	defer func() {
		if err := rdb.Close(); err != nil {
			slog.Warn("momentumcapturebinance.redis.close_failed", "err", err)
		}
	}()
	if err := rdb.Ping(ctx).Err(); err != nil {
		return fmt.Errorf("redis: %w", err)
	}
	healthStore, err := momentumcapture.NewRedisStore(rdb)
	if err != nil {
		return fmt.Errorf("health store: %w", err)
	}

	writer := momentumcapture.NewWriter(pool, exchange, marketType, universe.Hash)
	defer writer.Close()

	now := time.Now()
	oiLastSeenAt := make(map[string]time.Time, universe.Count())
	for _, symbol := range universe.Symbols {
		// Same "clock starts now" baseline as cmd/momentumcapture's own
		// tickerLastSeenAt: without this, checkOpenInterestGaps would flag
		// the entire universe interrupted before the first poll cycle
		// even has a chance to reach every symbol once.
		oiLastSeenAt[symbol] = now
	}

	app := &application{
		// Explicit, not New()'s own implicit default: Binance has no
		// ticker/price feed at all (see this file's own package doc
		// comment), so its OHLC comes from aggTrade prices instead --
		// see momentum.PriceSource's own doc comment for why this
		// choice belongs at construction, said out loud, not inferred.
		engine:                   momentum.NewWithPriceSource(momentum.PriceSourceAggregateTrade),
		writer:                   writer,
		universe:                 universe,
		readiness:                momentumcapture.NewReadinessTracker(universe),
		source:                   source,
		healthStore:              healthStore,
		tradeEvents:              make(chan binance.PublicTrade, tradeEventBuffer),
		tradeDrops:               make(chan binance.PublicTrade, tradeDropBuffer),
		lifecycleEvents:          make(chan binance.TradeLifecycleEvent, lifecycleEventBuffer),
		openInterestEvents:       make(chan binance.OpenInterestReading, openInterestEventBuffer),
		writerInbox:              make(chan []momentum.Bar, writerInboxBuffer),
		driftResults:             make(chan momentumcapture.DriftReport, driftResultBuffer),
		openInterestLastSeenAt:   oiLastSeenAt,
		openInterestGapMarked:    make(map[string]bool, universe.Count()),
		openInterestGapThreshold: computeOpenInterestGapThreshold(universe.Count(), cfg.OpenInterestScheduler),
		lastDrift:                universe.CheckDrift(universe.Symbols, now),
		catalog:                  catalog.Counts,
	}

	writerDone := make(chan error, 1)
	go runWriter(writer, app.writerInbox, writerDone)

	go runDriftPoller(ctx, source, universe, driftCheckInterval, app.driftResults)

	tradesDone := make(chan struct{})
	go func() {
		defer close(tradesDone)
		if err := source.RunTradesWithLifecycle(ctx, universe.Symbols, app.consumeTrade, app.consumeLifecycle); err != nil {
			slog.Error("momentumcapturebinance.trades.stopped", "err", err)
		}
	}()

	oiDone := make(chan struct{})
	go func() {
		defer close(oiDone)
		if err := source.PollOpenInterest(ctx, universe.Symbols, cfg.OpenInterestScheduler, app.consumeOpenInterest); err != nil {
			slog.Error("momentumcapturebinance.open_interest.stopped", "err", err)
		}
	}()

	slog.Info("momentumcapturebinance.starting", "symbols", universe.Count())
	return app.loop(ctx, tradesDone, oiDone, writerDone)
}

// runWriter is copied verbatim from cmd/momentumcapture: identical
// contract, identical Writer type, nothing venue-specific in it.
func runWriter(writer *momentumcapture.Writer, inbox <-chan []momentum.Bar, done chan<- error) {
	flushTicker := time.NewTicker(writerFlushInterval)
	defer flushTicker.Stop()
	for {
		select {
		case bars, ok := <-inbox:
			if !ok {
				shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
				err := writer.Flush(shutdownCtx)
				cancel()
				done <- err
				return
			}
			if dropped := writer.Enqueue(bars); dropped > 0 {
				slog.Warn("momentumcapturebinance.writer_queue_overflow", "dropped", dropped)
			}
		case now := <-flushTicker.C:
			if !writer.Ready(now) {
				continue
			}
			if err := writer.Flush(context.Background()); err != nil {
				slog.Warn("momentumcapturebinance.writer_flush_failed", "err", err)
			}
		}
	}
}

func runDriftPoller(
	ctx context.Context,
	source *binance.Source,
	universe momentumcapture.Universe,
	interval time.Duration,
	results chan<- momentumcapture.DriftReport,
) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			catalog, err := source.FetchSymbolCatalog(ctx)
			if err != nil {
				slog.Warn("momentumcapturebinance.drift_check_failed", "err", err)
				continue
			}
			drift := universe.CheckDrift(catalog.CryptoPerpetualSymbols, now)
			select {
			case results <- drift:
			default:
				slog.Warn("momentumcapturebinance.drift_result_queue_full")
			}
		}
	}
}

func (app *application) handleDriftResult(drift momentumcapture.DriftReport) {
	app.lastDrift = drift
	if drift.Stale {
		slog.Warn(
			"momentumcapturebinance.universe_drift",
			"added", len(drift.AddedSinceStart),
			"removed", len(drift.RemovedSinceStart),
			"frozen_hash", drift.FrozenHash,
			"live_hash", drift.LiveHash,
		)
	}
}

// consumeTrade mirrors cmd/momentumcapture's own consumeTrade exactly: same
// non-blocking-with-fallback-then-latch contract, called synchronously from
// a trade shard's own read goroutine.
func (app *application) consumeTrade(_ context.Context, trade binance.PublicTrade) error {
	select {
	case app.tradeEvents <- trade:
		return nil
	default:
	}
	select {
	case app.tradeDrops <- trade:
	default:
		app.tradeDropsLost.Add(1)
		app.tradeVisibilityLost.markOnce(time.Now())
	}
	return nil
}

func (app *application) consumeLifecycle(event binance.TradeLifecycleEvent) {
	select {
	case app.lifecycleEvents <- event:
	default:
		app.lifecycleVisibilityLost.markOnce(time.Now())
		slog.Warn("momentumcapturebinance.lifecycle_queue_full", "shard", event.ShardSessionID)
	}
}

// consumeOpenInterest is called from PollOpenInterest's own worker pool --
// as of fix/binance-oi-poll-scheduler-v1, up to cfg.OpenInterestScheduler.Workers
// (default 8) goroutines call this concurrently with each other, not the
// single dedicated goroutine an earlier version of this comment described.
// Safe as written: a channel send and an atomic increment are both
// concurrency-safe on their own, so nothing here needed to change for the
// new concurrency -- but a future edit that adds a plain (non-atomic)
// field write or map access here would introduce a real data race under
// this now-concurrent design, unlike when this really was single-
// producer. The same non-blocking-drop-and-count contract still applies
// so a stalled loop goroutine can never make the poller itself block.
// openInterestDropsLost is atomic (not a counters field) for the same
// reason tradeDropsLost is: this runs on a producer goroutine, never the
// loop goroutine.
func (app *application) consumeOpenInterest(_ context.Context, reading binance.OpenInterestReading) error {
	select {
	case app.openInterestEvents <- reading:
	default:
		app.openInterestDropsLost.Add(1)
		slog.Warn("momentumcapturebinance.open_interest_queue_full", "symbol", reading.Symbol)
	}
	return nil
}

func (app *application) loop(ctx context.Context, tradesDone, oiDone <-chan struct{}, writerDone <-chan error) error {
	flushTicker := time.NewTicker(flushInterval)
	defer flushTicker.Stop()
	healthTicker := time.NewTicker(healthInterval)
	defer healthTicker.Stop()

	for {
		select {
		case <-ctx.Done():
			return app.shutdown(tradesDone, oiDone, writerDone)

		case trade := <-app.tradeEvents:
			app.observeInputQueueDepth()
			app.handleTrade(trade)

		case trade := <-app.tradeDrops:
			app.observeInputQueueDepth()
			app.handleTradeDrop(trade)

		case event := <-app.lifecycleEvents:
			app.observeInputQueueDepth()
			app.handleLifecycle(event)

		case reading := <-app.openInterestEvents:
			app.observeInputQueueDepth()
			app.handleOpenInterest(reading)

		case now := <-flushTicker.C:
			started := time.Now()
			app.consumeLossLatches()
			app.checkOpenInterestGaps(now)
			app.enqueue(app.engine.Flush(now))
			app.stats.flush.observe(time.Since(started))

		case drift := <-app.driftResults:
			app.handleDriftResult(drift)

		case <-healthTicker.C:
			app.logHealth(ctx)
		}
	}
}

// shutdown mirrors cmd/momentumcapture's own shutdown sequence (stop
// producers, drain, finalize; order matters throughout), simplified by
// having no NATS subscription to unsubscribe from and no ticker-message
// channel with its own never-closes caveat. oiDone additionally gates the
// drain here: unlike the trade WS layer (whose 60s read timeout can make it
// slow to notice ctx cancellation), PollOpenInterest returns almost
// immediately once ctx is done, so waiting for it costs virtually nothing
// and keeps this function's own "every producer confirmed stopped before
// draining is final" invariant exact, not approximate.
func (app *application) shutdown(tradesDone, oiDone <-chan struct{}, writerDone <-chan error) error {
	deadline := time.Now().Add(shutdownTimeout)
	tradesStopped := false
	oiStopped := false

drain:
	for {
		select {
		case trade := <-app.tradeEvents:
			app.observeInputQueueDepth()
			app.handleTrade(trade)
		case trade := <-app.tradeDrops:
			app.observeInputQueueDepth()
			app.handleTradeDrop(trade)
		case event := <-app.lifecycleEvents:
			app.observeInputQueueDepth()
			app.handleLifecycle(event)
		case reading := <-app.openInterestEvents:
			app.observeInputQueueDepth()
			app.handleOpenInterest(reading)
		case <-tradesDone:
			tradesStopped = true
			tradesDone = nil
		case <-oiDone:
			oiStopped = true
			oiDone = nil
		default:
			if tradesStopped && oiStopped {
				break drain
			}
			if time.Now().After(deadline) {
				slog.Warn("momentumcapturebinance.shutdown_drain_timed_out", "trades_stopped", tradesStopped, "oi_stopped", oiStopped)
				break drain
			}
			time.Sleep(5 * time.Millisecond)
		}
	}

	app.consumeLossLatches()
	flushStarted := time.Now()
	final := app.engine.Flush(time.Now())
	app.stats.flush.observe(time.Since(flushStarted))
	app.recordBarStats(final)
	if len(final) > 0 {
		select {
		case app.writerInbox <- final:
		case <-time.After(shutdownTimeout):
			slog.Error("momentumcapturebinance.writer_inbox_full_at_shutdown", "dropped", len(final))
		}
	}
	close(app.writerInbox)

	select {
	case err := <-writerDone:
		if err != nil {
			return fmt.Errorf("final writer flush: %w", err)
		}
		return nil
	case <-time.After(shutdownTimeout):
		return errors.New("writer goroutine did not confirm its final flush in time")
	}
}

// consumeLossLatches mirrors cmd/momentumcapture's own, minus the NATS
// fault latch this process has no equivalent producer for.
func (app *application) consumeLossLatches() {
	if at, ok := app.tradeVisibilityLost.consume(); ok {
		app.markTradesFeedInterrupted(at, "trade_drop_channel_exhausted")
	}
	if at, ok := app.lifecycleVisibilityLost.consume(); ok {
		app.markTradesFeedInterrupted(at, "lifecycle_queue_full")
	}
}

func (app *application) enqueue(bars []momentum.Bar) {
	if len(bars) == 0 {
		return
	}
	select {
	case app.writerInbox <- bars:
	default:
		app.stats.writerInboxDropsTotal += uint64(len(bars))
		slog.Warn("momentumcapturebinance.writer_inbox_full", "dropped", len(bars))
	}
	app.recordBarStats(bars)
}

func (app *application) recordBarStats(bars []momentum.Bar) {
	if len(bars) == 0 {
		return
	}
	app.stats.barsCompletedTotal += uint64(len(bars))
	for _, bar := range bars {
		app.stats.lateEventsTotal += uint64(bar.LateTradesDropped)
		if bar.BucketStart.After(app.stats.lastBarAt) {
			app.stats.lastBarAt = bar.BucketStart
		}
		if bar.TradeLagMaxMs > app.stats.tradeLagMaxMs {
			app.stats.tradeLagMaxMs = bar.TradeLagMaxMs
		}
		if bar.TickerLagMaxMs > app.stats.tickerLagMaxMs {
			app.stats.tickerLagMaxMs = bar.TickerLagMaxMs
		}
	}
}

// handleTrade mirrors cmd/momentumcapture's own handleTrade. Two fields
// momentum.Trade accepts have no Binance equivalent and are set to their
// honest zero/false rather than a guessed value:
//
//   - Seq is left 0 ("unavailable", per momentum.Trade's own doc comment).
//     Binance's aggTrade id IS documented as monotonically increasing per
//     symbol, but also documented as NOT gap-free (100ms aggregation can
//     skip ids) -- using it for Seq's own "bounded regression counter"
//     role would need a live-verified understanding of what a regression
//     actually means for this id space, which is exactly the "aggTrade id
//     contiguity" question docs/research/binance-momentum-source-v1.md
//     left for this PR's own live probe (see docs/research/binance-
//     momentum-capture-v1.md) rather than guessed at here.
//   - IsBlockTrade/IsRPI stay false always: Binance's combined-stream
//     aggTrade carries no equivalent classification at all, not a "no" to
//     a question Bybit asks. Every Binance trade will always show
//     BlockTradeCount=0/RPITradeCount=0 in its own bar -- read that as
//     "not applicable to this venue", not "no block/RPI trades occurred".
func (app *application) handleTrade(trade binance.PublicTrade) {
	started := time.Now()
	defer func() { app.stats.tradeHandler.observe(time.Since(started)) }()
	if !trade.ReceivedAt.IsZero() && !trade.ReceivedAt.After(started) {
		app.stats.tradeReceiveToHandle.observe(started.Sub(trade.ReceivedAt))
	}
	side := momentum.SideBuy
	if trade.Side == "sell" {
		side = momentum.SideSell
	}
	bars, err := app.engine.AddTrade(momentum.Trade{
		Symbol:     trade.Symbol,
		Side:       side,
		Price:      trade.Price,
		Size:       trade.Size,
		EventAt:    trade.EventAt,
		ReceivedAt: trade.ReceivedAt,
		TradeID:    trade.AggTradeID,
	})
	if err != nil {
		app.stats.tradesInvalidTotal++
		return
	}
	app.stats.tradesAcceptedTotal++
	app.readiness.ObserveTrade(trade.Symbol)
	app.enqueue(bars)
}

func (app *application) handleTradeDrop(trade binance.PublicTrade) {
	app.stats.tradeDropsTotal++
	app.enqueue(app.engine.MarkTradesDiscontinuity(trade.Symbol, trade.EventAt))
}

// handleLifecycle mirrors cmd/momentumcapture's own handleLifecycle, plus
// tallies tradeReconnectTotal/tradeReadTimeoutTotal itself (see counters'
// own doc comment on why this process cannot read them from a Source.
// StreamStats() the way cmd/momentumcapture does).
func (app *application) handleLifecycle(event binance.TradeLifecycleEvent) {
	app.stats.tradeLifecycleTotal++
	if event.DisconnectedAt.IsZero() {
		return // a "connected" event: nothing to mark unhealthy
	}
	app.stats.tradeReconnectTotal++
	if event.ReadTimeout {
		app.stats.tradeReadTimeoutTotal++
	}
	for _, symbol := range event.Symbols {
		app.enqueue(app.engine.MarkTradesDiscontinuity(symbol, event.DisconnectedAt))
	}
	app.stats.lastDiscontinuityAt = event.DisconnectedAt
	if len(event.Symbols) == 1 {
		app.stats.lastDiscontinuityFor = event.Symbols[0]
	} else {
		app.stats.lastDiscontinuityFor = "*" // whole shard, not the whole universe
	}
}

// markTradesFeedInterrupted mirrors cmd/momentumcapture's own: used only
// when visibility into which symbol lost its trade feed was itself lost.
func (app *application) markTradesFeedInterrupted(at time.Time, reason string) {
	for _, symbol := range app.universe.Symbols {
		app.enqueue(app.engine.MarkTradesDiscontinuity(symbol, at))
	}
	app.stats.lastDiscontinuityAt = at
	app.stats.lastDiscontinuityFor = "*"
	slog.Warn("momentumcapturebinance.trades_feed_interrupted", "reason", reason)
}

// handleOpenInterest is this process's only source of AddTickerObservation
// calls: LastPrice/BidPrice/AskPrice are always nil (see this file's own
// package doc comment on what that means for OHLC), only OpenInterest and
// its own EventAt/ObservedAt pair are ever populated. OpenInterestValue
// stays nil always too: binance.OpenInterestReading itself has no value
// field (see internal/binance/openinterest.go's own doc comment on why,
// tracing to the capability preflight).
func (app *application) handleOpenInterest(reading binance.OpenInterestReading) {
	started := time.Now()
	defer func() { app.stats.openInterestHandler.observe(time.Since(started)) }()
	if !reading.ObservedAt.IsZero() && !reading.ObservedAt.After(started) {
		app.stats.openInterestReceiveToHandle.observe(started.Sub(reading.ObservedAt))
	}

	// Mirrors cmd/momentumcapture's own optionalNonNegativeFloat exactly:
	// strconv.ParseFloat accepts "NaN"/"Inf"/"+Inf" without error, so an
	// err == nil check alone is not enough. This matters more here than it
	// would elsewhere: checkOpenInterestGaps is this feed's ONLY
	// discontinuity detector (see its own doc comment), and the gap-mark
	// clear/lastSeenAt update below runs on every call that reaches past
	// this guard -- a recurring reading that parses but is not a real
	// finite number would silently satisfy the only alarm this feed has,
	// while zero real OI data ever reaches the engine.
	amount, err := strconv.ParseFloat(reading.Amount, 64)
	if err != nil || math.IsNaN(amount) || math.IsInf(amount, 0) || amount < 0 {
		app.stats.openInterestInvalidTotal++
		return
	}
	if !app.universe.Contains(reading.Symbol) {
		// Defensive only: PollOpenInterest is only ever started with this
		// process's own frozen universe.Symbols, so this should never
		// trigger, but silently trusting an out-of-scope symbol into the
		// engine would violate the same frozen-universe contract
		// cmd/momentumcapture enforces for its own out-of-process ticker
		// feed. Counted separately from openInterestInvalidTotal (see
		// openInterestOutOfScopeTotal's own doc comment): this is a
		// universe/catalog-drift signal, not a malformed-payload one.
		app.stats.openInterestOutOfScopeTotal++
		return
	}

	delete(app.openInterestGapMarked, reading.Symbol) // a fresh observation ends any open gap
	app.openInterestLastSeenAt[reading.Symbol] = time.Now()

	bars, err := app.engine.AddTickerObservation(momentum.TickerObservation{
		Symbol:                 reading.Symbol,
		OpenInterest:           &amount,
		OpenInterestEventAt:    &reading.EventAt,
		OpenInterestObservedAt: &reading.ObservedAt,
		EventAt:                reading.EventAt,
		ObservedAt:             reading.ObservedAt,
	})
	if err != nil {
		app.stats.openInterestInvalidTotal++
		return
	}
	app.stats.openInterestAcceptedTotal++
	app.readiness.ObserveTicker(reading.Symbol)
	app.enqueue(bars)
}

// computeOpenInterestGapThreshold combines openInterestExpectedCycleDuration
// with openInterestGapThresholdMultiple and openInterestGapThresholdFloor
// into the actual threshold checkOpenInterestGaps uses -- the one place
// both run() and this package's own tests (newTestApplication) compute it,
// so the two can never quietly drift apart.
func computeOpenInterestGapThreshold(universeSize int, cfg binance.OpenInterestSchedulerConfig) time.Duration {
	threshold := openInterestGapThresholdMultiple * openInterestExpectedCycleDuration(universeSize, cfg)
	if threshold < openInterestGapThresholdFloor {
		return openInterestGapThresholdFloor
	}
	return threshold
}

// openInterestExpectedCycleDuration is how long one full round of every
// symbol in universeSize should take under cfg's own rate limit: the
// scheduler's real, budget-driven cadence, computed from the actual
// numbers this run started with instead of a single hardcoded interval
// that stops being true the moment the universe size or configured rate
// changes. Returns 0 for a non-positive input (never used as a gap
// threshold's own basis when the run has no symbols at all).
func openInterestExpectedCycleDuration(universeSize int, cfg binance.OpenInterestSchedulerConfig) time.Duration {
	if universeSize <= 0 || cfg.RateLimitPerMinute <= 0 {
		return 0
	}
	return time.Duration(universeSize) * time.Minute / time.Duration(cfg.RateLimitPerMinute)
}

// checkOpenInterestGaps is the direct analog of cmd/momentumcapture's own
// checkTickerGaps, and here it is the ONLY discontinuity-detection
// mechanism for this feed (see application.openInterestGapThreshold's own
// doc comment).
func (app *application) checkOpenInterestGaps(now time.Time) {
	for _, symbol := range app.universe.Symbols {
		if app.openInterestGapMarked[symbol] {
			continue
		}
		lastSeen, ok := app.openInterestLastSeenAt[symbol]
		if !ok || now.Sub(lastSeen) < app.openInterestGapThreshold {
			continue
		}
		app.stats.openInterestGapTotal++
		app.openInterestGapMarked[symbol] = true
		app.enqueue(app.engine.MarkTickerDiscontinuity(symbol, now))
	}
}

func (app *application) logHealth(ctx context.Context) {
	now := time.Now()
	inputDepth := app.observeInputQueueDepth()

	health := momentumcapture.BuildUniverseHealth(app.universe, app.lastDrift, app.readiness, now)
	health.Exchange = exchange
	health.CatalogItemsTotal = app.catalog.CatalogItemsTotal
	health.CryptoPerpetualsIncluded = app.catalog.CryptoPerpetualsIncluded
	health.InvalidInstrumentExcluded = app.catalog.InvalidInstrumentExcluded
	health.NonUSDTExcluded = app.catalog.NonUSDTExcluded
	health.NonTradingExcluded = app.catalog.NonTradingExcluded
	// StandardCryptoIncluded/InnovationCryptoIncluded/DatedFuturesExcluded/
	// StockPerpetualsExcluded/CommodityPerpetualsExcluded/
	// UnknownContractExcluded/UnknownSymbolTypeExcluded stay zero: Bybit's
	// own finer-grained taxonomy, not applicable here (see Health's own
	// doc comment). Binance's remaining exclusion reasons, which have no
	// Bybit-shaped field, go into ExclusionCounts using the exact same
	// keys binance.translateUniverse already uses.
	health.ExclusionCounts = map[string]int{
		"non_perpetual_contract":  app.catalog.NonPerpetualContractExcluded,
		"underlying_index":        app.catalog.UnderlyingIndexExcluded,
		"unknown_underlying_type": app.catalog.UnknownUnderlyingTypeExcluded,
	}
	health = momentumcapture.ApplyWriterStats(health, app.writer.Stats())
	health.StartedAt = app.universe.CapturedAt
	health.UpdatedAt = now
	health.LastBarAt = app.stats.lastBarAt
	health.BarsCompletedTotal = app.stats.barsCompletedTotal
	health.LateEventsTotal = app.stats.lateEventsTotal
	// TickerGapTotal doubles as this process's own OI-gap counter: same
	// underlying concept (a per-symbol feed silence detection), see
	// checkOpenInterestGaps.
	health.TickerGapTotal = app.stats.openInterestGapTotal
	health.LastDiscontinuityAt = app.stats.lastDiscontinuityAt
	health.LastDiscontinuityFor = app.stats.lastDiscontinuityFor
	health.TradeReconnectTotal = app.stats.tradeReconnectTotal
	health.TradeReadTimeoutTotal = app.stats.tradeReadTimeoutTotal
	// NATSDisconnectTotal/NATSReconnectTotal/NATSSlowConsumerTotal/
	// NATSDroppedTotal stay zero always: this process has no NATS
	// dependency at all, unlike cmd/momentumcapture.
	health.InputQueueDepth = inputDepth
	health.InputQueuePeak = app.stats.inputQueuePeak
	health.InputQueueDropsTotal = app.stats.tradeDropsTotal + app.stats.writerInboxDropsTotal +
		app.tradeDropsLost.Load() + app.openInterestDropsLost.Load()
	health.TradeLagMaxMs = app.stats.tradeLagMaxMs
	health.TickerLagMaxMs = app.stats.tickerLagMaxMs
	applyLatencyHealth(&health, &app.stats)
	health.ProjectedBytesPerDay = float64(app.universe.Count()) * 1440 * estimatedHotBytesPerRow
	health.Status = deriveHealthStatus(health)

	storeCtx, cancel := context.WithTimeout(ctx, 2*time.Second) // matches cmd/momentumcapture's own bound
	defer cancel()
	publishStarted := time.Now()
	if err := app.healthStore.StoreHealth(storeCtx, health); err != nil {
		slog.Warn("momentumcapturebinance.health_store_failed", "err", err)
	}
	app.stats.healthPublish.observe(time.Since(publishStarted))
	slog.Info(
		"momentumcapturebinance.health",
		"status", health.Status,
		"ready_symbols", health.ReadySymbols,
		"subscribed_symbols", health.SubscribedSymbols,
		"bars_completed_total", health.BarsCompletedTotal,
		"bars_persisted_total", health.BarsPersistedTotal,
		"writer_queue_depth", health.WriterQueueDepth,
		"persist_errors_total", health.PersistErrorsTotal,
		"payload_hash_mismatch_total", health.PayloadHashMismatchTotal,
		"open_interest_out_of_scope_total", app.stats.openInterestOutOfScopeTotal,
	)
}

func (app *application) observeInputQueueDepth() int {
	depth := len(app.tradeEvents) + len(app.tradeDrops) + len(app.lifecycleEvents) + len(app.openInterestEvents)
	if depth > app.stats.inputQueuePeak {
		app.stats.inputQueuePeak = depth
	}
	return depth
}

func applyLatencyHealth(health *momentumcapture.Health, stats *counters) {
	tradeWait := stats.tradeReceiveToHandle.summary()
	health.TradeReceiveToHandleCount = stats.tradeReceiveToHandle.count
	health.TradeReceiveToHandleP95Us = durationMicroseconds(tradeWait.P95)
	health.TradeReceiveToHandleP99Us = durationMicroseconds(tradeWait.P99)
	health.TradeReceiveToHandleMaxUs = durationMicroseconds(tradeWait.Max)

	tradeHandler := stats.tradeHandler.summary()
	health.TradeHandlerCount = stats.tradeHandler.count
	health.TradeHandlerP95Us = durationMicroseconds(tradeHandler.P95)
	health.TradeHandlerP99Us = durationMicroseconds(tradeHandler.P99)
	health.TradeHandlerMaxUs = durationMicroseconds(tradeHandler.Max)

	// TickerReceiveToHandle*/TickerHandler* double as this process's own
	// OI-side queue-wait/handler latency, same pairing as TickerGapTotal
	// above.
	oiWait := stats.openInterestReceiveToHandle.summary()
	health.TickerReceiveToHandleCount = stats.openInterestReceiveToHandle.count
	health.TickerReceiveToHandleP95Us = durationMicroseconds(oiWait.P95)
	health.TickerReceiveToHandleP99Us = durationMicroseconds(oiWait.P99)
	health.TickerReceiveToHandleMaxUs = durationMicroseconds(oiWait.Max)

	oiHandler := stats.openInterestHandler.summary()
	health.TickerHandlerCount = stats.openInterestHandler.count
	health.TickerHandlerP95Us = durationMicroseconds(oiHandler.P95)
	health.TickerHandlerP99Us = durationMicroseconds(oiHandler.P99)
	health.TickerHandlerMaxUs = durationMicroseconds(oiHandler.Max)

	flush := stats.flush.summary()
	health.FlushCount = stats.flush.count
	health.FlushP95Us = durationMicroseconds(flush.P95)
	health.FlushP99Us = durationMicroseconds(flush.P99)
	health.FlushMaxUs = durationMicroseconds(flush.Max)

	healthPublish := stats.healthPublish.summary()
	health.HealthPublishCount = stats.healthPublish.count
	health.HealthPublishP95Us = durationMicroseconds(healthPublish.P95)
	health.HealthPublishP99Us = durationMicroseconds(healthPublish.P99)
	health.HealthPublishMaxUs = durationMicroseconds(healthPublish.Max)
}

// deriveHealthStatus is copied verbatim from cmd/momentumcapture (not
// shared): both operate on nothing but momentumcapture.Health fields, but
// keeping each capture binary's own thresholds independently editable
// matters once Binance's real operating characteristics (coarser OI
// cadence, no NATS layer) turn out to need different tuning -- exactly the
// same "documented duplication over a premature shared abstraction"
// decision as latency.go in this package.
func deriveHealthStatus(health momentumcapture.Health) string {
	switch {
	case health.PersistErrorsTotal > 0:
		return "degraded_persist_errors"
	case health.PayloadHashMismatchTotal > 0:
		return "degraded_payload_hash_mismatch"
	case health.InputQueueDropsTotal > 0 || health.WriterQueueDropsTotal > 0 ||
		float64(health.WriterQueueDepth) > float64(momentumcapture.MaxPendingBars)*queuePressureWarnFraction:
		return "degraded_queue_pressure"
	case health.NATSDisconnectTotal > 0 || health.NATSSlowConsumerTotal > 0 ||
		health.NATSDroppedTotal > 0 || health.TickerGapTotal > 0:
		return "degraded_feed_interrupted"
	case health.UniverseStale:
		return "degraded_universe_stale"
	default:
		return "ok"
	}
}

func configureLogging() {
	level := slog.LevelInfo
	if strings.EqualFold(os.Getenv("LOG_LEVEL"), "debug") {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level})))
}

func loadConfig() config {
	defaultScheduler := binance.DefaultOpenInterestSchedulerConfig()
	return config{
		DatabaseURL: envString("DATABASE_URL", "postgres://schurfer:schurfer_dev@localhost:5432/schurfer"),
		RedisAddr:   envString("REDIS_ADDR", "localhost:6379"),
		OpenInterestScheduler: binance.OpenInterestSchedulerConfig{
			Workers:            envInt("OI_POLL_WORKERS", defaultScheduler.Workers),
			RateLimitPerMinute: envInt("OI_POLL_RATE_LIMIT_PER_MINUTE", defaultScheduler.RateLimitPerMinute),
		},
	}
}

func envString(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func envInt(key string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		slog.Warn("momentumcapturebinance.invalid_env_int", "key", key, "value", raw, "fallback", fallback)
		return fallback
	}
	return value
}

// Command momentumcapture runs the Bybit early-momentum-capture line
// (ROADMAP "Active course" item 5): a frozen universe of linear-USDT
// symbols, trades pulled directly from Bybit's public WebSocket, ticker/OI
// consumed from the same NATS feed apps/collector/cmd/collector already
// publishes, aggregated by momentum.Engine and persisted by
// momentumcapture.Writer.
//
// momentum.Engine is owned exclusively by app.loop's own goroutine: trade
// shards, the ticker NATS subscription, and lifecycle/disconnect callbacks
// all feed bounded channels instead of calling the engine directly, since
// momentum.Engine (like momentum-capture's other pure types) is not safe
// for concurrent access. Database writes and the REST drift poll run on
// their own dedicated goroutines (runWriter, runDriftPoller): both can
// block for real seconds (a slow database, Bybit's own retry-with-backoff),
// and blocking the engine-owning loop on either would let the live trade
// firehose overrun its own input channels while the loop waits.
package main

import (
	"context"
	"encoding/json"
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
	"github.com/mavlevich/schurfer/collector/internal/bybit"
	"github.com/mavlevich/schurfer/collector/internal/momentum"
	"github.com/mavlevich/schurfer/collector/internal/momentumcapture"
	"github.com/nats-io/nats.go"
	"github.com/redis/go-redis/v9"
)

const (
	tickerNATSSubject = "market.bybit.ticker.*"
	exchange          = "bybit"
	marketType        = "linear"

	tradeEventBuffer     = 8192
	tradeDropBuffer      = 256
	tickerEventBuffer    = 8192
	lifecycleEventBuffer = 64
	natsFaultBuffer      = 16
	writerInboxBuffer    = 64
	driftResultBuffer    = 2

	// flushInterval drives momentum.Engine.Flush: how often a symbol whose
	// minute has fully elapsed gets force-closed even with zero new
	// events, which is what lets a genuinely quiet-but-healthy minute
	// still produce a bar (see momentum.Bar's own doc comment).
	flushInterval = 5 * time.Second
	// driftCheckInterval re-fetches the live symbol catalog to compare
	// against the frozen universe (see Universe's doc comment on the v1
	// frozen-universe boundary). Independent of flushInterval: drift
	// detection is a slow, low-cost REST poll, not per-minute work.
	driftCheckInterval = 5 * time.Minute
	healthInterval     = 5 * time.Second
	// writerFlushInterval is the dedicated writer goroutine's own flush
	// cadence, independent of flushInterval: the writer goroutine decides
	// when it writes, the engine-owning loop never calls into it directly.
	writerFlushInterval = 5 * time.Second
	// shutdownTimeout bounds the ENTIRE shutdown sequence (waiting for
	// producers to confirm stopped, draining, the final writer flush).
	// The trade WS layer's own read timeout is 60s (3x its 20s ping
	// interval, see internal/bybit/ws.go): a shard blocked in a live
	// Read() call does not observe ctx cancellation until that read
	// unblocks, so a fully synchronized shutdown can genuinely need close
	// to that long in the worst case. 75s is picked to sit comfortably
	// above it; infra/docker's own stop_grace_period must stay above this
	// or Docker will SIGKILL before this budget is used.
	shutdownTimeout     = 75 * time.Second
	maxTickerFutureSkew = 5 * time.Second

	// tickerGapThreshold proactively marks a symbol's ticker feed
	// interrupted after this long without any observation, rather than
	// waiting for the NEXT message to reveal a reconnect happened via a
	// StreamSessionID change. That retroactive detection alone is
	// unbounded: any bar engine.Flush force-closes during the gap would
	// otherwise keep looking complete until whenever the next message
	// happens to arrive. cmd/collector's own ticker WS read-timeout is 3x
	// its 20s ping interval = 60s (see internal/bybit/ws.go) for the WHOLE
	// connection, but a single quiet SYMBOL on an otherwise-healthy
	// connection has no such bound at all. 180s is a deliberately
	// conservative multiple of that 60s connection-level bound, picked
	// without a real measured per-symbol ticker inter-arrival distribution.
	//
	// Known residual gap, not fully closable by this threshold alone: a
	// real outage lasting roughly 60-170s that happens to straddle a
	// minute boundary can close as "complete" via engine.Flush before this
	// threshold ever fires, and StreamSessionID will only reveal it
	// retroactively once reconnected. Closing this fully needs either a
	// proactive lifecycle/control event from the ticker collector itself
	// (a cross-process protocol change, out of scope here) or a delayed
	// finalization / incident-overlay design (bars not considered settled
	// until some grace window after their own minute closes). Revisit
	// after the canary has real per-symbol inter-arrival data.
	tickerGapThreshold = 180 * time.Second

	// estimatedHotBytesPerRow is packages/journal/migrations/versions/
	// 0024_bybit_momentum_bars_1m.py's own measured hot (uncompressed)
	// bytes/row, from a real capture against the live universe, not a
	// guess. Backs ProjectedBytesPerDay; revisit both together.
	estimatedHotBytesPerRow = 1143.6
	// queuePressureWarnFraction flags degraded_queue_pressure once the
	// writer's own backlog crosses this fraction of its bound
	// (momentumcapture.MaxPendingBars): comfortably before Enqueue itself
	// would start dropping the oldest bars.
	queuePressureWarnFraction = 0.5
)

type config struct {
	NATSURL     string
	DatabaseURL string
	RedisAddr   string
}

type counters struct {
	tradesAcceptedTotal uint64
	tradesInvalidTotal  uint64
	// tradeDropsTotal counts trades routed through tradeDrops and turned
	// into a real MarkTradesDiscontinuity call, not just tallied: see
	// consumeTrade and handleTradeDrop.
	tradeDropsTotal        uint64
	tickersAcceptedTotal   uint64
	tickersInvalidTotal    uint64
	tickersOutOfScopeTotal uint64 // symbols on the NATS wildcard outside this process's own frozen universe
	natsDroppedTotal       uint64
	tickerReconnectTotal   uint64
	tickerGapTotal         uint64 // proactive per-symbol silence detections, see tickerGapThreshold
	tradeLifecycleTotal    uint64
	natsDisconnectTotal    uint64
	natsReconnectTotal     uint64
	natsSlowConsumerTotal  uint64
	barsCompletedTotal     uint64
	lateEventsTotal        uint64
	writerInboxDropsTotal  uint64
	inputQueuePeak         int
	lastDiscontinuityAt    time.Time
	lastDiscontinuityFor   string
	lastBarAt              time.Time
	tradeLagMaxMs          int64
	tickerLagMaxMs         int64
	tradeReceiveToHandle   latencyHistogram
	tradeHandler           latencyHistogram
	tickerReceiveToHandle  latencyHistogram
	tickerHandler          latencyHistogram
	flush                  latencyHistogram
	healthPublish          latencyHistogram
}

type application struct {
	engine      *momentum.Engine
	writer      *momentumcapture.Writer
	universe    momentumcapture.Universe
	readiness   *momentumcapture.ReadinessTracker
	source      *bybit.Source
	healthStore *momentumcapture.RedisStore

	tickerSub  *nats.Subscription
	tickerMsgs <-chan *nats.Msg

	tradeEvents     chan bybit.PublicTrade
	tradeDrops      chan bybit.PublicTrade
	lifecycleEvents chan bybit.TradeLifecycleEvent
	natsFaults      chan natsFault

	writerInbox  chan []momentum.Bar
	driftResults chan momentumcapture.DriftReport

	tickerSessionBySymbol map[string]string
	tickerLastSeenAt      map[string]time.Time
	tickerGapMarked       map[string]bool

	// lastDrift is the most recent REAL drift check from runDriftPoller,
	// owned exclusively by the loop goroutine (only it ever assigns to
	// this field, on receiving from driftResults). logHealth reads it
	// directly rather than recomputing a comparison of app.universe
	// against itself, which could never show drift regardless of what the
	// live exchange catalog actually looks like.
	lastDrift momentumcapture.DriftReport
	catalog   bybit.SymbolCatalogCounts

	// tradeDropsLost is the absolute last resort: incremented from a trade
	// shard's own goroutine when even tradeDrops (itself already a
	// fallback for a full tradeEvents) is full. Every other counter in
	// counters is only ever touched from the loop goroutine; this one
	// genuinely cannot be, so it alone needs to be atomic.
	tradeDropsLost atomic.Uint64

	// {trade,lifecycle,natsFault}VisibilityLost latch the earliest moment
	// a producer's own goroutine had nowhere left to put an event (see
	// lossLatch's own doc comment for why these are latches, not plain
	// counters, and consumeLossLatches for how the loop goroutine turns
	// them into a real, conservative discontinuity mark).
	tradeVisibilityLost     lossLatch
	lifecycleVisibilityLost lossLatch
	natsFaultVisibilityLost lossLatch

	stats counters
}

// lossLatch records the earliest time visibility was lost on some channel
// that has no room left to signal it any other way: it is set from a
// producer's own goroutine (which must not block or touch app.stats
// directly, see consumeTrade/consumeLifecycle/pushNATSFault's final
// fallback branches) and consumed from the loop goroutine, at least once
// per flushTicker tick and once more during shutdown, so any such loss
// becomes a real MarkDiscontinuity call within one tick instead of
// silently leaving affected bars looking complete.
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

// natsFault is a feed-wide event (unlike TradeLifecycleEvent, which is
// always scoped to one shard's own symbols): a NATS-level disconnect or
// slow-consumer condition means momentum-capture cannot know which
// symbols' ticker updates were actually lost, so it conservatively marks
// the entire universe's ticker feed interrupted.
type natsFault struct {
	kind string // "disconnected" | "reconnected" | "slow_consumer"
	at   time.Time
	err  error
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

	source := bybit.NewSource()
	catalog, err := source.FetchSymbolCatalog(ctx)
	if err != nil {
		return fmt.Errorf("fetch initial universe: %w", err)
	}
	universe := momentumcapture.NewUniverse(catalog.CryptoPerpetualSymbols, time.Now())
	slog.Info("momentumcapture.universe_frozen", "symbols", universe.Count(), "hash", universe.Hash)

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("database: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return fmt.Errorf("database ping: %w", err)
	}

	// Capture-startup invariant (feat/momentum-universe-identity-
	// foundation-v1, a colleague review's own explicit recommendation):
	// fetch venue catalog -> normalize/validate -> freeze the subscription
	// universe (universe.Hash/CapturedAt above -- needed as part of the
	// snapshot's own key, so it is computed before persisting) -> persist
	// that frozen snapshot atomically -> start capture. This process must
	// never subscribe to a universe that has no matching identity catalog
	// durably recorded -- a snapshot write failure aborts startup here,
	// before any trade/ticker stream opens, rather than capturing bars a
	// later cross-venue resolution step could never reliably attribute.
	// See momentumcapture.PersistCaptureStartupSnapshot's own doc comment
	// (shared with cmd/momentumcapturebinance, not duplicated here).
	if err := momentumcapture.PersistCaptureStartupSnapshot(
		ctx, pool, exchange, universe.Hash, catalog.Instruments, universe.CapturedAt,
	); err != nil {
		return err
	}

	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	defer func() {
		if err := rdb.Close(); err != nil {
			slog.Warn("momentumcapture.redis.close_failed", "err", err)
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
	tickerLastSeenAt := make(map[string]time.Time, universe.Count())
	for _, symbol := range universe.Symbols {
		// A fair "clock starts now" baseline: without this, every symbol
		// would look like it has been silent since the zero time and the
		// very first checkTickerGaps call would immediately flag the
		// entire universe as interrupted before any real message had a
		// chance to arrive.
		tickerLastSeenAt[symbol] = now
	}

	app := &application{
		engine:                momentum.New(),
		writer:                writer,
		universe:              universe,
		readiness:             momentumcapture.NewReadinessTracker(universe),
		source:                source,
		healthStore:           healthStore,
		tradeEvents:           make(chan bybit.PublicTrade, tradeEventBuffer),
		tradeDrops:            make(chan bybit.PublicTrade, tradeDropBuffer),
		lifecycleEvents:       make(chan bybit.TradeLifecycleEvent, lifecycleEventBuffer),
		natsFaults:            make(chan natsFault, natsFaultBuffer),
		writerInbox:           make(chan []momentum.Bar, writerInboxBuffer),
		driftResults:          make(chan momentumcapture.DriftReport, driftResultBuffer),
		tickerSessionBySymbol: make(map[string]string, universe.Count()),
		tickerLastSeenAt:      tickerLastSeenAt,
		tickerGapMarked:       make(map[string]bool, universe.Count()),
		// An initial real drift snapshot (comparing the frozen universe
		// against itself, so Stale is correctly false) means health never
		// reports a misleadingly empty placeholder for the 5 minutes
		// before runDriftPoller's first real REST check completes.
		lastDrift: universe.CheckDrift(universe.Symbols, now),
		catalog:   catalog.Counts,
	}

	nc, err := nats.Connect(
		cfg.NATSURL,
		nats.MaxReconnects(-1),
		nats.ReconnectWait(2*time.Second),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			app.pushNATSFault(natsFault{kind: "disconnected", at: time.Now(), err: err})
		}),
		nats.ReconnectHandler(func(_ *nats.Conn) {
			app.pushNATSFault(natsFault{kind: "reconnected", at: time.Now()})
		}),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) {
			if errors.Is(err, nats.ErrSlowConsumer) {
				app.pushNATSFault(natsFault{kind: "slow_consumer", at: time.Now(), err: err})
				return
			}
			slog.Warn("momentumcapture.nats.async_error", "err", err)
		}),
	)
	if err != nil {
		return fmt.Errorf("nats: %w", err)
	}
	defer nc.Close()

	tickerMsgs := make(chan *nats.Msg, tickerEventBuffer)
	sub, err := nc.ChanSubscribe(tickerNATSSubject, tickerMsgs)
	if err != nil {
		return fmt.Errorf("subscribe %s: %w", tickerNATSSubject, err)
	}
	if err := nc.FlushTimeout(5 * time.Second); err != nil {
		return fmt.Errorf("flush subscription: %w", err)
	}
	app.tickerSub = sub
	app.tickerMsgs = tickerMsgs

	writerDone := make(chan error, 1)
	go runWriter(writer, app.writerInbox, writerDone)

	go runDriftPoller(ctx, source, universe, driftCheckInterval, app.driftResults)

	tradesDone := make(chan struct{})
	go func() {
		defer close(tradesDone)
		if err := source.RunTradesWithLifecycle(ctx, universe.Symbols, app.consumeTrade, app.consumeLifecycle); err != nil {
			slog.Error("momentumcapture.trades.stopped", "err", err)
		}
	}()

	slog.Info("momentumcapture.starting", "symbols", universe.Count(), "nats_subject", tickerNATSSubject)
	return app.loop(ctx, tradesDone, writerDone)
}

// runWriter owns all database I/O for the writer on its own goroutine (see
// the package doc comment on why). inbox delivers closed bars to enqueue.
// Termination is signaled by the loop goroutine CLOSING inbox (not a
// separate signal channel): Go guarantees every value sent before a
// channel close is received before the zero-value/ok=false read that
// follows it, so by the time this loop observes !ok, every bar the loop
// goroutine will ever hand off has already been enqueued here. done
// receives the final flush's own error exactly once, so a failed drain can
// make the process exit non-zero instead of silently reporting success.
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
				slog.Warn("momentumcapture.writer_queue_overflow", "dropped", dropped)
			}
		case now := <-flushTicker.C:
			if !writer.Ready(now) {
				continue
			}
			if err := writer.Flush(context.Background()); err != nil {
				slog.Warn("momentumcapture.writer_flush_failed", "err", err)
			}
		}
	}
}

// runDriftPoller re-fetches the live symbol catalog on its own goroutine:
// FetchSymbolCatalog retries with its own exponential backoff internally and can
// block for real seconds, which must never stall the goroutine that owns
// momentum.Engine. results is a small buffered channel; a result that
// can't be delivered before the next tick is superseded anyway, so it is
// dropped rather than blocking this goroutine on a loop that is busy.
func runDriftPoller(
	ctx context.Context,
	source *bybit.Source,
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
				slog.Warn("momentumcapture.drift_check_failed", "err", err)
				continue
			}
			drift := universe.CheckDrift(catalog.CryptoPerpetualSymbols, now)
			select {
			case results <- drift:
			default:
				slog.Warn("momentumcapture.drift_result_queue_full")
			}
		}
	}
}

// handleDriftResult stores the real drift report from runDriftPoller. This
// is the loop goroutine's only writer to lastDrift, so logHealth (also
// loop-goroutine-only) can read it directly instead of computing a
// comparison of app.universe against itself, which could never show drift
// regardless of what the live exchange catalog actually looks like.
func (app *application) handleDriftResult(drift momentumcapture.DriftReport) {
	app.lastDrift = drift
	if drift.Stale {
		slog.Warn(
			"momentumcapture.universe_drift",
			"added", len(drift.AddedSinceStart),
			"removed", len(drift.RemovedSinceStart),
			"frozen_hash", drift.FrozenHash,
			"live_hash", drift.LiveHash,
		)
	}
}

// consumeTrade is called synchronously from a trade shard's own read
// goroutine (see bybit.TradeFn) and must not block. A full tradeEvents
// falls back to tradeDrops so the loop goroutine can still turn the drop
// into a real MarkTradesDiscontinuity call (see handleTradeDrop); only if
// even that fallback is full does this give up on per-symbol precision and
// latch a whole-feed loss signal instead (see lossLatch, consumeLossLatches).
// tradeDropsLost and the latch are the only state in this file touched
// from a producer's own goroutine; everything else belongs to the loop
// goroutine alone.
func (app *application) consumeTrade(_ context.Context, trade bybit.PublicTrade) error {
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

// consumeLifecycle is called synchronously from a trade shard's own
// goroutine (see bybit.TradeLifecycleFn) with the same non-blocking
// contract as consumeTrade. Losing a lifecycle event specifically means
// losing the one signal that would otherwise mark a shard's own reconnect,
// so a full queue here latches a whole-feed loss the same way consumeTrade
// does, rather than only logging it.
func (app *application) consumeLifecycle(event bybit.TradeLifecycleEvent) {
	select {
	case app.lifecycleEvents <- event:
	default:
		app.lifecycleVisibilityLost.markOnce(time.Now())
		slog.Warn("momentumcapture.lifecycle_queue_full", "shard", event.ShardSessionID)
	}
}

func (app *application) pushNATSFault(fault natsFault) {
	select {
	case app.natsFaults <- fault:
	default:
		app.natsFaultVisibilityLost.markOnce(fault.at)
		slog.Warn("momentumcapture.nats_fault_queue_full", "kind", fault.kind)
	}
}

func (app *application) loop(ctx context.Context, tradesDone <-chan struct{}, writerDone <-chan error) error {
	flushTicker := time.NewTicker(flushInterval)
	defer flushTicker.Stop()
	healthTicker := time.NewTicker(healthInterval)
	defer healthTicker.Stop()

	for {
		select {
		case <-ctx.Done():
			return app.shutdown(tradesDone, writerDone)

		case trade := <-app.tradeEvents:
			app.observeInputQueueDepth()
			app.handleTrade(trade)

		case trade := <-app.tradeDrops:
			app.observeInputQueueDepth()
			app.handleTradeDrop(trade)

		case event := <-app.lifecycleEvents:
			app.observeInputQueueDepth()
			app.handleLifecycle(event)

		case fault := <-app.natsFaults:
			app.observeInputQueueDepth()
			app.handleNATSFault(fault)

		case msg, ok := <-app.tickerMsgs:
			if !ok {
				// nats.go never actually closes a ChanSubscribe channel
				// today (see shutdown's own doc comment for the source
				// dive that confirmed this), so this is not expected to
				// fire in practice; if it ever does, that is itself
				// unexpected enough to treat as fatal rather than paper
				// over silently.
				return errors.New("NATS ticker channel closed")
			}
			app.observeInputQueueDepth()
			app.handleTickerMessage(msg, time.Now())

		case now := <-flushTicker.C:
			started := time.Now()
			// Any loss latched since the last tick, and any newly silent
			// symbol, must be reflected BEFORE Flush closes bars for this
			// tick: closing first and marking after would let exactly the
			// bars this exists to protect look complete for one more
			// cycle.
			app.consumeLossLatches()
			app.checkTickerGaps(now)
			app.enqueue(app.engine.Flush(now))
			app.stats.flush.observe(time.Since(started))

		case drift := <-app.driftResults:
			app.handleDriftResult(drift)

		case <-healthTicker.C:
			app.logHealth(ctx)
		}
	}
}

// shutdown stops producers before draining, then drains, then finalizes;
// order matters throughout.
//
// The ticker side needs a different stop signal than the trade side, and
// this matters: nats.go's ChanSubscribe channel is NEVER closed by the
// client library, not on Unsubscribe, not on Drain, not even on the whole
// Conn closing (confirmed against nats.go v1.42.0's own source: both
// removeSub and Conn.close only call close(s.mch) when s.typ ==
// SyncSubscription; a ChanSubscription's channel is simply abandoned).
// Waiting for tickerMsgs to report ok=false, as an earlier version of this
// function did, waits forever in production and was only ever proven
// wrong by an actual SIGTERM against a running container, not by any
// review or by go test. So "the ticker producer stopped" here means
// exactly one thing: Subscription.Unsubscribe() has returned, which is a
// direct, synchronous call to the server (not the fire-and-forget
// nc.Drain(), which also interacts with this process's own infinite
// auto-reconnect (MaxReconnects(-1)) in ways that were observed, live, to
// leave the connection never actually finishing its drain). Once
// Unsubscribe returns, the server sends nothing further for this subject;
// only whatever was already buffered in tickerMsgs before that point
// remains to drain below, which the loop does without needing to wait for
// anything from the channel itself.
//
// The trade side is different: this process owns tradesDone (it is
// closed by run()'s own goroutine wrapper), so waiting for it to close is
// a real, working "producer confirmed stopped" signal, bounded here by
// shutdownTimeout in case the trade WS layer's own 60s read timeout (see
// internal/bybit/ws.go) makes it slow to notice ctx cancellation.
//
// Only once the trade producer is confirmed stopped (or the deadline
// passes) does draining the input channels become a complete, final
// operation rather than a snapshot a late callback could still add to.
// The final bars go through the SAME writerInbox channel every other bar
// uses, never a direct Writer.Enqueue bypass (which could otherwise
// interleave with an in-flight Flush's own overflow-trim path in the
// writer goroutine); the channel is then closed to signal the writer
// goroutine's own final flush.
func (app *application) shutdown(tradesDone <-chan struct{}, writerDone <-chan error) error {
	if err := app.tickerSub.Unsubscribe(); err != nil {
		slog.Warn("momentumcapture.nats.unsubscribe_failed", "err", err)
	}

	deadline := time.Now().Add(shutdownTimeout)
	tradesStopped := false

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
		case fault := <-app.natsFaults:
			app.observeInputQueueDepth()
			app.handleNATSFault(fault)
		case msg, ok := <-app.tickerMsgs:
			if ok {
				app.observeInputQueueDepth()
				app.handleTickerMessage(msg, time.Now())
			}
			// ok=false here is defensive only: see this function's own
			// doc comment on why nats.go never actually closes this
			// channel today. If a future client version ever does, there
			// is nothing further to drain from it, but tradesStopped
			// alone still gates the loop below.
		case <-tradesDone:
			tradesStopped = true
			tradesDone = nil
		default:
			if tradesStopped {
				break drain
			}
			if time.Now().After(deadline) {
				slog.Warn("momentumcapture.shutdown_drain_timed_out", "trades_stopped", tradesStopped)
				break drain
			}
			// Nothing ready on any channel right now but trades hasn't
			// confirmed stopping yet: a short sleep avoids busy spinning
			// while staying well within the bound above.
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
			slog.Error("momentumcapture.writer_inbox_full_at_shutdown", "dropped", len(final))
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

// consumeLossLatches turns any pending visibility-loss signal into a real,
// conservative discontinuity mark. Called before every engine.Flush (both
// the periodic tick and the final one at shutdown) so a bar never closes
// as complete just because the specific channel meant to report its own
// overflow was itself the thing that overflowed.
func (app *application) consumeLossLatches() {
	if at, ok := app.tradeVisibilityLost.consume(); ok {
		app.markTradesFeedInterrupted(at, "trade_drop_channel_exhausted")
	}
	if at, ok := app.lifecycleVisibilityLost.consume(); ok {
		app.markTradesFeedInterrupted(at, "lifecycle_queue_full")
	}
	if at, ok := app.natsFaultVisibilityLost.consume(); ok {
		app.markTickerFeedInterrupted(at, "nats_fault_queue_full")
	}
}

// enqueue hands closed bars to the dedicated writer goroutine without
// blocking the engine-owning loop: a full writerInbox (the writer
// goroutine's own database I/O falling behind) drops the newest bars and
// counts it, the same non-blocking-with-drop-counter contract every other
// channel in this file uses during normal operation. shutdown uses a
// bounded blocking send instead for its own final bars (see shutdown).
func (app *application) enqueue(bars []momentum.Bar) {
	if len(bars) == 0 {
		return
	}
	select {
	case app.writerInbox <- bars:
	default:
		app.stats.writerInboxDropsTotal += uint64(len(bars))
		slog.Warn("momentumcapture.writer_inbox_full", "dropped", len(bars))
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

func (app *application) handleTrade(trade bybit.PublicTrade) {
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
		Symbol:       trade.Symbol,
		Side:         side,
		Price:        trade.Price,
		Size:         trade.Size,
		EventAt:      trade.EventAt,
		ReceivedAt:   trade.ReceivedAt,
		TradeID:      trade.TradeID,
		IsBlockTrade: trade.BlockTrade,
		IsRPI:        trade.RPI,
		Seq:          trade.Seq,
	})
	if err != nil {
		app.stats.tradesInvalidTotal++
		return
	}
	app.stats.tradesAcceptedTotal++
	app.readiness.ObserveTrade(trade.Symbol)
	app.enqueue(bars)
}

// handleTradeDrop marks the dropped trade's own symbol interrupted: a
// trade that never reached the engine (because tradeEvents was full, see
// consumeTrade) must not let that symbol's current bar go on looking
// complete just because nothing here ever saw it arrive and get lost.
func (app *application) handleTradeDrop(trade bybit.PublicTrade) {
	app.stats.tradeDropsTotal++
	app.enqueue(app.engine.MarkTradesDiscontinuity(trade.Symbol, trade.EventAt))
}

// handleLifecycle marks exactly the affected shard's own symbols
// interrupted on disconnect (see TradeLifecycleEvent's doc comment on why
// per-shard detail matters here): a global reconnect counter cannot say
// which symbols actually lost their feed.
func (app *application) handleLifecycle(event bybit.TradeLifecycleEvent) {
	app.stats.tradeLifecycleTotal++
	if event.DisconnectedAt.IsZero() {
		return // a "connected" event: nothing to mark unhealthy
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

func (app *application) handleNATSFault(fault natsFault) {
	switch fault.kind {
	case "disconnected":
		app.stats.natsDisconnectTotal++
		app.markTickerFeedInterrupted(fault.at, "nats_disconnected")
		slog.Warn("momentumcapture.nats_disconnected", "err", fault.err)
	case "reconnected":
		app.stats.natsReconnectTotal++
		slog.Info("momentumcapture.nats_reconnected")
	case "slow_consumer":
		app.stats.natsSlowConsumerTotal++
		// A slow consumer means NATS has already started or is about to
		// start dropping messages for this subscription; which symbols
		// were affected is unknowable from here, so this is treated the
		// same as a full disconnect rather than left as a counter-only
		// event.
		app.markTickerFeedInterrupted(fault.at, "nats_slow_consumer")
	}
}

// markTickerFeedInterrupted marks the WHOLE universe's ticker feed
// interrupted: unlike a trade shard, momentum-capture cannot know which
// symbols were affected by a NATS-level disconnect or slow-consumer
// condition from its own client, since ticker/OI publishing lives in a
// separate process (cmd/collector). Conservative by design: marking too
// much incomplete costs nothing but disk; marking too little lets a real
// gap look like clean data.
func (app *application) markTickerFeedInterrupted(at time.Time, reason string) {
	for _, symbol := range app.universe.Symbols {
		app.enqueue(app.engine.MarkTickerDiscontinuity(symbol, at))
	}
	app.stats.lastDiscontinuityAt = at
	app.stats.lastDiscontinuityFor = "*"
	slog.Warn("momentumcapture.ticker_feed_interrupted", "reason", reason)
}

// markTradesFeedInterrupted marks the WHOLE universe's trades feed
// interrupted. Used only when visibility into WHICH shard or symbol lost
// its trade feed was itself lost (see consumeLossLatches): at that point
// the system is overloaded broadly enough that a single-symbol guess would
// understate the real risk, the same reasoning markTickerFeedInterrupted
// applies to a NATS-level fault.
func (app *application) markTradesFeedInterrupted(at time.Time, reason string) {
	for _, symbol := range app.universe.Symbols {
		app.enqueue(app.engine.MarkTradesDiscontinuity(symbol, at))
	}
	app.stats.lastDiscontinuityAt = at
	app.stats.lastDiscontinuityFor = "*"
	slog.Warn("momentumcapture.trades_feed_interrupted", "reason", reason)
}

func (app *application) handleTickerMessage(msg *nats.Msg, receivedAt time.Time) {
	started := time.Now()
	defer func() { app.stats.tickerHandler.observe(time.Since(started)) }()
	observation, sessionID, err := parseTickerObservation(msg.Data, receivedAt)
	if err != nil {
		app.stats.tickersInvalidTotal++
		return
	}
	if !app.universe.Contains(observation.Symbol) {
		// cmd/collector freezes its OWN ticker universe independently
		// (see Universe's doc comment on the cross-process drift risk): a
		// symbol it publishes that this process's own trades-WS universe
		// never subscribed to must never reach the engine or be persisted
		// under this process's universe_version.
		app.stats.tickersOutOfScopeTotal++
		return
	}
	if !observation.ObservedAt.IsZero() && !observation.ObservedAt.After(started) {
		app.stats.tickerReceiveToHandle.observe(started.Sub(observation.ObservedAt))
	}
	if lastSession, seen := app.tickerSessionBySymbol[observation.Symbol]; seen && lastSession != sessionID {
		app.stats.tickerReconnectTotal++
		app.enqueue(app.engine.MarkTickerDiscontinuity(observation.Symbol, observation.EventAt))
	}
	app.tickerSessionBySymbol[observation.Symbol] = sessionID
	app.tickerLastSeenAt[observation.Symbol] = receivedAt
	delete(app.tickerGapMarked, observation.Symbol) // a fresh observation ends any open gap

	bars, err := app.engine.AddTickerObservation(observation)
	if err != nil {
		app.stats.tickersInvalidTotal++
		return
	}
	app.stats.tickersAcceptedTotal++
	app.readiness.ObserveTicker(observation.Symbol)
	app.enqueue(bars)
}

// checkTickerGaps proactively marks a symbol's ticker feed interrupted
// after tickerGapThreshold of silence (see its doc comment, including the
// known residual gap it does not close), rather than only detecting a
// reconnect retroactively via the next message's StreamSessionID: without
// this, a bar engine.Flush force-closes during a real but
// not-yet-revealed gap would keep looking complete.
func (app *application) checkTickerGaps(now time.Time) {
	for _, symbol := range app.universe.Symbols {
		if app.tickerGapMarked[symbol] {
			continue
		}
		lastSeen, ok := app.tickerLastSeenAt[symbol]
		if !ok || now.Sub(lastSeen) < tickerGapThreshold {
			continue
		}
		app.stats.tickerGapTotal++
		app.tickerGapMarked[symbol] = true
		app.enqueue(app.engine.MarkTickerDiscontinuity(symbol, now))
	}
}

func (app *application) logHealth(ctx context.Context) {
	now := time.Now()
	if dropped, err := app.tickerSub.Dropped(); err == nil {
		if uint64(dropped) > app.stats.natsDroppedTotal {
			// NATS's own client-side buffer has dropped messages since we
			// last checked: some symbols' ticker updates are genuinely
			// gone, and which ones is unknowable, so this is treated the
			// same as a slow-consumer/disconnect fault.
			app.markTickerFeedInterrupted(now, "nats_client_buffer_dropped")
		}
		app.stats.natsDroppedTotal = uint64(dropped)
	}

	inputDepth := app.observeInputQueueDepth()

	health := momentumcapture.BuildUniverseHealth(app.universe, app.lastDrift, app.readiness, now)
	health.Exchange = exchange
	health.CatalogItemsTotal = app.catalog.CatalogItemsTotal
	health.CryptoPerpetualsIncluded = app.catalog.CryptoPerpetualsIncluded
	health.StandardCryptoIncluded = app.catalog.StandardCryptoIncluded
	health.InnovationCryptoIncluded = app.catalog.InnovationCryptoIncluded
	health.DatedFuturesExcluded = app.catalog.DatedFuturesExcluded
	health.StockPerpetualsExcluded = app.catalog.StockPerpetualsExcluded
	health.CommodityPerpetualsExcluded = app.catalog.CommodityPerpetualsExcluded
	health.UnknownContractExcluded = app.catalog.UnknownContractExcluded
	health.UnknownSymbolTypeExcluded = app.catalog.UnknownSymbolTypeExcluded
	health.InvalidInstrumentExcluded = app.catalog.InvalidInstrumentExcluded
	health.NonUSDTExcluded = app.catalog.NonUSDTExcluded
	health.NonTradingExcluded = app.catalog.NonTradingExcluded
	health = momentumcapture.ApplyWriterStats(health, app.writer.Stats())
	health.StartedAt = app.universe.CapturedAt
	health.UpdatedAt = now
	health.LastBarAt = app.stats.lastBarAt
	health.BarsCompletedTotal = app.stats.barsCompletedTotal
	health.LateEventsTotal = app.stats.lateEventsTotal
	health.TickerGapTotal = app.stats.tickerGapTotal
	health.LastDiscontinuityAt = app.stats.lastDiscontinuityAt
	health.LastDiscontinuityFor = app.stats.lastDiscontinuityFor
	health.TradeReconnectTotal = app.source.StreamStats().TradeReconnectTotal
	health.TradeReadTimeoutTotal = app.source.StreamStats().TradeReadTimeoutTotal
	health.NATSDisconnectTotal = app.stats.natsDisconnectTotal
	health.NATSReconnectTotal = app.stats.natsReconnectTotal
	health.NATSSlowConsumerTotal = app.stats.natsSlowConsumerTotal
	health.NATSDroppedTotal = app.stats.natsDroppedTotal
	health.InputQueueDepth = inputDepth
	health.InputQueuePeak = app.stats.inputQueuePeak
	health.InputQueueDropsTotal = app.stats.tradeDropsTotal + app.stats.writerInboxDropsTotal + app.tradeDropsLost.Load()
	health.TradeLagMaxMs = app.stats.tradeLagMaxMs
	health.TickerLagMaxMs = app.stats.tickerLagMaxMs
	applyLatencyHealth(&health, &app.stats)
	health.ProjectedBytesPerDay = float64(app.universe.Count()) * 1440 * estimatedHotBytesPerRow
	health.Status = deriveHealthStatus(health)

	storeCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	publishStarted := time.Now()
	if err := app.healthStore.StoreHealth(storeCtx, health); err != nil {
		slog.Warn("momentumcapture.health_store_failed", "err", err)
	}
	app.stats.healthPublish.observe(time.Since(publishStarted))
	slog.Info(
		"momentumcapture.health",
		"status", health.Status,
		"ready_symbols", health.ReadySymbols,
		"subscribed_symbols", health.SubscribedSymbols,
		"bars_completed_total", health.BarsCompletedTotal,
		"bars_persisted_total", health.BarsPersistedTotal,
		"writer_queue_depth", health.WriterQueueDepth,
		"persist_errors_total", health.PersistErrorsTotal,
		"payload_hash_mismatch_total", health.PayloadHashMismatchTotal,
		"tickers_out_of_scope_total", app.stats.tickersOutOfScopeTotal,
	)
}

func (app *application) observeInputQueueDepth() int {
	depth := len(app.tradeEvents) + len(app.tradeDrops) + len(app.lifecycleEvents) + len(app.natsFaults) + len(app.tickerMsgs)
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

	tickerWait := stats.tickerReceiveToHandle.summary()
	health.TickerReceiveToHandleCount = stats.tickerReceiveToHandle.count
	health.TickerReceiveToHandleP95Us = durationMicroseconds(tickerWait.P95)
	health.TickerReceiveToHandleP99Us = durationMicroseconds(tickerWait.P99)
	health.TickerReceiveToHandleMaxUs = durationMicroseconds(tickerWait.Max)

	tickerHandler := stats.tickerHandler.summary()
	health.TickerHandlerCount = stats.tickerHandler.count
	health.TickerHandlerP95Us = durationMicroseconds(tickerHandler.P95)
	health.TickerHandlerP99Us = durationMicroseconds(tickerHandler.P99)
	health.TickerHandlerMaxUs = durationMicroseconds(tickerHandler.Max)

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

// deriveHealthStatus picks the most severe applicable condition, most
// severe first. A canary run with ANY persist error or payload hash
// mismatch has already failed ROADMAP item 6's "zero persistence/drop
// errors" bar for its whole run, so these stay degraded once seen rather
// than clearing themselves if the immediate symptom does; the counters
// backing every case here are always visible in Health regardless of
// status for anyone who wants the raw numbers instead of the summary.
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

// parseTickerObservation decodes one bybit.TickerEvent (published by
// cmd/collector to tickerNATSSubject) into a momentum.TickerObservation,
// alongside the message's own StreamSessionID for reconnect detection.
// Both the future-skew check and ObservedAt use the event's own
// ReceivedAtMs (cmd/collector's original wall-clock receive time from
// Bybit) when present, not this process's own NATS consumption time:
// using two different clocks for those two things would make the skew
// check and the measured lag inconsistent with each other, and using
// local receive time for lag specifically would inflate every measured
// ticker lag by however long the extra collector-to-NATS-to-here hop
// took, making TickerLagMaxMs incomparable to TradeLagMaxMs (which IS
// measured at this process's own WebSocket receive, since it owns that
// connection directly). Fields that fail to parse individually are
// dropped (set nil) rather than rejecting the whole message, except when
// that would violate momentum.Engine's pairComplete contract (a value
// with no timestamp, or vice versa): OpenInterest/OpenInterestValue and
// their timestamp pairs are kept or dropped as atomic groups.
func parseTickerObservation(data []byte, receivedAt time.Time) (momentum.TickerObservation, string, error) {
	var event bybit.TickerEvent
	if err := json.Unmarshal(data, &event); err != nil {
		return momentum.TickerObservation{}, "", fmt.Errorf("decode ticker: %w", err)
	}
	if event.SchemaVersion != 0 && event.SchemaVersion != 1 {
		return momentum.TickerObservation{}, "", fmt.Errorf("unsupported ticker schema version %d", event.SchemaVersion)
	}
	if event.Source != "bybit" {
		return momentum.TickerObservation{}, "", fmt.Errorf("unexpected ticker source %q", event.Source)
	}
	if event.TS <= 0 {
		return momentum.TickerObservation{}, "", errors.New("ticker timestamp is required")
	}

	observedAt := receivedAt
	if event.ReceivedAtMs > 0 {
		observedAt = time.UnixMilli(event.ReceivedAtMs)
	}

	eventAt := time.UnixMilli(event.TS)
	if eventAt.After(observedAt.Add(maxTickerFutureSkew)) {
		return momentum.TickerObservation{}, "", errors.New("ticker timestamp is too far in the future")
	}

	oi, oiEventAt, oiObservedAt := optionalTriple(event.OpenInterest, event.OpenInterestEventAtMs, event.OpenInterestObservedAtMs)
	oiValue, oiValueEventAt, oiValueObservedAt := optionalTriple(event.OpenInterestValue, event.OpenInterestValueEventAtMs, event.OpenInterestValueObservedAtMs)

	return momentum.TickerObservation{
		Symbol:    event.Symbol,
		LastPrice: optionalPositiveFloat(event.LastPrice),
		BidPrice:  optionalPositiveFloat(event.Bid),
		AskPrice:  optionalPositiveFloat(event.Ask),

		OpenInterest:           oi,
		OpenInterestEventAt:    oiEventAt,
		OpenInterestObservedAt: oiObservedAt,

		OpenInterestValue:           oiValue,
		OpenInterestValueEventAt:    oiValueEventAt,
		OpenInterestValueObservedAt: oiValueObservedAt,

		EventAt:    eventAt,
		ObservedAt: observedAt,
	}, event.StreamSessionID, nil
}

// optionalTriple keeps value+eventAtMs+observedAtMs together or drops them
// together, matching momentum.Engine's pairComplete requirement.
func optionalTriple(value *string, eventAtMs, observedAtMs *int64) (*float64, *time.Time, *time.Time) {
	parsed := optionalNonNegativeFloat(value)
	if parsed == nil || eventAtMs == nil || observedAtMs == nil {
		return nil, nil, nil
	}
	eventAt := time.UnixMilli(*eventAtMs)
	observedAt := time.UnixMilli(*observedAtMs)
	return parsed, &eventAt, &observedAt
}

func optionalPositiveFloat(value *string) *float64 {
	if value == nil {
		return nil
	}
	parsed, err := strconv.ParseFloat(*value, 64)
	if err != nil || math.IsNaN(parsed) || math.IsInf(parsed, 0) || parsed <= 0 {
		return nil
	}
	return &parsed
}

func optionalNonNegativeFloat(value *string) *float64 {
	if value == nil {
		return nil
	}
	parsed, err := strconv.ParseFloat(*value, 64)
	if err != nil || math.IsNaN(parsed) || math.IsInf(parsed, 0) || parsed < 0 {
		return nil
	}
	return &parsed
}

func configureLogging() {
	level := slog.LevelInfo
	if strings.EqualFold(os.Getenv("LOG_LEVEL"), "debug") {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level})))
}

func loadConfig() config {
	return config{
		NATSURL:     envString("NATS_URL", "nats://localhost:4222"),
		DatabaseURL: envString("DATABASE_URL", "postgres://schurfer:schurfer_dev@localhost:5432/schurfer"),
		RedisAddr:   envString("REDIS_ADDR", "localhost:6379"),
	}
}

func envString(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

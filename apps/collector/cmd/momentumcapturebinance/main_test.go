package main

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/mavlevich/schurfer/collector/internal/binance"
	"github.com/mavlevich/schurfer/collector/internal/momentum"
	"github.com/mavlevich/schurfer/collector/internal/momentumcapture"
	"github.com/redis/go-redis/v9"
)

func newTestApplication(symbols []string) *application {
	universe := momentumcapture.NewUniverse(symbols, time.Unix(0, 0))
	return &application{
		engine:                   momentum.New(),
		writer:                   momentumcapture.NewWriter(nil, "binance", marketType, universe.Hash),
		universe:                 universe,
		readiness:                momentumcapture.NewReadinessTracker(universe),
		source:                   binance.NewSource(), // never started: no goroutine touches it in these tests
		writerInbox:              make(chan []momentum.Bar, writerInboxBuffer),
		openInterestLastSeenAt:   make(map[string]time.Time),
		openInterestGapMarked:    make(map[string]bool),
		openInterestGapThreshold: computeOpenInterestGapThreshold(len(symbols), binance.DefaultOpenInterestSchedulerConfig()),
		lastDrift:                universe.CheckDrift(universe.Symbols, time.Unix(0, 0)),
	}
}

// TestMarketTypeFitsTheSharedColumnAndMatchesBybitsConvention is a
// regression for a code-review finding: an earlier version of this file
// passed binance.MarketType ("linear_usdt_perpetual", 21 bytes) directly
// into NewWriter. timeseries.bybit_momentum_bars_1m's market_type column
// is VARCHAR(16) (packages/journal/migrations/versions/
// 0024_bybit_momentum_bars_1m.py) -- every single insert would have failed
// with "value too long for type character varying(16)", and no unit test
// here catches that directly since every test constructs Writer with a
// nil pgxpool (no real INSERT ever executes). This test would have caught
// it without needing a database.
func TestMarketTypeFitsTheSharedColumnAndMatchesBybitsConvention(t *testing.T) {
	t.Parallel()
	const columnWidth = 16 // market_type VARCHAR(16)
	if len(marketType) > columnWidth {
		t.Fatalf("marketType %q is %d bytes, exceeds the shared market_type VARCHAR(%d) column", marketType, len(marketType), columnWidth)
	}
	// Not just a width check: "linear" is genuinely the same product type
	// (USDT-margined linear perpetual) cmd/momentumcapture's own literal
	// already uses, so a future cross-venue query grouping by market_type
	// sees both venues, not just whichever one happened to match.
	if marketType != "linear" {
		t.Fatalf("marketType = %q, want \"linear\" to match cmd/momentumcapture's own convention", marketType)
	}
}

func TestApplicationHandleTradeMarksReadinessAndEnqueuesBars(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	trade := binance.PublicTrade{
		Symbol:     "BTCUSDT",
		AggTradeID: "1",
		Side:       "buy",
		EventAt:    time.Unix(60, 0).UTC(),
		ReceivedAt: time.Unix(60, 0).UTC(),
		Price:      100,
		Size:       1,
	}
	app.handleTrade(trade)
	if app.stats.tradesAcceptedTotal != 1 {
		t.Fatalf("tradesAcceptedTotal = %d, want 1", app.stats.tradesAcceptedTotal)
	}
	for _, symbol := range app.readiness.MissingTrades() {
		if symbol == "BTCUSDT" {
			t.Fatal("BTCUSDT should no longer be missing a trade observation")
		}
	}

	invalid := binance.PublicTrade{Symbol: "BTCUSDT"} // missing required fields
	app.handleTrade(invalid)
	if app.stats.tradesInvalidTotal != 1 {
		t.Fatalf("tradesInvalidTotal = %d, want 1", app.stats.tradesInvalidTotal)
	}
}

// TestApplicationHandleTradeLeavesBinanceOnlyFieldsAtTheirHonestZeroValue
// is the regression this file's own doc comment on handleTrade promises:
// Seq/IsBlockTrade/IsRPI must never be fabricated for a venue with no such
// concept.
func TestApplicationHandleTradeLeavesBinanceOnlyFieldsAtTheirHonestZeroValue(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.handleTrade(binance.PublicTrade{
		Symbol: "BTCUSDT", AggTradeID: "1", Side: "sell",
		EventAt: time.Unix(60, 0).UTC(), ReceivedAt: time.Unix(60, 0).UTC(),
		Price: 100, Size: 1,
	})
	bars := app.engine.Flush(time.Unix(120, 0).UTC())
	if len(bars) != 1 {
		t.Fatalf("got %d bars, want 1", len(bars))
	}
	bar := bars[0]
	if bar.MinTradeSeq != nil || bar.MaxTradeSeq != nil {
		t.Fatalf("MinTradeSeq/MaxTradeSeq = %v/%v, want nil: Seq is never populated for Binance", bar.MinTradeSeq, bar.MaxTradeSeq)
	}
	if bar.Sell.BlockTradeCount != 0 || bar.Sell.RPITradeCount != 0 {
		t.Fatalf("BlockTradeCount/RPITradeCount = %d/%d, want 0: no such concept exists for Binance", bar.Sell.BlockTradeCount, bar.Sell.RPITradeCount)
	}
}

func TestApplicationHandleTradeDropMarksDiscontinuity(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.handleTradeDrop(binance.PublicTrade{Symbol: "BTCUSDT", EventAt: time.Unix(60, 0).UTC()})
	if app.stats.tradeDropsTotal != 1 {
		t.Fatalf("tradeDropsTotal = %d, want 1", app.stats.tradeDropsTotal)
	}
	bars := app.engine.Flush(time.Unix(120, 0).UTC())
	if len(bars) != 1 || bars[0].TradesComplete {
		t.Fatalf("expected exactly one incomplete bar from the discontinuity mark, got %+v", bars)
	}
}

func TestApplicationHandleLifecycleMarksOnlyAffectedSymbolsAndTalliesReconnects(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT", "ETHUSDT"})
	for _, symbol := range []string{"BTCUSDT", "ETHUSDT"} {
		app.handleTrade(binance.PublicTrade{
			Symbol: symbol, AggTradeID: "1", Side: "buy",
			EventAt: time.Unix(60, 0).UTC(), ReceivedAt: time.Unix(60, 0).UTC(),
			Price: 100, Size: 1,
		})
	}

	app.handleLifecycle(binance.TradeLifecycleEvent{
		ShardSessionID: "shard-1",
		Symbols:        []string{"BTCUSDT"},
		DisconnectedAt: time.Unix(65, 0).UTC(),
		Reason:         "read timeout",
		ReadTimeout:    true,
	})

	if app.stats.lastDiscontinuityFor != "BTCUSDT" {
		t.Fatalf("lastDiscontinuityFor = %q, want BTCUSDT", app.stats.lastDiscontinuityFor)
	}
	// Regression: cmd/momentumcapture reads these from bybit.Source.
	// StreamStats(); this process has no such method and must tally them
	// itself directly from the lifecycle event (see handleLifecycle's own
	// doc comment).
	if app.stats.tradeReconnectTotal != 1 {
		t.Fatalf("tradeReconnectTotal = %d, want 1", app.stats.tradeReconnectTotal)
	}
	if app.stats.tradeReadTimeoutTotal != 1 {
		t.Fatalf("tradeReadTimeoutTotal = %d, want 1", app.stats.tradeReadTimeoutTotal)
	}

	// A "connected" lifecycle event (zero DisconnectedAt) must not mark
	// anything unhealthy or count as a reconnect.
	before := app.stats.tradeLifecycleTotal
	app.handleLifecycle(binance.TradeLifecycleEvent{ShardSessionID: "shard-1", Symbols: []string{"ETHUSDT"}, ConnectedAt: time.Now()})
	if app.stats.tradeLifecycleTotal != before+1 {
		t.Fatal("connected lifecycle event should still be counted")
	}
	if app.stats.tradeReconnectTotal != 1 {
		t.Fatal("a connected event must not itself count as a reconnect")
	}
	if app.stats.lastDiscontinuityFor != "BTCUSDT" {
		t.Fatal("a connected event must not overwrite the last real discontinuity")
	}
}

// TestHandleOpenInterestRecordsReceiveToHandleLatency is a regression for a
// code-review finding: an earlier version tracked handler duration
// (openInterestHandler) but never queue-wait duration, unlike the trade
// path's tradeReceiveToHandle -- leaving Health's own TickerReceiveToHandle*
// fields permanently zero for this process regardless of real queue
// backpressure.
func TestHandleOpenInterestRecordsReceiveToHandleLatency(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	observedAt := time.Now().Add(-5 * time.Millisecond)
	app.handleOpenInterest(binance.OpenInterestReading{
		Symbol: "BTCUSDT", Amount: "1000",
		EventAt: observedAt, ObservedAt: observedAt,
	})
	if app.stats.openInterestReceiveToHandle.count != 1 {
		t.Fatalf("openInterestReceiveToHandle.count = %d, want 1", app.stats.openInterestReceiveToHandle.count)
	}
}

func TestApplicationHandleOpenInterestAcceptsValidReadingAndMarksReadiness(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.handleOpenInterest(binance.OpenInterestReading{
		Symbol: "BTCUSDT", Amount: "1000.5",
		EventAt: time.Unix(60, 0).UTC(), ObservedAt: time.Unix(60, 0).UTC(),
	})
	if app.stats.openInterestAcceptedTotal != 1 {
		t.Fatalf("openInterestAcceptedTotal = %d, want 1", app.stats.openInterestAcceptedTotal)
	}
	for _, symbol := range app.readiness.MissingTicker() {
		if symbol == "BTCUSDT" {
			t.Fatal("BTCUSDT should no longer be missing a ticker/OI observation")
		}
	}
	bars := app.engine.Flush(time.Unix(120, 0).UTC())
	if len(bars) != 1 {
		t.Fatalf("got %d bars, want 1", len(bars))
	}
	bar := bars[0]
	if bar.OpenInterest == nil || *bar.OpenInterest != 1000.5 {
		t.Fatalf("OpenInterest = %v, want 1000.5", bar.OpenInterest)
	}
	if bar.OpenInterestValue != nil {
		t.Fatalf("OpenInterestValue = %v, want nil: this endpoint has no value field", bar.OpenInterestValue)
	}
	if bar.OpenPrice != nil || bar.ClosePrice != nil || bar.LastBidPrice != nil || bar.LastAskPrice != nil {
		t.Fatalf("OHLC/bid/ask must stay nil forever: got open=%v close=%v bid=%v ask=%v",
			bar.OpenPrice, bar.ClosePrice, bar.LastBidPrice, bar.LastAskPrice)
	}
}

func TestApplicationHandleOpenInterestRejectsMalformedAmount(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.handleOpenInterest(binance.OpenInterestReading{
		Symbol: "BTCUSDT", Amount: "not-a-number",
		EventAt: time.Unix(60, 0).UTC(), ObservedAt: time.Unix(60, 0).UTC(),
	})
	if app.stats.openInterestInvalidTotal != 1 {
		t.Fatalf("openInterestInvalidTotal = %d, want 1", app.stats.openInterestInvalidTotal)
	}
	if app.stats.openInterestAcceptedTotal != 0 {
		t.Fatal("a malformed amount must never be accepted into the engine")
	}
}

// TestApplicationHandleOpenInterestRejectsNonFiniteAmount is a regression
// for a code-review finding: strconv.ParseFloat accepts "NaN"/"Inf"/"+Inf"
// without error, so an err == nil check alone let a non-finite amount past
// the guard -- worse than a plain parse failure here specifically, since
// checkOpenInterestGaps is this feed's ONLY discontinuity detector (no
// NATS-level backup like Bybit has): a recurring non-finite reading would
// have silently satisfied the only alarm this feed has while contributing
// zero real OI data.
func TestApplicationHandleOpenInterestRejectsNonFiniteAmount(t *testing.T) {
	t.Parallel()
	for _, amount := range []string{"NaN", "Inf", "+Inf", "-Inf"} {
		app := newTestApplication([]string{"BTCUSDT"})
		app.handleOpenInterest(binance.OpenInterestReading{
			Symbol: "BTCUSDT", Amount: amount,
			EventAt: time.Unix(60, 0).UTC(), ObservedAt: time.Unix(60, 0).UTC(),
		})
		if app.stats.openInterestInvalidTotal != 1 {
			t.Fatalf("amount %q: openInterestInvalidTotal = %d, want 1", amount, app.stats.openInterestInvalidTotal)
		}
		if app.stats.openInterestAcceptedTotal != 0 {
			t.Fatalf("amount %q: must never be accepted into the engine", amount)
		}
		if _, ok := app.openInterestLastSeenAt["BTCUSDT"]; ok {
			t.Fatalf("amount %q: a rejected reading must not satisfy the gap detector's own last-seen clock", amount)
		}
	}
}

func TestApplicationHandleOpenInterestRejectsOutOfScopeSymbol(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"}) // ETHUSDT deliberately not in the frozen universe
	app.handleOpenInterest(binance.OpenInterestReading{
		Symbol: "ETHUSDT", Amount: "1000",
		EventAt: time.Unix(60, 0).UTC(), ObservedAt: time.Unix(60, 0).UTC(),
	})
	// Regression: an out-of-scope symbol must be counted separately from a
	// malformed payload (openInterestOutOfScopeTotal, not
	// openInterestInvalidTotal) -- a code-review finding, mirroring
	// cmd/momentumcapture's own tickersOutOfScopeTotal/tickersInvalidTotal
	// split.
	if app.stats.openInterestOutOfScopeTotal != 1 {
		t.Fatalf("openInterestOutOfScopeTotal = %d, want 1", app.stats.openInterestOutOfScopeTotal)
	}
	if app.stats.openInterestInvalidTotal != 0 {
		t.Fatalf("openInterestInvalidTotal = %d, want 0: an out-of-scope symbol is not a malformed payload", app.stats.openInterestInvalidTotal)
	}
	if app.stats.openInterestAcceptedTotal != 0 {
		t.Fatal("an out-of-scope symbol must never be accepted into the engine")
	}
	for _, bar := range app.engine.Flush(time.Unix(120, 0).UTC()) {
		if bar.Symbol == "ETHUSDT" {
			t.Fatal("the engine must never have created state for a symbol outside the frozen universe")
		}
	}
}

func TestApplicationHandleOpenInterestClearsAnOpenGapMark(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.openInterestGapMarked["BTCUSDT"] = true

	app.handleOpenInterest(binance.OpenInterestReading{
		Symbol: "BTCUSDT", Amount: "1000",
		EventAt: time.Unix(60, 0).UTC(), ObservedAt: time.Unix(60, 0).UTC(),
	})

	if app.openInterestGapMarked["BTCUSDT"] {
		t.Fatal("a fresh observation must clear an open gap mark")
	}
}

func TestCheckOpenInterestGapsMarksSilentSymbolOnceThenStopsUntilFreshObservation(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	start := time.Unix(1000, 0).UTC()
	app.openInterestLastSeenAt["BTCUSDT"] = start

	app.checkOpenInterestGaps(start.Add(app.openInterestGapThreshold + time.Second))
	if app.stats.openInterestGapTotal != 1 {
		t.Fatalf("openInterestGapTotal = %d, want 1", app.stats.openInterestGapTotal)
	}
	if !app.openInterestGapMarked["BTCUSDT"] {
		t.Fatal("BTCUSDT should be recorded as an already-flagged gap")
	}

	// A second check without any fresh observation must not re-mark (and
	// re-count) the same still-ongoing gap.
	app.checkOpenInterestGaps(start.Add(app.openInterestGapThreshold + 2*time.Second))
	if app.stats.openInterestGapTotal != 1 {
		t.Fatalf("openInterestGapTotal = %d after a second check, want still 1 (idempotent)", app.stats.openInterestGapTotal)
	}
}

func TestCheckOpenInterestGapsIgnoresRecentlySeenSymbols(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	now := time.Unix(1000, 0).UTC()
	app.openInterestLastSeenAt["BTCUSDT"] = now

	app.checkOpenInterestGaps(now.Add(time.Second))
	if app.stats.openInterestGapTotal != 0 {
		t.Fatalf("openInterestGapTotal = %d, want 0 for a symbol seen a second ago", app.stats.openInterestGapTotal)
	}
}

// TestComputeOpenInterestGapThresholdAppliesTheFloorForASmallUniverse is a
// regression: openInterestExpectedCycleDuration scales linearly with
// universe size, so a tiny universe (this package's own test fixtures use
// 1-2 symbols) divides a generous per-minute budget into a near-zero
// expected cycle -- a threshold that small would false-positive on
// ordinary single-request latency jitter, not catch a genuine
// interruption. TestCheckOpenInterestGapsIgnoresRecentlySeenSymbols above
// depends on this floor already; this test pins the exact mechanism so a
// future change to the multiplier/formula cannot silently drop it.
func TestComputeOpenInterestGapThresholdAppliesTheFloorForASmallUniverse(t *testing.T) {
	t.Parallel()
	got := computeOpenInterestGapThreshold(1, binance.OpenInterestSchedulerConfig{Workers: 8, RateLimitPerMinute: 1200})
	if got != openInterestGapThresholdFloor {
		t.Fatalf("computeOpenInterestGapThreshold(1 symbol) = %v, want the floor %v (the raw formula gives %v)",
			got, openInterestGapThresholdFloor,
			openInterestGapThresholdMultiple*openInterestExpectedCycleDuration(1, binance.OpenInterestSchedulerConfig{Workers: 8, RateLimitPerMinute: 1200}))
	}
}

// TestComputeOpenInterestGapThresholdScalesWithRealisticUniverseSize is
// the complementary case: at production scale the floor must NOT be what
// binds -- the real, budget-driven cadence should govern, matching this
// whole PR's own point (fix/binance-oi-poll-scheduler-v1: per-symbol
// cadence depends on universe size and configured rate, not one hardcoded
// constant).
func TestComputeOpenInterestGapThresholdScalesWithRealisticUniverseSize(t *testing.T) {
	t.Parallel()
	cfg := binance.OpenInterestSchedulerConfig{Workers: 8, RateLimitPerMinute: 1200}
	got := computeOpenInterestGapThreshold(525, cfg)
	if got <= openInterestGapThresholdFloor {
		t.Fatalf("computeOpenInterestGapThreshold(525 symbols) = %v, want it well above the %v floor at production scale", got, openInterestGapThresholdFloor)
	}
	want := openInterestGapThresholdMultiple * openInterestExpectedCycleDuration(525, cfg)
	if got != want {
		t.Fatalf("computeOpenInterestGapThreshold(525 symbols) = %v, want the raw formula's %v (floor should not have applied)", got, want)
	}
}

func TestConsumeOpenInterestRoutesOverflowToADropLogRatherThanBlocking(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.openInterestEvents = make(chan binance.OpenInterestReading, 1)
	app.openInterestEvents <- binance.OpenInterestReading{Symbol: "BTCUSDT"}

	done := make(chan error, 1)
	go func() {
		done <- app.consumeOpenInterest(context.Background(), binance.OpenInterestReading{Symbol: "ETHUSDT"})
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("consumeOpenInterest returned an error: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("consumeOpenInterest must never block PollOpenInterest's own goroutine on a full channel")
	}
	// Regression: an earlier version dropped the reading with only a log
	// line, no counter at all -- unlike every other loss path in this
	// file (tradeDropsLost, lifecycleVisibilityLost, ...), breaking the
	// "non-blocking-drop-and-COUNT" contract this function's own doc
	// comment promises. This ultimately feeds Health.InputQueueDropsTotal,
	// the only place an operator would see it.
	if got := app.openInterestDropsLost.Load(); got != 1 {
		t.Fatalf("openInterestDropsLost = %d, want 1", got)
	}
}

func TestObserveInputQueueDepthIncludesOpenInterestBuffer(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	tradeEvents := make(chan binance.PublicTrade, 2)
	tradeEvents <- binance.PublicTrade{}
	openInterestEvents := make(chan binance.OpenInterestReading, 3)
	openInterestEvents <- binance.OpenInterestReading{}
	openInterestEvents <- binance.OpenInterestReading{}
	app.tradeEvents = tradeEvents
	app.openInterestEvents = openInterestEvents

	if got := app.observeInputQueueDepth(); got != 3 {
		t.Fatalf("input queue depth = %d, want 3 including open interest readings", got)
	}
	if app.stats.inputQueuePeak != 3 {
		t.Fatalf("input queue peak = %d, want 3", app.stats.inputQueuePeak)
	}
}

func TestHandleDriftResultStoresRealDrift(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	drift := app.universe.CheckDrift([]string{"BTCUSDT", "NEWLISTINGUSDT"}, time.Now())

	app.handleDriftResult(drift)

	if !app.lastDrift.Stale || app.lastDrift.LiveHash != drift.LiveHash {
		t.Fatalf("lastDrift = %+v, want the passed-in real drift report stored, not discarded", app.lastDrift)
	}
}

func TestDeriveHealthStatusPriority(t *testing.T) {
	t.Parallel()
	if got := deriveHealthStatus(momentumcapture.Health{}); got != "ok" {
		t.Fatalf("status = %q, want ok for a clean snapshot", got)
	}

	stale := momentumcapture.Health{UniverseStale: true}
	if got := deriveHealthStatus(stale); got != "degraded_universe_stale" {
		t.Fatalf("status = %q, want degraded_universe_stale", got)
	}

	// TickerGapTotal doubles as this process's own OI-gap counter (see
	// logHealth's own doc comment); this asserts that pairing actually
	// drives the same status branch cmd/momentumcapture's NATS/ticker-gap
	// signals drive.
	feedInterrupted := stale
	feedInterrupted.TickerGapTotal = 1
	if got := deriveHealthStatus(feedInterrupted); got != "degraded_feed_interrupted" {
		t.Fatalf("status = %q, want degraded_feed_interrupted to outrank universe staleness", got)
	}

	queuePressure := feedInterrupted
	queuePressure.InputQueueDropsTotal = 1
	if got := deriveHealthStatus(queuePressure); got != "degraded_queue_pressure" {
		t.Fatalf("status = %q, want degraded_queue_pressure to outrank feed interruption", got)
	}

	mismatch := queuePressure
	mismatch.PayloadHashMismatchTotal = 1
	if got := deriveHealthStatus(mismatch); got != "degraded_payload_hash_mismatch" {
		t.Fatalf("status = %q, want degraded_payload_hash_mismatch to outrank queue pressure", got)
	}

	persistError := mismatch
	persistError.PersistErrorsTotal = 1
	if got := deriveHealthStatus(persistError); got != "degraded_persist_errors" {
		t.Fatalf("status = %q, want degraded_persist_errors to outrank everything else", got)
	}
}

func TestApplicationLogHealthPublishesToRedisWithExchangeAndExclusionCounts(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := momentumcapture.NewRedisStore(client)
	if err != nil {
		t.Fatal(err)
	}

	app := newTestApplication([]string{"BTCUSDT", "ETHUSDT"})
	app.healthStore = store
	app.catalog = binance.SymbolCatalogCounts{
		CatalogItemsTotal:             10,
		CryptoPerpetualsIncluded:      2,
		NonPerpetualContractExcluded:  5,
		UnderlyingIndexExcluded:       1,
		UnknownUnderlyingTypeExcluded: 1,
		NonUSDTExcluded:               1,
	}
	app.stats.barsCompletedTotal = 42

	app.logHealth(context.Background())

	fields, err := client.HGetAll(context.Background(), momentumcapture.HealthKey("binance")).Result()
	if err != nil {
		t.Fatal(err)
	}
	if fields["exchange"] != "binance" {
		t.Fatalf("exchange = %q, want binance", fields["exchange"])
	}
	if fields["status"] != "ok" {
		t.Fatalf("status = %q, want ok for a freshly frozen, non-stale universe", fields["status"])
	}
	if fields["bars_completed_total"] != "42" {
		t.Fatalf("bars_completed_total = %q, want 42", fields["bars_completed_total"])
	}
	if fields["catalog_items_total"] != "10" || fields["crypto_perpetuals_included"] != "2" {
		t.Fatalf("catalog fields wrong: %+v", fields)
	}
	want := `{"non_perpetual_contract":5,"underlying_index":1,"unknown_underlying_type":1}`
	if fields["exclusion_counts_json"] != want {
		t.Fatalf("exclusion_counts_json = %q, want %q", fields["exclusion_counts_json"], want)
	}
}

// closedChan returns an already-closed channel, standing in for a
// production tradesDone/oiDone whose real producer goroutine has already
// exited: these shutdown tests have no such goroutine at all.
func closedChan() <-chan struct{} {
	ch := make(chan struct{})
	close(ch)
	return ch
}

// drainWriterInbox mimics runWriter's own shutdown behavior (range over
// inbox until the loop goroutine closes it, then report result) without a
// real Writer/database.
func drainWriterInbox(inbox <-chan []momentum.Bar, done chan<- error, result error) {
	for range inbox {
	}
	done <- result
}

func TestApplicationShutdownReturnsWriterFlushError(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	writerDone := make(chan error, 1)
	go drainWriterInbox(app.writerInbox, writerDone, errors.New("db unavailable"))

	if err := app.shutdown(closedChan(), closedChan(), writerDone); err == nil {
		t.Fatal("shutdown must propagate a failed final writer flush, not report success")
	}
}

func TestApplicationShutdownDrainsBufferedTradeAndOpenInterestBeforeFinalFlush(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.tradeEvents = make(chan binance.PublicTrade, 1)
	app.tradeEvents <- binance.PublicTrade{
		Symbol: "BTCUSDT", AggTradeID: "1", Side: "buy",
		EventAt: time.Unix(60, 0).UTC(), ReceivedAt: time.Unix(60, 0).UTC(),
		Price: 100, Size: 1,
	}
	app.openInterestEvents = make(chan binance.OpenInterestReading, 1)
	app.openInterestEvents <- binance.OpenInterestReading{
		Symbol: "BTCUSDT", Amount: "1000",
		EventAt: time.Unix(60, 0).UTC(), ObservedAt: time.Unix(60, 0).UTC(),
	}

	writerDone := make(chan error, 1)
	go drainWriterInbox(app.writerInbox, writerDone, nil)

	if err := app.shutdown(closedChan(), closedChan(), writerDone); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	if app.stats.tradesAcceptedTotal != 1 {
		t.Fatal("shutdown must drain a trade already buffered in tradeEvents before its final flush")
	}
	if app.stats.openInterestAcceptedTotal != 1 {
		t.Fatal("shutdown must drain an OI reading already buffered before its final flush")
	}
}

func TestApplicationShutdownWaitsForBothTradesAndOpenInterestToStop(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	writerDone := make(chan error, 1)
	go drainWriterInbox(app.writerInbox, writerDone, nil)

	oiDone := make(chan struct{})
	go func() {
		time.Sleep(20 * time.Millisecond)
		close(oiDone)
	}()

	started := time.Now()
	if err := app.shutdown(closedChan(), oiDone, writerDone); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	if time.Since(started) < 20*time.Millisecond {
		t.Fatal("shutdown must wait for the OI poller to confirm stopped, not only the trade producer")
	}
}

func TestApplicationShutdownAppliesPendingLossLatchesBeforeFinalFlush(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.tradeVisibilityLost.markOnce(time.Unix(60, 0).UTC())

	writerDone := make(chan error, 1)
	go drainWriterInbox(app.writerInbox, writerDone, nil)

	if err := app.shutdown(closedChan(), closedChan(), writerDone); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	if app.stats.lastDiscontinuityFor != "*" {
		t.Fatal("a pending loss latch must be applied (whole-universe discontinuity) before the final flush, not dropped")
	}
}

func TestLossLatchMarksOnceAndConsumesOnce(t *testing.T) {
	t.Parallel()
	var latch lossLatch
	if _, ok := latch.consume(); ok {
		t.Fatal("an unmarked latch must report nothing to consume")
	}
	at := time.Unix(100, 0)
	latch.markOnce(at)
	latch.markOnce(at.Add(time.Second)) // a second mark before consume must not move the timestamp
	got, ok := latch.consume()
	if !ok || !got.Equal(at) {
		t.Fatalf("consume() = %v, %v, want %v, true", got, ok, at)
	}
	if _, ok := latch.consume(); ok {
		t.Fatal("consume must be one-shot: a second call with nothing new marked must report false")
	}
}

func TestRunWriterFlushesAndReportsOnInboxClose(t *testing.T) {
	t.Parallel()
	inbox := make(chan []momentum.Bar, 1)
	done := make(chan error, 1)
	go runWriter(momentumcapture.NewWriter(nil, "binance", marketType, "hash"), inbox, done)
	close(inbox)
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runWriter reported %v, want nil for an empty final flush against a nil pool", err)
		}
	case <-time.After(time.Second):
		t.Fatal("runWriter did not report on inbox close")
	}
}

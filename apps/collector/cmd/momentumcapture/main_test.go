package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/mavlevich/schurfer/collector/internal/bybit"
	"github.com/mavlevich/schurfer/collector/internal/momentum"
	"github.com/mavlevich/schurfer/collector/internal/momentumcapture"
	"github.com/nats-io/nats.go"
	"github.com/redis/go-redis/v9"
)

func newTestApplication(symbols []string) *application {
	universe := momentumcapture.NewUniverse(symbols, time.Unix(0, 0))
	return &application{
		engine:                momentum.New(),
		writer:                momentumcapture.NewWriter(nil, "bybit", "linear", universe.Hash),
		universe:              universe,
		readiness:             momentumcapture.NewReadinessTracker(universe),
		source:                bybit.NewSource(), // never Run/RunTrades-started: StreamStats() reads zero-valued atomics only
		writerInbox:           make(chan []momentum.Bar, writerInboxBuffer),
		tickerSessionBySymbol: make(map[string]string),
		tickerLastSeenAt:      make(map[string]time.Time),
		tickerGapMarked:       make(map[string]bool),
		lastDrift:             universe.CheckDrift(universe.Symbols, time.Unix(0, 0)),
	}
}

func TestParseTickerObservationDecodesAllFields(t *testing.T) {
	t.Parallel()
	last, bid, ask := "100.5", "100.4", "100.6"
	oi, oiValue := "12345", "1234500"
	oiEventMs, oiObservedMs := int64(1_000), int64(1_010)
	raw, err := json.Marshal(bybit.TickerEvent{
		SchemaVersion:                 1,
		Source:                        "bybit",
		Symbol:                        "BTCUSDT",
		TS:                            1_000,
		LastPrice:                     &last,
		Bid:                           &bid,
		Ask:                           &ask,
		OpenInterest:                  &oi,
		OpenInterestEventAtMs:         &oiEventMs,
		OpenInterestObservedAtMs:      &oiObservedMs,
		OpenInterestValue:             &oiValue,
		OpenInterestValueEventAtMs:    &oiEventMs,
		OpenInterestValueObservedAtMs: &oiObservedMs,
		StreamSessionID:               "session-a",
	})
	if err != nil {
		t.Fatal(err)
	}
	obs, sessionID, err := parseTickerObservation(raw, time.UnixMilli(1_020))
	if err != nil {
		t.Fatal(err)
	}
	if obs.Symbol != "BTCUSDT" || sessionID != "session-a" {
		t.Fatalf("unexpected observation: %+v session=%s", obs, sessionID)
	}
	if obs.LastPrice == nil || *obs.LastPrice != 100.5 {
		t.Fatalf("last price = %v, want 100.5", obs.LastPrice)
	}
	if obs.OpenInterest == nil || *obs.OpenInterest != 12345 {
		t.Fatalf("open interest = %v, want 12345", obs.OpenInterest)
	}
	if obs.OpenInterestEventAt == nil || !obs.OpenInterestEventAt.Equal(time.UnixMilli(1_000)) {
		t.Fatalf("open interest event at = %v", obs.OpenInterestEventAt)
	}
}

func TestParseDerivativesObservationPreservesSignedFundingAndFieldTimestamps(t *testing.T) {
	t.Parallel()
	mark, index, funding, next := "100.5", "100.0", "-0.00025", "7200000"
	eventMs, observedMs := int64(1_000), int64(1_010)
	raw, err := json.Marshal(bybit.TickerEvent{
		SchemaVersion: 1, Source: "bybit", Symbol: "BTCUSDT", TS: 1_000, ReceivedAtMs: observedMs,
		MarkPrice: &mark, MarkPriceEventAtMs: &eventMs, MarkPriceObservedAtMs: &observedMs,
		IndexPrice: &index, IndexPriceEventAtMs: &eventMs, IndexPriceObservedAtMs: &observedMs,
		FundingRate: &funding, FundingRateEventAtMs: &eventMs, FundingRateObservedAtMs: &observedMs,
		NextFundingTime: &next, NextFundingEventAtMs: &eventMs, NextFundingObservedAtMs: &observedMs,
	})
	if err != nil {
		t.Fatal(err)
	}
	obs, present, err := parseDerivativesObservation(raw, time.UnixMilli(1_020))
	if err != nil || !present {
		t.Fatalf("parse = %+v, %v, %v", obs, present, err)
	}
	if obs.FundingRate == nil || *obs.FundingRate != -0.00025 || obs.MarkPrice == nil || *obs.MarkPrice != 100.5 {
		t.Fatalf("unexpected values: %+v", obs)
	}
	if obs.NextFundingAt == nil || obs.NextFundingAt.UnixMilli() != 7_200_000 ||
		obs.MarkPriceEventAt == nil || obs.MarkPriceEventAt.UnixMilli() != 1_000 {
		t.Fatalf("unexpected provenance: %+v", obs)
	}
}

func TestParseDerivativesObservationDistinguishesRollingDeployAbsenceFromInvalidTuple(t *testing.T) {
	t.Parallel()
	legacy, err := json.Marshal(bybit.TickerEvent{SchemaVersion: 1, Source: "bybit", Symbol: "BTCUSDT", TS: 1_000})
	if err != nil {
		t.Fatal(err)
	}
	if _, present, err := parseDerivativesObservation(legacy, time.UnixMilli(1_010)); err != nil || present {
		t.Fatalf("legacy event = present %v err %v, want clean absence", present, err)
	}

	mark := "100"
	invalid, err := json.Marshal(bybit.TickerEvent{SchemaVersion: 1, Source: "bybit", Symbol: "BTCUSDT", TS: 1_000, MarkPrice: &mark})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := parseDerivativesObservation(invalid, time.UnixMilli(1_010)); err == nil {
		t.Fatal("a present value without provenance must fail closed")
	}
}

func TestParseTickerObservationDropsIncompleteOIGroupTogether(t *testing.T) {
	t.Parallel()
	last := "100"
	oi := "12345"
	oiEventMs := int64(1_000)
	// OpenInterestObservedAtMs deliberately missing: an incomplete pair
	// must drop the whole OI group, not send a value with a nil timestamp
	// into momentum.Engine (which would reject the entire observation).
	raw, err := json.Marshal(bybit.TickerEvent{
		SchemaVersion:         1,
		Source:                "bybit",
		Symbol:                "BTCUSDT",
		TS:                    1_000,
		LastPrice:             &last,
		OpenInterest:          &oi,
		OpenInterestEventAtMs: &oiEventMs,
	})
	if err != nil {
		t.Fatal(err)
	}
	obs, _, err := parseTickerObservation(raw, time.UnixMilli(1_020))
	if err != nil {
		t.Fatal(err)
	}
	if obs.OpenInterest != nil || obs.OpenInterestEventAt != nil {
		t.Fatalf("expected the whole OI group dropped, got %+v", obs)
	}
	if obs.LastPrice == nil || *obs.LastPrice != 100 {
		t.Fatal("an unrelated valid field must not be dropped by an incomplete OI group")
	}
}

func TestParseTickerObservationRejectsUnknownSchemaAndWrongSource(t *testing.T) {
	t.Parallel()
	last := "100"
	for _, event := range []bybit.TickerEvent{
		{SchemaVersion: 2, Source: "bybit", Symbol: "BTCUSDT", TS: 1_000, LastPrice: &last},
		{SchemaVersion: 1, Source: "binance", Symbol: "BTCUSDT", TS: 1_000, LastPrice: &last},
	} {
		raw, err := json.Marshal(event)
		if err != nil {
			t.Fatal(err)
		}
		if _, _, err := parseTickerObservation(raw, time.UnixMilli(1_020)); err == nil {
			t.Fatalf("invalid event unexpectedly accepted: %+v", event)
		}
	}
}

func TestParseTickerObservationRejectsFutureTimestamp(t *testing.T) {
	t.Parallel()
	last := "100"
	received := time.UnixMilli(1_000)
	raw, err := json.Marshal(bybit.TickerEvent{
		SchemaVersion: 1,
		Source:        "bybit",
		Symbol:        "BTCUSDT",
		TS:            received.Add(maxTickerFutureSkew + time.Millisecond).UnixMilli(),
		LastPrice:     &last,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := parseTickerObservation(raw, received); err == nil {
		t.Fatal("a ticker timestamp far in the future was unexpectedly accepted")
	}
}

func TestApplicationHandleTradeMarksReadinessAndEnqueuesBars(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	trade := bybit.PublicTrade{
		Symbol:     "BTCUSDT",
		TradeID:    "1",
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

	invalid := bybit.PublicTrade{Symbol: "BTCUSDT"} // missing required fields
	app.handleTrade(invalid)
	if app.stats.tradesInvalidTotal != 1 {
		t.Fatalf("tradesInvalidTotal = %d, want 1", app.stats.tradesInvalidTotal)
	}
}

func TestApplicationHandleLifecycleMarksOnlyAffectedSymbols(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT", "ETHUSDT"})
	// Seed both symbols with an open bar via a trade each.
	for _, symbol := range []string{"BTCUSDT", "ETHUSDT"} {
		app.handleTrade(bybit.PublicTrade{
			Symbol: symbol, TradeID: "1", Side: "buy",
			EventAt: time.Unix(60, 0).UTC(), ReceivedAt: time.Unix(60, 0).UTC(),
			Price: 100, Size: 1,
		})
	}

	app.handleLifecycle(bybit.TradeLifecycleEvent{
		ShardSessionID: "shard-1",
		Symbols:        []string{"BTCUSDT"},
		DisconnectedAt: time.Unix(65, 0).UTC(),
		Reason:         "read timeout",
	})

	if app.stats.lastDiscontinuityFor != "BTCUSDT" {
		t.Fatalf("lastDiscontinuityFor = %q, want BTCUSDT", app.stats.lastDiscontinuityFor)
	}
	// A "connected" lifecycle event (zero DisconnectedAt) must not mark anything.
	before := app.stats.tradeLifecycleTotal
	app.handleLifecycle(bybit.TradeLifecycleEvent{ShardSessionID: "shard-1", Symbols: []string{"ETHUSDT"}, ConnectedAt: time.Now()})
	if app.stats.tradeLifecycleTotal != before+1 {
		t.Fatal("connected lifecycle event should still be counted")
	}
	if app.stats.lastDiscontinuityFor != "BTCUSDT" {
		t.Fatal("a connected event must not overwrite the last real discontinuity")
	}
}

func TestApplicationHandleNATSFaultDisconnectMarksWholeUniverse(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT", "ETHUSDT", "SOLUSDT"})
	app.handleNATSFault(natsFault{kind: "disconnected", at: time.Unix(100, 0).UTC()})

	if app.stats.natsDisconnectTotal != 1 {
		t.Fatalf("natsDisconnectTotal = %d, want 1", app.stats.natsDisconnectTotal)
	}
	if app.stats.lastDiscontinuityFor != "*" {
		t.Fatalf("lastDiscontinuityFor = %q, want * (feed-wide)", app.stats.lastDiscontinuityFor)
	}

	app.handleNATSFault(natsFault{kind: "reconnected", at: time.Unix(101, 0).UTC()})
	if app.stats.natsReconnectTotal != 1 {
		t.Fatalf("natsReconnectTotal = %d, want 1", app.stats.natsReconnectTotal)
	}

	app.handleNATSFault(natsFault{kind: "slow_consumer", at: time.Unix(102, 0).UTC()})
	if app.stats.natsSlowConsumerTotal != 1 {
		t.Fatalf("natsSlowConsumerTotal = %d, want 1", app.stats.natsSlowConsumerTotal)
	}
	// A slow consumer is treated the same as a disconnect: which symbols'
	// updates NATS actually dropped is unknowable, so the whole universe's
	// ticker feed is marked interrupted, not just counted.
	if app.stats.lastDiscontinuityFor != "*" || !app.stats.lastDiscontinuityAt.Equal(time.Unix(102, 0).UTC()) {
		t.Fatalf("slow_consumer must mark the whole universe interrupted too, got for=%q at=%v",
			app.stats.lastDiscontinuityFor, app.stats.lastDiscontinuityAt)
	}
}

func TestApplicationHandleTickerMessageDetectsSessionChangeAndMarksDiscontinuity(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	last := "100"

	firstRaw, err := json.Marshal(bybit.TickerEvent{
		SchemaVersion: 1, Source: "bybit", Symbol: "BTCUSDT", TS: 60_000,
		LastPrice: &last, StreamSessionID: "session-a",
	})
	if err != nil {
		t.Fatal(err)
	}
	app.handleTickerMessage(&nats.Msg{Data: firstRaw}, time.UnixMilli(60_000))
	if app.stats.tickersAcceptedTotal != 1 || app.stats.tickerReconnectTotal != 0 {
		t.Fatalf("after first message: accepted=%d reconnects=%d", app.stats.tickersAcceptedTotal, app.stats.tickerReconnectTotal)
	}

	secondRaw, err := json.Marshal(bybit.TickerEvent{
		SchemaVersion: 1, Source: "bybit", Symbol: "BTCUSDT", TS: 120_000,
		LastPrice: &last, StreamSessionID: "session-b", // a new session: a reconnect happened
	})
	if err != nil {
		t.Fatal(err)
	}
	app.handleTickerMessage(&nats.Msg{Data: secondRaw}, time.UnixMilli(120_000))
	if app.stats.tickerReconnectTotal != 1 {
		t.Fatalf("tickerReconnectTotal = %d, want 1 after a StreamSessionID change", app.stats.tickerReconnectTotal)
	}
	if app.tickerSessionBySymbol["BTCUSDT"] != "session-b" {
		t.Fatalf("session tracking not updated: %v", app.tickerSessionBySymbol)
	}
}

func TestApplicationLogHealthPublishesToRedis(t *testing.T) {
	t.Parallel()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	store, err := momentumcapture.NewRedisStore(client)
	if err != nil {
		t.Fatal(err)
	}

	app := newTestApplication([]string{"BTCUSDT", "ETHUSDT"})
	app.universe.CapturedAt = time.Now()
	app.healthStore = store
	app.stats.barsCompletedTotal = 42
	app.stats.tradeHandler.observe(200 * time.Microsecond)
	app.stats.flush.observe(2 * time.Millisecond)

	app.logHealth(context.Background())

	fields, err := client.HGetAll(context.Background(), momentumcapture.HealthKey("bybit")).Result()
	if err != nil {
		t.Fatal(err)
	}
	if fields["status"] != "ok" {
		t.Fatalf("status = %q, want ok for a freshly frozen, non-stale universe", fields["status"])
	}
	if fields["subscribed_symbols"] != "2" {
		t.Fatalf("subscribed_symbols = %q, want 2", fields["subscribed_symbols"])
	}
	if fields["bars_completed_total"] != "42" {
		t.Fatalf("bars_completed_total = %q, want 42", fields["bars_completed_total"])
	}
	if fields["trade_handler_count"] != "1" || fields["trade_handler_p99_us"] != "250" {
		t.Fatalf("trade handler latency fields wrong: %+v", fields)
	}
	if fields["flush_count"] != "1" || fields["flush_p99_us"] != "2500" {
		t.Fatalf("flush latency fields wrong: %+v", fields)
	}
	if server.TTL(momentumcapture.HealthKey("bybit")) <= 0 {
		t.Fatal("health key should have a TTL so a dead process's last snapshot expires")
	}
}

func TestObserveInputQueueDepthIncludesTickerBuffer(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	tradeEvents := make(chan bybit.PublicTrade, 2)
	tradeEvents <- bybit.PublicTrade{}
	tickerMsgs := make(chan *nats.Msg, 3)
	tickerMsgs <- &nats.Msg{}
	tickerMsgs <- &nats.Msg{}
	app.tradeEvents = tradeEvents
	app.tickerMsgs = tickerMsgs

	if got := app.observeInputQueueDepth(); got != 3 {
		t.Fatalf("input queue depth = %d, want 3 including ticker messages", got)
	}
	if app.stats.inputQueuePeak != 3 {
		t.Fatalf("input queue peak = %d, want 3", app.stats.inputQueuePeak)
	}
}

func TestApplicationHandleTickerMessageRejectsOutOfScopeSymbol(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"}) // ETHUSDT deliberately not in the frozen universe
	last := "100"
	raw, err := json.Marshal(bybit.TickerEvent{
		SchemaVersion: 1, Source: "bybit", Symbol: "ETHUSDT", TS: 60_000,
		LastPrice: &last, StreamSessionID: "session-a",
	})
	if err != nil {
		t.Fatal(err)
	}
	app.handleTickerMessage(&nats.Msg{Data: raw}, time.UnixMilli(60_000))

	if app.stats.tickersOutOfScopeTotal != 1 {
		t.Fatalf("tickersOutOfScopeTotal = %d, want 1", app.stats.tickersOutOfScopeTotal)
	}
	if app.stats.tickersAcceptedTotal != 0 {
		t.Fatal("an out-of-scope symbol must never be accepted into the engine")
	}
	for _, bar := range app.engine.Flush(time.Unix(200, 0).UTC()) {
		if bar.Symbol == "ETHUSDT" {
			t.Fatal("the engine must never have created state for a symbol outside the frozen universe")
		}
	}
}

func TestApplicationHandleTradeDropMarksDiscontinuity(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.handleTradeDrop(bybit.PublicTrade{Symbol: "BTCUSDT", EventAt: time.Unix(60, 0).UTC()})

	if app.stats.tradeDropsTotal != 1 {
		t.Fatalf("tradeDropsTotal = %d, want 1", app.stats.tradeDropsTotal)
	}
	// Confirm it actually reached the engine, not just a counter: a real
	// trade landing right after must still close a bar that reads
	// TradesComplete=false, since the drop already interrupted it.
	app.handleTrade(bybit.PublicTrade{
		Symbol: "BTCUSDT", TradeID: "1", Side: "buy",
		EventAt: time.Unix(61, 0).UTC(), ReceivedAt: time.Unix(61, 0).UTC(),
		Price: 100, Size: 1,
	})
	bars := app.engine.Flush(time.Unix(200, 0).UTC())
	if len(bars) == 0 {
		t.Fatal("expected a bar to close")
	}
	if bars[0].TradesComplete {
		t.Fatal("a bar following a dropped trade must not read TradesComplete=true")
	}
}

func TestConsumeTradeRoutesOverflowToTradeDropsChannel(t *testing.T) {
	t.Parallel()
	app := &application{
		tradeEvents: make(chan bybit.PublicTrade, 1),
		tradeDrops:  make(chan bybit.PublicTrade, 1),
	}
	trade1 := bybit.PublicTrade{Symbol: "BTCUSDT", TradeID: "1"}
	trade2 := bybit.PublicTrade{Symbol: "BTCUSDT", TradeID: "2"}

	if err := app.consumeTrade(context.Background(), trade1); err != nil {
		t.Fatal(err)
	}
	// tradeEvents (capacity 1) is now full: the second trade must fall
	// back to tradeDrops instead of being silently lost with no way for
	// the loop to ever mark that symbol interrupted.
	if err := app.consumeTrade(context.Background(), trade2); err != nil {
		t.Fatal(err)
	}

	select {
	case got := <-app.tradeEvents:
		if got.TradeID != "1" {
			t.Fatalf("tradeEvents got %+v, want trade1", got)
		}
	default:
		t.Fatal("expected trade1 in tradeEvents")
	}
	select {
	case got := <-app.tradeDrops:
		if got.TradeID != "2" {
			t.Fatalf("tradeDrops got %+v, want trade2", got)
		}
	default:
		t.Fatal("expected trade2 routed to tradeDrops after tradeEvents filled up")
	}
	if app.tradeDropsLost.Load() != 0 {
		t.Fatal("tradeDropsLost should stay 0 while tradeDrops still has room")
	}
}

func TestConsumeTradeFallsBackToLostCounterWhenBothChannelsFull(t *testing.T) {
	t.Parallel()
	app := &application{
		tradeEvents: make(chan bybit.PublicTrade, 1),
		tradeDrops:  make(chan bybit.PublicTrade, 1),
	}
	_ = app.consumeTrade(context.Background(), bybit.PublicTrade{TradeID: "1"})
	_ = app.consumeTrade(context.Background(), bybit.PublicTrade{TradeID: "2"})
	// Both channels are now full; a third trade has nowhere left to go
	// except the absolute last-resort atomic counter.
	if err := app.consumeTrade(context.Background(), bybit.PublicTrade{TradeID: "3"}); err != nil {
		t.Fatal(err)
	}
	if got := app.tradeDropsLost.Load(); got != 1 {
		t.Fatalf("tradeDropsLost = %d, want 1", got)
	}
}

// TestConsumeTradeIsRaceFreeUnderConcurrentShardPressure exercises the
// exact scenario a real deployment has (multiple trade-WS shard goroutines
// calling consumeTrade concurrently) against small channels sized to
// guarantee real contention. Run under `go test -race`: the old design
// incremented a plain uint64 counter directly inside consumeTrade itself,
// which a concurrent health read could race with; the current design never
// touches app.stats from this goroutine at all.
func TestConsumeTradeIsRaceFreeUnderConcurrentShardPressure(t *testing.T) {
	_ = t
	app := &application{
		tradeEvents: make(chan bybit.PublicTrade, 4),
		tradeDrops:  make(chan bybit.PublicTrade, 4),
	}
	var wg sync.WaitGroup
	for shard := 0; shard < 4; shard++ {
		wg.Add(1)
		go func(shard int) {
			defer wg.Done()
			for i := 0; i < 200; i++ {
				_ = app.consumeTrade(context.Background(), bybit.PublicTrade{
					Symbol: "BTCUSDT", TradeID: fmt.Sprintf("%d-%d", shard, i),
				})
			}
		}(shard)
	}
	drainDone := make(chan struct{})
	go func() {
		defer close(drainDone)
		for {
			select {
			case <-app.tradeEvents:
			case <-app.tradeDrops:
			case <-time.After(50 * time.Millisecond):
				return
			}
		}
	}()
	wg.Wait()
	<-drainDone
	_ = app.tradeDropsLost.Load()
}

func TestCheckTickerGapsMarksSilentSymbolOnceThenStopsUntilFreshObservation(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	start := time.Unix(1000, 0).UTC()
	app.tickerLastSeenAt["BTCUSDT"] = start

	app.checkTickerGaps(start.Add(tickerGapThreshold + time.Second))
	if app.stats.tickerGapTotal != 1 {
		t.Fatalf("tickerGapTotal = %d, want 1", app.stats.tickerGapTotal)
	}
	if !app.tickerGapMarked["BTCUSDT"] {
		t.Fatal("BTCUSDT should be recorded as an already-flagged gap")
	}

	// A second check without any fresh observation must not re-mark (and
	// re-count) the same still-ongoing gap.
	app.checkTickerGaps(start.Add(tickerGapThreshold + 2*time.Second))
	if app.stats.tickerGapTotal != 1 {
		t.Fatalf("tickerGapTotal = %d after a second check, want still 1 (idempotent)", app.stats.tickerGapTotal)
	}
}

func TestCheckTickerGapsIgnoresRecentlySeenSymbols(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	now := time.Unix(1000, 0).UTC()
	app.tickerLastSeenAt["BTCUSDT"] = now

	app.checkTickerGaps(now.Add(time.Second))
	if app.stats.tickerGapTotal != 0 {
		t.Fatalf("tickerGapTotal = %d, want 0 for a symbol seen a second ago", app.stats.tickerGapTotal)
	}
}

func TestApplicationHandleTickerMessageClearsAnOpenGapMark(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.tickerGapMarked["BTCUSDT"] = true

	last := "100"
	raw, err := json.Marshal(bybit.TickerEvent{
		SchemaVersion: 1, Source: "bybit", Symbol: "BTCUSDT", TS: 60_000,
		LastPrice: &last, StreamSessionID: "session-a",
	})
	if err != nil {
		t.Fatal(err)
	}
	app.handleTickerMessage(&nats.Msg{Data: raw}, time.UnixMilli(60_000))

	if app.tickerGapMarked["BTCUSDT"] {
		t.Fatal("a fresh observation must clear an open gap mark")
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
	derivativesStale := momentumcapture.Health{StartedAt: time.Unix(0, 0), UpdatedAt: time.Unix(181, 0)}
	if got := deriveHealthStatus(derivativesStale); got != "degraded_derivatives_stale" {
		t.Fatalf("derivatives stale status = %q", got)
	}

	stale := momentumcapture.Health{UniverseStale: true}
	if got := deriveHealthStatus(stale); got != "degraded_universe_stale" {
		t.Fatalf("status = %q, want degraded_universe_stale", got)
	}

	feedInterrupted := stale
	feedInterrupted.NATSDisconnectTotal = 1
	if got := deriveHealthStatus(feedInterrupted); got != "degraded_feed_interrupted" {
		t.Fatalf("status = %q, want degraded_feed_interrupted to outrank universe staleness", got)
	}

	queuePressure := feedInterrupted
	queuePressure.InputQueueDropsTotal = 1
	if got := deriveHealthStatus(queuePressure); got != "degraded_queue_pressure" {
		t.Fatalf("status = %q, want degraded_queue_pressure to outrank universe staleness", got)
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

func TestDeriveHealthStatusFlagsQueuePressureFromWriterBacklogDepth(t *testing.T) {
	t.Parallel()
	health := momentumcapture.Health{
		WriterQueueDepth: int(float64(momentumcapture.MaxPendingBars)*queuePressureWarnFraction) + 1,
	}
	if got := deriveHealthStatus(health); got != "degraded_queue_pressure" {
		t.Fatalf("status = %q, want degraded_queue_pressure once the writer backlog crosses the warn fraction", got)
	}
}

// closedChan returns an already-closed channel, standing in for a
// production tradesDone whose real trade-WS goroutine has already exited:
// these shutdown tests have no such goroutine at all.
func closedChan() <-chan struct{} {
	ch := make(chan struct{})
	close(ch)
	return ch
}

// drainWriterInbox mimics runWriter's own shutdown behavior (range over
// inbox until the loop goroutine closes it, then report result) without a
// real Writer/database, for tests that only care about shutdown's own
// sequencing, not the writer goroutine's.
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

	if err := app.shutdown(closedChan(), writerDone); err == nil {
		t.Fatal("shutdown must propagate a failed final writer flush, not report success")
	}
}

func TestApplicationShutdownDrainsBufferedTradeBeforeFinalFlush(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.tradeEvents = make(chan bybit.PublicTrade, 1)
	app.tradeEvents <- bybit.PublicTrade{
		Symbol: "BTCUSDT", TradeID: "1", Side: "buy",
		EventAt: time.Unix(60, 0).UTC(), ReceivedAt: time.Unix(60, 0).UTC(),
		Price: 100, Size: 1,
	}

	writerDone := make(chan error, 1)
	go drainWriterInbox(app.writerInbox, writerDone, nil)

	if err := app.shutdown(closedChan(), writerDone); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	if app.stats.tradesAcceptedTotal != 1 {
		t.Fatal("shutdown must drain a trade already buffered in tradeEvents before its final flush, not abandon it")
	}
}

func TestApplicationShutdownDrainsProducersRegardlessOfArrivalOrder(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.lifecycleEvents = make(chan bybit.TradeLifecycleEvent, 1)
	app.lifecycleEvents <- bybit.TradeLifecycleEvent{
		ShardSessionID: "shard-1", Symbols: []string{"BTCUSDT"},
		DisconnectedAt: time.Unix(60, 0).UTC(),
	}
	app.natsFaults = make(chan natsFault, 1)
	app.natsFaults <- natsFault{kind: "reconnected", at: time.Unix(61, 0).UTC()}

	writerDone := make(chan error, 1)
	go drainWriterInbox(app.writerInbox, writerDone, nil)

	if err := app.shutdown(closedChan(), writerDone); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	if app.stats.tradeLifecycleTotal != 1 {
		t.Fatal("shutdown must drain a buffered lifecycle event before its final flush")
	}
	if app.stats.natsReconnectTotal != 1 {
		t.Fatal("shutdown must drain a buffered NATS fault before its final flush")
	}
}

func TestApplicationShutdownAppliesPendingLossLatchesBeforeFinalFlush(t *testing.T) {
	t.Parallel()
	app := newTestApplication([]string{"BTCUSDT"})
	app.tradeVisibilityLost.markOnce(time.Unix(60, 0).UTC())

	writerDone := make(chan error, 1)
	go drainWriterInbox(app.writerInbox, writerDone, nil)

	if err := app.shutdown(closedChan(), writerDone); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	if app.stats.lastDiscontinuityFor != "*" {
		t.Fatal("a loss latched before shutdown must still be applied as a conservative discontinuity mark")
	}
}

func TestLossLatchMarksOnceAndConsumesOnce(t *testing.T) {
	t.Parallel()
	var latch lossLatch
	if _, ok := latch.consume(); ok {
		t.Fatal("a fresh latch must not report a loss")
	}

	first := time.Unix(100, 0).UTC()
	latch.markOnce(first)
	latch.markOnce(time.Unix(200, 0).UTC()) // must not overwrite the first mark

	at, ok := latch.consume()
	if !ok || !at.Equal(first) {
		t.Fatalf("consume() = (%v, %v), want (%v, true)", at, ok, first)
	}

	if _, ok := latch.consume(); ok {
		t.Fatal("consuming twice in a row must report nothing the second time")
	}

	latch.markOnce(time.Unix(300, 0).UTC())
	if _, ok := latch.consume(); !ok {
		t.Fatal("a latch must be markable again after being consumed")
	}
}

// TestRunWriterFlushesAndReportsOnInboxClose exercises the real runWriter
// against a real (if db-less) momentumcapture.Writer: closing inbox is the
// ONLY termination signal (see runWriter's own doc comment on why this,
// not a separate shutdown channel, is what guarantees every bar sent
// before the close is enqueued before the final flush runs). A nil pool is
// safe here because nothing is ever enqueued, so Flush never touches it.
func TestRunWriterFlushesAndReportsOnInboxClose(t *testing.T) {
	t.Parallel()
	writer := momentumcapture.NewWriter(nil, "bybit", "linear", "hash")
	inbox := make(chan []momentum.Bar, 1)
	done := make(chan error, 1)

	go runWriter(writer, inbox, done)
	close(inbox)

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runWriter reported %v, want nil for an empty final flush", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("runWriter did not report completion after inbox was closed")
	}
}

func TestDeriveHealthStatusFlagsFeedInterruptedFromNATSOrTickerGapSignals(t *testing.T) {
	t.Parallel()
	cases := map[string]momentumcapture.Health{
		"disconnect":    {NATSDisconnectTotal: 1},
		"slow_consumer": {NATSSlowConsumerTotal: 1},
		"dropped":       {NATSDroppedTotal: 1},
		"ticker_gap":    {TickerGapTotal: 1},
	}
	for name, health := range cases {
		t.Run(name, func(t *testing.T) {
			if got := deriveHealthStatus(health); got != "degraded_feed_interrupted" {
				t.Fatalf("status = %q, want degraded_feed_interrupted for %s", got, name)
			}
		})
	}
}

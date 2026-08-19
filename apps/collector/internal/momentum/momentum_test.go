package momentum

import (
	"encoding/json"
	"math"
	"sort"
	"strconv"
	"testing"
	"time"
)

func at(offsetSeconds int) time.Time {
	return time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC).Add(time.Duration(offsetSeconds) * time.Second)
}

func trade(side Side, notional float64, when time.Time, id string) Trade {
	return Trade{
		Symbol:     "AKEUSDT",
		Side:       side,
		Price:      1,
		Size:       notional,
		EventAt:    when,
		ReceivedAt: when,
		TradeID:    id,
	}
}

func tickerAt(when time.Time, opts ...func(*TickerObservation)) TickerObservation {
	o := TickerObservation{Symbol: "AKEUSDT", EventAt: when, ObservedAt: when}
	for _, opt := range opts {
		opt(&o)
	}
	return o
}

func withOI(oi, value float64, eventAt, observedAt time.Time) func(*TickerObservation) {
	return func(o *TickerObservation) {
		o.OpenInterest, o.OpenInterestEventAt, o.OpenInterestObservedAt = f(oi), &eventAt, &observedAt
		o.OpenInterestValue = f(value)
		o.OpenInterestValueEventAt, o.OpenInterestValueObservedAt = &eventAt, &observedAt
	}
}

func f(v float64) *float64 { return &v }

// --- validation ---

func TestAddTradeRejectsInvalidInput(t *testing.T) {
	t.Parallel()
	valid := Trade{Symbol: "AKEUSDT", Side: SideBuy, Price: 1, Size: 1, EventAt: at(0), ReceivedAt: at(0), TradeID: "id"}
	huge := math.MaxFloat64 / 2
	overflowing := withSize(withPrice(valid, huge), huge)
	cases := map[string]Trade{
		"empty symbol":        withSymbol(valid, ""),
		"zero EventAt":        withEventAt(valid, time.Time{}),
		"zero ReceivedAt":     withReceivedAt(valid, time.Time{}),
		"empty TradeID":       withTradeID(valid, ""),
		"unknown side":        withSide(valid, "BUY"),
		"non-positive price":  withPrice(valid, 0),
		"non-positive size":   withSize(valid, -1),
		"NaN price":           withPrice(valid, math.NaN()),
		"infinite size":       withSize(valid, math.Inf(1)),
		"overflowing product": overflowing,
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			e := New()
			if _, err := e.AddTrade(tc); err != ErrInvalidTrade {
				t.Fatalf("err = %v, want ErrInvalidTrade", err)
			}
		})
	}
}

func withSymbol(t Trade, v string) Trade        { t.Symbol = v; return t }
func withEventAt(t Trade, v time.Time) Trade    { t.EventAt = v; return t }
func withReceivedAt(t Trade, v time.Time) Trade { t.ReceivedAt = v; return t }
func withTradeID(t Trade, v string) Trade       { t.TradeID = v; return t }
func withSide(t Trade, v Side) Trade            { t.Side = v; return t }
func withPrice(t Trade, v float64) Trade        { t.Price = v; return t }
func withSize(t Trade, v float64) Trade         { t.Size = v; return t }

func TestAddTickerObservationRejectsInvalidInput(t *testing.T) {
	t.Parallel()
	eventAt, observedAt := at(0), at(0)
	cases := map[string]TickerObservation{
		"empty symbol":               {Symbol: "", EventAt: at(0), ObservedAt: at(0)},
		"zero EventAt":               {Symbol: "AKEUSDT", EventAt: time.Time{}, ObservedAt: at(0)},
		"zero ObservedAt":            {Symbol: "AKEUSDT", EventAt: at(0), ObservedAt: time.Time{}},
		"negative OI":                {Symbol: "AKEUSDT", EventAt: at(0), ObservedAt: at(0), OpenInterest: f(-1), OpenInterestEventAt: &eventAt, OpenInterestObservedAt: &observedAt},
		"NaN OI value":               {Symbol: "AKEUSDT", EventAt: at(0), ObservedAt: at(0), OpenInterestValue: f(math.NaN()), OpenInterestValueEventAt: &eventAt, OpenInterestValueObservedAt: &observedAt},
		"zero last price":            {Symbol: "AKEUSDT", EventAt: at(0), ObservedAt: at(0), LastPrice: f(0)},
		"negative bid":               {Symbol: "AKEUSDT", EventAt: at(0), ObservedAt: at(0), BidPrice: f(-1)},
		"OI value with no timestamp": {Symbol: "AKEUSDT", EventAt: at(0), ObservedAt: at(0), OpenInterest: f(100)},
		"OI timestamp with no value": {Symbol: "AKEUSDT", EventAt: at(0), ObservedAt: at(0), OpenInterestEventAt: &eventAt, OpenInterestObservedAt: &observedAt},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			e := New()
			if _, err := e.AddTickerObservation(tc); err != ErrInvalidTickerObservation {
				t.Fatalf("err = %v, want ErrInvalidTickerObservation", err)
			}
		})
	}
}

// --- histogram ---

func TestHistogramBucketsAreNonCumulativeAndBoundaryCorrect(t *testing.T) {
	t.Parallel()
	e := New()
	amounts := []float64{999, 1_000, 3_000}
	for i, amount := range amounts {
		tr := trade(SideBuy, amount, at(i), "id"+string(rune('a'+i)))
		if _, err := e.AddTrade(tr); err != nil {
			t.Fatalf("add trade: %v", err)
		}
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(120))
	if len(closed) == 0 {
		t.Fatal("expected a closed bar")
	}
	hist := closed[0].Buy.Histogram
	if hist[0].Count != 1 || hist[0].NotionalUSD != 999 {
		t.Fatalf("bucket 0 (<1000) = %+v, want count=1 notional=999", hist[0])
	}
	if hist[1].Count != 1 || hist[1].NotionalUSD != 1_000 {
		t.Fatalf("bucket 1 ([1000,2500)) = %+v, want count=1 notional=1000", hist[1])
	}
	if hist[2].Count != 1 || hist[2].NotionalUSD != 3_000 {
		t.Fatalf("bucket 2 ([2500,5000)) = %+v, want count=1 notional=3000", hist[2])
	}
}

func TestHistogramLastBucketIsOpenEndedAndJSONMarshalable(t *testing.T) {
	t.Parallel()
	e := New()
	if _, err := e.AddTrade(trade(SideBuy, 10_000_000, at(0), "id1")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(120))
	last := closed[0].Buy.Histogram[len(closed[0].Buy.Histogram)-1]
	if last.UpperBoundUSD != nil {
		t.Fatalf("last bucket upper bound = %v, want nil (open-ended)", *last.UpperBoundUSD)
	}
	if _, err := json.Marshal(closed[0]); err != nil {
		t.Fatalf("a Bar with an open-ended histogram bucket must be JSON-marshalable: %v", err)
	}
}

// --- top-K ---

func TestTopKKeepsLargestDescendingCappedAtFive(t *testing.T) {
	t.Parallel()
	e := New()
	amounts := []float64{100, 500, 50, 900, 300, 700, 200}
	for i, amount := range amounts {
		tr := trade(SideBuy, amount, at(i), "id"+string(rune('a'+i)))
		if _, err := e.AddTrade(tr); err != nil {
			t.Fatalf("add trade: %v", err)
		}
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(120))
	top := closed[0].Buy.TopNotionalsUSD
	want := []float64{900, 700, 500, 300, 200}
	if len(top) != len(want) {
		t.Fatalf("top-K length = %d, want %d: %v", len(top), len(want), top)
	}
	for i := range want {
		if top[i] != want[i] {
			t.Fatalf("top-K = %v, want %v", top, want)
		}
	}
}

// --- burst metrics, including out-of-order arrival ---

func TestBurstWindowFindsDenseCluster(t *testing.T) {
	t.Parallel()
	e := New()
	for i, offset := range []int{0, 3, 5} {
		tr := trade(SideBuy, 100, at(offset), "cluster"+string(rune('a'+i)))
		if _, err := e.AddTrade(tr); err != nil {
			t.Fatalf("add trade: %v", err)
		}
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(120))
	if closed[0].Buy.Max10sNotionalUSD != 300 {
		t.Fatalf("max 10s notional = %v, want 300", closed[0].Buy.Max10sNotionalUSD)
	}
}

func TestBurstWindowCrossesAMinuteBoundary(t *testing.T) {
	t.Parallel()
	e := New()
	if _, err := e.AddTrade(trade(SideBuy, 60_000, at(52), "a")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if _, err := e.AddTrade(trade(SideBuy, 70_000, at(63), "b")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	closedSecond := e.MarkTickerDiscontinuity("AKEUSDT", at(130))
	if len(closedSecond) != 1 {
		t.Fatalf("expected exactly 1 more closed bar, got %d", len(closedSecond))
	}
	if closedSecond[0].Buy.Max30sNotionalUSD != 130_000 {
		t.Fatalf("12:01 bar max 30s = %v, want 130000", closedSecond[0].Buy.Max30sNotionalUSD)
	}
	if closedSecond[0].Buy.Max10sNotionalUSD != 70_000 {
		t.Fatalf("12:01 bar max 10s = %v, want 70000", closedSecond[0].Buy.Max10sNotionalUSD)
	}
}

func TestBurstWindowIsNotCorruptedByOutOfOrderArrival(t *testing.T) {
	t.Parallel()
	e := New()
	// A trade at t=50s arrives first; a trade at t=0s arrives after it
	// (late relative to arrival order, but not late enough relative to the
	// bar's own bucket start to be dropped). They are 50 seconds apart, so
	// no 10s or 30s window can ever contain both.
	if _, err := e.AddTrade(trade(SideBuy, 100, at(50), "arrived-first")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if _, err := e.AddTrade(trade(SideBuy, 200, at(0), "arrived-second")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(120))
	buy := closed[0].Buy
	if buy.Max30sNotionalUSD >= 300 {
		t.Fatalf(
			"max 30s notional = %v: must not combine two trades 50s apart just because they were inserted out of arrival order",
			buy.Max30sNotionalUSD,
		)
	}
	if buy.Max30sNotionalUSD != 200 {
		t.Fatalf("max 30s notional = %v, want 200 (the larger of the two trades alone)", buy.Max30sNotionalUSD)
	}
}

func TestBurstTrackerMatchesExactReferenceAcrossOrderedAndOutOfOrderEvents(t *testing.T) {
	t.Parallel()
	tracker := burstTracker{}
	var reference []tradeRecord
	var gotMax10, gotMax30, wantMax10, wantMax30 float64
	start := at(0)

	for index := range 2_000 {
		offset := time.Duration(index*17) * time.Millisecond
		if index%17 == 0 {
			offset -= time.Duration(index%41) * time.Second
		}
		eventAt := start.Add(offset)
		notional := float64(index%97 + 1)

		got10, got30 := tracker.add(eventAt, notional)
		gotMax10 = max(gotMax10, got10)
		gotMax30 = max(gotMax30, got30)
		insertSortedByEventAt(&reference, tradeRecord{eventAt: eventAt, notionalUSD: notional})
		want10 := slidingWindowMaxSum(reference, burstWindow10s)
		want30 := slidingWindowMaxSum(reference, burstWindow30s)
		wantMax10 = max(wantMax10, want10)
		wantMax30 = max(wantMax30, want30)
		if gotMax10 != wantMax10 || gotMax30 != wantMax30 {
			t.Fatalf(
				"event %d at %v: burst maxima = (%v, %v), exact reference = (%v, %v); records=%d left10=%d left30=%d stored=(%v,%v) reference=%d",
				index,
				eventAt.Sub(start),
				gotMax10,
				gotMax30,
				wantMax10,
				wantMax30,
				len(tracker.records),
				tracker.left10,
				tracker.left30,
				tracker.sum10,
				tracker.sum30,
				len(reference),
			)
		}

		latest := reference[len(reference)-1].eventAt
		cutoff := latest.Add(-burstWindow30s)
		first := sort.Search(len(reference), func(position int) bool {
			return !reference[position].eventAt.Before(cutoff)
		})
		reference = reference[first:]
	}
}

func TestEngineHandlesBoundedDenseBurstWithoutLosingStatistics(t *testing.T) {
	t.Parallel()
	const tradeCount = 20_000
	engine := New()
	start := at(0)
	for index := range tradeCount {
		eventAt := start.Add(time.Duration(index) * time.Millisecond)
		_, err := engine.AddTrade(Trade{
			Symbol:     "BURSTUSDT",
			Side:       SideBuy,
			Price:      1,
			Size:       100,
			EventAt:    eventAt,
			ReceivedAt: eventAt,
			TradeID:    strconv.Itoa(index),
		})
		if err != nil {
			t.Fatalf("add trade %d: %v", index, err)
		}
	}

	bars := engine.Flush(start.Add(time.Minute + time.Second))
	if len(bars) != 1 {
		t.Fatalf("closed bars = %d, want 1", len(bars))
	}
	buy := bars[0].Buy
	if buy.TradeCount != tradeCount || buy.TotalNotionalUSD != tradeCount*100 {
		t.Fatalf("dense burst totals = count %d notional %v", buy.TradeCount, buy.TotalNotionalUSD)
	}
	if buy.Max10sNotionalUSD != 1_000_100 {
		t.Fatalf("max 10s notional = %v, want 1000100 with inclusive endpoints", buy.Max10sNotionalUSD)
	}
	if buy.Max30sNotionalUSD != tradeCount*100 {
		t.Fatalf("max 30s notional = %v, want %d", buy.Max30sNotionalUSD, tradeCount*100)
	}
}

// --- block/RPI separation, buy/sell independence, dedup, late trades ---

func TestBlockAndRPITradesAreExcludedFromOrdinaryStats(t *testing.T) {
	t.Parallel()
	e := New()
	normal := trade(SideBuy, 100, at(0), "normal")
	block := trade(SideBuy, 1_000_000, at(1), "block")
	block.IsBlockTrade = true
	rpi := trade(SideBuy, 500_000, at(2), "rpi")
	rpi.IsRPI = true
	for _, tr := range []Trade{normal, block, rpi} {
		if _, err := e.AddTrade(tr); err != nil {
			t.Fatalf("add trade: %v", err)
		}
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(120))
	buy := closed[0].Buy
	if buy.TotalNotionalUSD != 100 || buy.TradeCount != 1 {
		t.Fatalf("ordinary flow = %+v, want only the 100-notional normal trade", buy)
	}
	if buy.BlockTradeCount != 1 || buy.BlockTradeNotionalUSD != 1_000_000 {
		t.Fatalf("block stats = %+v", buy)
	}
	if buy.RPITradeCount != 1 || buy.RPITradeNotionalUSD != 500_000 {
		t.Fatalf("RPI stats = %+v", buy)
	}
	if closed[0].TradeCount != 3 {
		t.Fatalf("bar-level trade count = %d, want 3", closed[0].TradeCount)
	}
}

func TestBuyAndSellAreAggregatedIndependently(t *testing.T) {
	t.Parallel()
	e := New()
	if _, err := e.AddTrade(trade(SideBuy, 100, at(0), "buy-1")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if _, err := e.AddTrade(trade(SideSell, 250, at(1), "sell-1")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(120))
	bar := closed[0]
	if bar.Buy.TotalNotionalUSD != 100 || bar.Sell.TotalNotionalUSD != 250 {
		t.Fatalf("buy/sell not independent: %+v / %+v", bar.Buy, bar.Sell)
	}
}

func TestDuplicateTradeIDIsDroppedAndCounted(t *testing.T) {
	t.Parallel()
	e := New()
	tr := trade(SideBuy, 100, at(0), "dup-1")
	if _, err := e.AddTrade(tr); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if _, err := e.AddTrade(tr); err != nil {
		t.Fatalf("add duplicate trade: %v", err)
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(120))
	if closed[0].Buy.TradeCount != 1 || closed[0].DuplicateTradesDropped != 1 {
		t.Fatalf("duplicate handling wrong: %+v", closed[0])
	}
}

func TestLateTradeAfterBarCloseIsDroppedAndReportedOnNextBar(t *testing.T) {
	t.Parallel()
	e := New()
	if _, err := e.AddTrade(trade(SideBuy, 100, at(0), "on-time")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if _, err := e.AddTrade(trade(SideBuy, 200, at(65), "next-minute")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if _, err := e.AddTrade(trade(SideBuy, 999, at(30), "late")); err != nil {
		t.Fatalf("add late trade: %v", err)
	}
	secondClosed := e.MarkTickerDiscontinuity("AKEUSDT", at(130))
	if len(secondClosed) != 1 || secondClosed[0].LateTradesDropped != 1 {
		t.Fatalf("late trade not reported correctly: %+v", secondClosed)
	}
}

// --- feed lifecycle: health vs per-bar interruption ---

func TestRecoveryMidBarDoesNotRepairTheAlreadyDamagedMinute(t *testing.T) {
	t.Parallel()
	e := New()
	if closed := e.MarkTradesDiscontinuity("AKEUSDT", at(10)); len(closed) != 0 {
		t.Fatal("marking mid-minute should not itself close a bar")
	}
	// The feed recovers 10 seconds later, well within the same minute.
	closed, err := e.AddTrade(trade(SideBuy, 100, at(20), "recovered-mid-minute"))
	if err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if len(closed) != 0 {
		t.Fatalf("no bar should close yet, got %d", len(closed))
	}
	// Roll into the next minute to close the damaged one.
	closed, err = e.AddTrade(trade(SideBuy, 100, at(65), "next-minute"))
	if err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if len(closed) != 1 {
		t.Fatalf("expected 1 closed bar, got %d", len(closed))
	}
	if closed[0].TradesComplete {
		t.Fatal("the 12:00 bar was damaged by a real outage for part of its window and must stay TradesComplete=false even though the feed recovered before the bar closed")
	}
}

func TestLateOrDuplicateTradeMustNotSignalRecovery(t *testing.T) {
	t.Parallel()
	e := New()
	tr := trade(SideBuy, 100, at(0), "a")
	if _, err := e.AddTrade(tr); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if _, err := e.AddTrade(trade(SideBuy, 100, at(65), "b")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if closed := e.MarkTradesDiscontinuity("AKEUSDT", at(70)); len(closed) != 0 {
		t.Fatal("marking mid-minute should not itself close a bar")
	}
	// A duplicate of an already-accepted trade, and a late trade for the
	// already-closed first bar, both arrive next. Neither is a genuinely
	// accepted new trade, so neither should signal recovery.
	if _, err := e.AddTrade(trade(SideBuy, 100, at(65), "b")); err != nil {
		t.Fatalf("add duplicate: %v", err)
	}
	if _, err := e.AddTrade(trade(SideBuy, 100, at(30), "late-for-first-bar")); err != nil {
		t.Fatalf("add late trade: %v", err)
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(130))
	if len(closed) != 1 {
		t.Fatalf("expected 1 closed bar, got %d", len(closed))
	}
	if closed[0].TradesComplete {
		t.Fatal("a duplicate or late trade must not signal recovery: the bar must stay TradesComplete=false")
	}
}

func TestHealthyButQuietMinuteIsComplete(t *testing.T) {
	t.Parallel()
	e := New()
	if _, err := e.AddTrade(trade(SideBuy, 100, at(0), "a")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if _, err := e.AddTickerObservation(tickerAt(at(0))); err != nil {
		t.Fatalf("add observation: %v", err)
	}
	// Advance one minute at a time (as a real polling Flush loop would),
	// not in one large jump: a jump spanning more than one minute is
	// itself treated as a process-level stall (see
	// TestGapProducesSyntheticIncompleteBarsForEverySkippedMinute) and is
	// a deliberately different scenario from an ordinary quiet minute.
	if closed := e.Flush(at(65)); len(closed) != 1 {
		t.Fatalf("expected 1 closed bar, got %d", len(closed))
	}
	// A full minute passes with zero trades and zero ticker observations,
	// but nothing was ever marked broken: this must read as a healthy,
	// quiet minute, not an outage.
	closed := e.Flush(at(130))
	if len(closed) != 1 {
		t.Fatalf("expected 1 closed bar, got %d", len(closed))
	}
	quietBar := closed[0]
	if !quietBar.Complete {
		t.Fatalf("a healthy but quiet minute must be Complete=true, not treated as an outage: %+v", quietBar)
	}
	if quietBar.TickerObservedThisMinute {
		t.Fatal("no observation actually landed in the quiet minute; TickerObservedThisMinute must be false")
	}
}

func TestLastKnownValuesCarryForwardWithOriginalTimestamps(t *testing.T) {
	t.Parallel()
	e := New()
	oiEventAt, oiObservedAt := at(0), at(0)
	if _, err := e.AddTickerObservation(tickerAt(at(0),
		func(o *TickerObservation) { o.BidPrice, o.AskPrice = f(10), f(11) },
		withOI(1_000_000, 12_345, oiEventAt, oiObservedAt),
	)); err != nil {
		t.Fatalf("add observation: %v", err)
	}
	if closed := e.Flush(at(65)); len(closed) != 1 {
		t.Fatalf("expected 1 closed bar, got %d", len(closed))
	}
	// A full quiet minute follows with no new observation at all.
	closed := e.Flush(at(130))
	if len(closed) != 1 {
		t.Fatalf("expected 1 closed bar, got %d", len(closed))
	}
	quietBar := closed[0]
	if quietBar.LastBidPrice == nil || *quietBar.LastBidPrice != 10 {
		t.Fatalf("last bid must carry forward: %v", quietBar.LastBidPrice)
	}
	if quietBar.OpenInterest == nil || *quietBar.OpenInterest != 1_000_000 {
		t.Fatalf("OI must carry forward: %v", quietBar.OpenInterest)
	}
	if quietBar.OpenInterestEventAt == nil || !quietBar.OpenInterestEventAt.Equal(oiEventAt) {
		t.Fatalf(
			"carried-forward OI must keep its ORIGINAL event timestamp, got %v, want %v",
			quietBar.OpenInterestEventAt, oiEventAt,
		)
	}
	if quietBar.OpenPrice != nil {
		t.Fatalf("OHLC must NOT be fabricated for a bar with no real price tick: %v", quietBar.OpenPrice)
	}
}

// --- rollover / gap synthesis ---

func TestGapProducesSyntheticIncompleteBarsForEverySkippedMinute(t *testing.T) {
	t.Parallel()
	e := New()
	if _, err := e.AddTrade(trade(SideBuy, 100, at(0), "a")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	closed, err := e.AddTrade(trade(SideBuy, 100, at(5*60), "b"))
	if err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if len(closed) != 5 {
		t.Fatalf("expected 5 closed bars, got %d", len(closed))
	}
	for i, bar := range closed[1:] {
		if bar.Complete {
			t.Fatalf("gap-filler bar %d must be incomplete regardless of last known health: %+v", i, bar)
		}
	}
}

func TestCappedBackfillRecordsAnExplicitUnbackfilledGap(t *testing.T) {
	t.Parallel()
	e := New()
	if _, err := e.AddTrade(trade(SideBuy, 100, at(0), "a")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	farFuture := at(0).Add(400 * time.Minute)
	closed, err := e.AddTrade(Trade{
		Symbol: "AKEUSDT", Side: SideBuy, Price: 1, Size: 100,
		EventAt: farFuture, ReceivedAt: farFuture, TradeID: "b",
	})
	if err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if len(closed) > maxSyntheticBackfill+1 {
		t.Fatalf("expected at most maxSyntheticBackfill+1 closed bars, got %d", len(closed))
	}
	final := e.MarkTickerDiscontinuity("AKEUSDT", farFuture.Add(time.Minute))
	if len(final) != 1 {
		t.Fatalf("expected 1 more closed bar, got %d", len(final))
	}
	wantUnbackfilled := 399 - maxSyntheticBackfill
	if final[0].UnbackfilledGapMinutes != wantUnbackfilled {
		t.Fatalf("unbackfilled gap minutes = %d, want %d", final[0].UnbackfilledGapMinutes, wantUnbackfilled)
	}
	if final[0].UnbackfilledGapFrom == nil || final[0].UnbackfilledGapTo == nil {
		t.Fatal("expected an explicit unbackfilled gap range")
	}
}

// --- diagnostics ---

func TestTradeDiagnosticsTrackLagAndSequence(t *testing.T) {
	t.Parallel()
	e := New()
	if _, err := e.AddTrade(Trade{
		Symbol: "AKEUSDT", Side: SideBuy, Price: 1, Size: 100,
		EventAt: at(0), ReceivedAt: at(0).Add(50 * time.Millisecond), TradeID: "a", Seq: 100,
	}); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if _, err := e.AddTrade(Trade{
		Symbol: "AKEUSDT", Side: SideBuy, Price: 1, Size: 100,
		EventAt: at(1), ReceivedAt: at(1).Add(200 * time.Millisecond), TradeID: "b", Seq: 90,
	}); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(120))
	bar := closed[0]
	if bar.TradeLagCount != 2 || bar.TradeLagSumMs != 250 || bar.TradeLagMaxMs != 200 {
		t.Fatalf("lag diagnostics wrong: count=%d sum=%d max=%d", bar.TradeLagCount, bar.TradeLagSumMs, bar.TradeLagMaxMs)
	}
	if bar.MinTradeSeq == nil || *bar.MinTradeSeq != 90 || bar.MaxTradeSeq == nil || *bar.MaxTradeSeq != 100 {
		t.Fatalf("seq range wrong: min=%v max=%v", bar.MinTradeSeq, bar.MaxTradeSeq)
	}
	if bar.OutOfOrderTradeCount != 1 {
		t.Fatalf("out-of-order count = %d, want 1 (seq regressed from 100 to 90)", bar.OutOfOrderTradeCount)
	}
	if bar.FirstTradeEventAt == nil || !bar.FirstTradeEventAt.Equal(at(0)) {
		t.Fatalf("first trade event at = %v, want %v", bar.FirstTradeEventAt, at(0))
	}
	if bar.LastTradeEventAt == nil || !bar.LastTradeEventAt.Equal(at(1)) {
		t.Fatalf("last trade event at = %v, want %v", bar.LastTradeEventAt, at(1))
	}
}

func TestTickerDiagnosticsTrackLag(t *testing.T) {
	t.Parallel()
	e := New()
	if _, err := e.AddTickerObservation(TickerObservation{
		Symbol: "AKEUSDT", LastPrice: f(1),
		EventAt: at(0), ObservedAt: at(0).Add(100 * time.Millisecond),
	}); err != nil {
		t.Fatalf("add observation: %v", err)
	}
	closed := e.MarkTickerDiscontinuity("AKEUSDT", at(120))
	bar := closed[0]
	if bar.TickerLagCount != 1 || bar.TickerLagSumMs != 100 || bar.TickerLagMaxMs != 100 {
		t.Fatalf("ticker lag diagnostics wrong: %+v", bar)
	}
}

// --- Flush ---

func TestFlushForceClosesElapsedBarsWithoutANewEvent(t *testing.T) {
	t.Parallel()
	e := New()
	if _, err := e.AddTrade(trade(SideBuy, 100, at(0), "a")); err != nil {
		t.Fatalf("add trade: %v", err)
	}
	if closed := e.Flush(at(30)); len(closed) != 0 {
		t.Fatalf("flush before the minute elapses should close nothing, got %d", len(closed))
	}
	closed := e.Flush(at(90))
	if len(closed) != 1 || closed[0].Buy.TradeCount != 1 {
		t.Fatalf("flush should close exactly the real bar, got %+v", closed)
	}
}

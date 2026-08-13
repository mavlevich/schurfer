// Package momentum is a pure, in-memory aggregation engine for the Bybit
// early-momentum capture line (ROADMAP "Active course" item 5). It has no
// dependency on any specific exchange's wire format, NATS, a database, or
// Docker: it consumes two small, decoupled domain types (Trade,
// TickerObservation) and produces per-symbol, per-minute Bars. Wiring real
// Bybit WebSocket streams into these inputs, and persisting the resulting
// Bars, is deliberately left to the capture service (a separate PR), so
// this package's logic is testable as ordinary Go values with no network
// or storage involved.
//
// Design decisions carried over from review of this line:
//
//   - Only fixed cumulative-size questions ("how much notional was at least
//     $50k") were considered first, but rejected: since raw trades are never
//     persisted (only these aggregates are), any threshold not tracked at
//     capture time is lost forever once the minute closes. A non-cumulative,
//     log-spaced notional histogram is tracked per side instead.
//   - A gap (a symbol with no data for one or more full minutes, most often
//     caused by a reconnect) is never silently skipped. Every minute in a
//     symbol's observed lifetime gets a Bar: either a real one, or a
//     synthetic empty one with Complete=false.
//   - Feed health and per-bar interruption are two different things, kept
//     deliberately separate. tickerHealthy/tradesHealthy on symbolState are
//     the CURRENT belief about whether each feed is connected, updated by
//     Mark*Discontinuity (false) and by a genuinely accepted Trade/
//     TickerObservation (true). barTickerInterrupted/barTradesInterrupted
//     are per-bar and sticky: once a bar's window has seen either feed
//     unhealthy at any point, that bar stays marked interrupted for its own
//     lifetime no matter how quickly the feed recovers. A trade arriving 5
//     seconds into a 10-second outage does not retroactively repair the 55
//     already-damaged seconds of that minute; only bars opened AFTER
//     recovery start out healthy.
//   - Completeness is a function of feed health, not of whether a symbol
//     happened to trade or tick that particular minute: Bybit's ~100ms push
//     frequency is not a per-symbol heartbeat guarantee, and treating
//     "market was quiet" as "feed was broken" would bias the very
//     accumulation hypothesis this capture line exists to test (quiet
//     price action while OI/flow build is exactly the pattern being
//     looked for). TickerObservedThisMinute is kept as a separate,
//     informational-only field for whoever wants to distinguish "healthy
//     and quiet" from "healthy and active".
package momentum

import (
	"errors"
	"math"
	"sort"
	"time"
)

// Side is the taker side of a trade.
type Side string

const (
	SideBuy  Side = "buy"
	SideSell Side = "sell"
)

// Trade is one taker-side public trade, decoupled from any exchange's wire
// format. EventAt is exchange time; ReceivedAt is the capture service's own
// receive time, kept for lag diagnostics. TradeID is required: a trade this
// package cannot deduplicate is worse than one it refuses outright, since a
// silently-undeduplicated retransmit corrupts every aggregate downstream.
// Seq is Bybit's own cross-sequence value (0 if unavailable), used only for
// a bounded regression counter, never for gap detection on its own (Bybit
// documents no contiguity guarantee for it).
type Trade struct {
	Symbol       string
	Side         Side
	Price        float64
	Size         float64
	EventAt      time.Time
	ReceivedAt   time.Time
	TradeID      string
	IsBlockTrade bool
	IsRPI        bool
	Seq          int64
}

// NotionalUSD is price times size. Bybit linear-USDT contracts are quoted
// and sized such that this is already a USD notional; a non-USDT-margined
// venue would need its own conversion before reaching this package.
func (t Trade) NotionalUSD() float64 { return t.Price * t.Size }

// TickerObservation is one reading from the ticker/OI feed (see the
// bybit-ticker-oi-contract-v1 PR). Every field beyond Symbol/EventAt/
// ObservedAt is optional (a nil pointer), matching that PR's contract: a
// delta can carry price with no OI, OI with no price change, or neither.
// For each of OpenInterest and OpenInterestValue, the value and its own
// EventAt/ObservedAt pair must be present together or not at all (enforced
// by AddTickerObservation): a value with no timestamp, or a timestamp with
// no value, is a contract violation this package refuses to accept
// silently, since it would corrupt the freshness discipline the ticker
// contract PR specifically added.
type TickerObservation struct {
	Symbol string

	LastPrice *float64
	BidPrice  *float64
	AskPrice  *float64

	OpenInterest           *float64
	OpenInterestEventAt    *time.Time
	OpenInterestObservedAt *time.Time

	OpenInterestValue           *float64
	OpenInterestValueEventAt    *time.Time
	OpenInterestValueObservedAt *time.Time

	EventAt    time.Time
	ObservedAt time.Time
}

// histogramBoundsUSD are the upper bounds of every bucket except the last,
// which is open-ended. Log-spaced and deliberately generous: an unrecorded
// boundary can never be reconstructed later, since raw trades are not kept.
var histogramBoundsUSD = []float64{
	1_000, 2_500, 5_000, 10_000, 25_000,
	50_000, 100_000, 250_000, 500_000, 1_000_000,
}

// HistogramBucket is one non-cumulative notional bucket. UpperBoundUSD is
// nil for the last, open-ended bucket: encoding/json cannot marshal
// math.Inf, and every Bar this package produces is expected to eventually
// be persisted.
type HistogramBucket struct {
	UpperBoundUSD *float64
	Count         int
	NotionalUSD   float64
}

func newHistogram() []HistogramBucket {
	buckets := make([]HistogramBucket, len(histogramBoundsUSD)+1)
	for i, bound := range histogramBoundsUSD {
		b := bound
		buckets[i] = HistogramBucket{UpperBoundUSD: &b}
	}
	buckets[len(buckets)-1] = HistogramBucket{UpperBoundUSD: nil}
	return buckets
}

func addToHistogram(buckets []HistogramBucket, notionalUSD float64) {
	for i := range buckets {
		if buckets[i].UpperBoundUSD == nil || notionalUSD < *buckets[i].UpperBoundUSD {
			buckets[i].Count++
			buckets[i].NotionalUSD += notionalUSD
			return
		}
	}
}

// topK is the number of largest individual trade notionals kept per side
// per bar, verbatim (not bucketed).
const topK = 5

const (
	burstWindow10s = 10 * time.Second
	burstWindow30s = 30 * time.Second
)

// SideStats aggregates one side (buy or sell) of trading activity within
// one minute for one symbol. Block-trade and RPI-flagged trades are kept
// out of the ordinary histogram/top-K/burst figures entirely.
type SideStats struct {
	TotalNotionalUSD  float64
	TradeCount        int
	Histogram         []HistogramBucket
	TopNotionalsUSD   []float64 // descending, at most topK entries
	Max10sNotionalUSD float64
	Max30sNotionalUSD float64

	BlockTradeCount       int
	BlockTradeNotionalUSD float64
	RPITradeCount         int
	RPITradeNotionalUSD   float64
}

func newSideStats() SideStats {
	return SideStats{Histogram: newHistogram()}
}

// Bar is one symbol's fully self-describing 1-minute aggregate.
type Bar struct {
	Symbol      string
	BucketStart time.Time // UTC, truncated to the minute

	// Price OHLC within the minute, from real ticker ticks only: a quiet
	// minute with no price observation leaves these nil rather than
	// fabricating unchanged OHLC. LastBidPrice/LastAskPrice/OpenInterest*
	// are state, not activity, and DO carry forward from the last known
	// reading (with that reading's own original timestamps) when a minute
	// has no fresh observation of its own; see advance.
	OpenPrice, HighPrice, LowPrice, ClosePrice *float64
	LastBidPrice, LastAskPrice                 *float64

	Buy  SideStats
	Sell SideStats

	OpenInterest           *float64
	OpenInterestEventAt    *time.Time
	OpenInterestObservedAt *time.Time

	OpenInterestValue           *float64
	OpenInterestValueEventAt    *time.Time
	OpenInterestValueObservedAt *time.Time

	// TickerObservedThisMinute is informational only, and must never be
	// used to derive completeness: Bybit's push frequency is not a
	// per-symbol heartbeat guarantee, and a quiet minute on a healthy
	// connection is normal, not an outage.
	TickerObservedThisMinute bool

	TradeCount             int
	DuplicateTradesDropped int
	// LateTradesDropped counts trades whose EventAt belonged to a bar that
	// had already closed by the time they arrived. Attached to the NEXT
	// bar emitted for the symbol so it stays visible in the persisted
	// series.
	LateTradesDropped int

	// Bounded trade-stream diagnostics, since raw trades are never kept:
	// enough to reconstruct lag and sequence-health after the fact without
	// storing every timestamp.
	FirstTradeEventAt, LastTradeEventAt       *time.Time
	FirstTradeReceivedAt, LastTradeReceivedAt *time.Time
	TradeLagSumMs, TradeLagMaxMs              int64
	TradeLagCount                             int
	MinTradeSeq, MaxTradeSeq                  *int64
	// OutOfOrderTradeCount counts accepted trades whose Seq regressed
	// relative to the previous accepted trade for this symbol (tracked
	// across bars, not reset per minute, since sequence is a
	// connection-level concept).
	OutOfOrderTradeCount int

	// Bounded ticker-stream diagnostics, same rationale.
	FirstTickerEventAt, LastTickerEventAt       *time.Time
	FirstTickerReceivedAt, LastTickerReceivedAt *time.Time
	TickerLagSumMs, TickerLagMaxMs              int64
	TickerLagCount                              int

	// UnbackfilledGapMinutes/From/To describe a gap longer than this
	// package will backfill minute-by-minute (see maxSyntheticBackfill).
	// Attached to the first real bar after such a gap.
	UnbackfilledGapMinutes int
	UnbackfilledGapFrom    *time.Time
	UnbackfilledGapTo      *time.Time

	// TickerComplete/TradesComplete reflect feed HEALTH for this bar's
	// entire window (see the package doc), not activity. Complete is their
	// conjunction.
	TickerComplete bool
	TradesComplete bool
	Complete       bool
}

type tradeRecord struct {
	eventAt     time.Time
	notionalUSD float64
}

// burstTracker keeps exact rolling 10s/30s sums for the common case where
// exchange event times arrive in order. Out-of-order events take a slower exact
// rebuild path, preserving the existing semantics without making every ordinary
// trade rescan the whole active tail.
type burstTracker struct {
	records        []tradeRecord
	left10, left30 int
	sum10, sum30   float64
}

// carryForward bundles everything that survives a bar rotation: it is
// neither reset to zero-value nor scoped to one minute.
type carryForward struct {
	tickerHealthy, tradesHealthy bool
	lastSeq                      int64
	buyBurst, sellBurst          burstTracker

	lastBidPrice, lastAskPrice *float64

	lastOpenInterest             *float64
	lastOpenInterestEventAt      *time.Time
	lastOpenInterestObservedAt   *time.Time
	lastOpenInterestValue        *float64
	lastOpenInterestValueEventAt *time.Time
	lastOpenInterestValueObserve *time.Time
}

type symbolState struct {
	bucketStart time.Time
	dedup       map[string]struct{}
	bar         Bar
	lateDropped int

	tickerHealthy, tradesHealthy               bool
	barTickerInterrupted, barTradesInterrupted bool
	lastSeq                                    int64

	buyBurst, sellBurst burstTracker
}

// maxSyntheticBackfill bounds how many empty, Complete=false bars a single
// gap eagerly backfills minute-by-minute. The remainder is never silently
// dropped: see UnbackfilledGapMinutes.
const maxSyntheticBackfill = 180

var (
	// ErrInvalidTrade is returned for a Trade missing required identity,
	// carrying an unrecognized Side, a non-finite/non-positive price or
	// size, or a price*size product that overflows to +Inf.
	ErrInvalidTrade = errors.New("invalid trade")
	// ErrInvalidTickerObservation is returned for a TickerObservation
	// missing required identity/timestamps, carrying a non-positive
	// LastPrice/BidPrice/AskPrice, a negative/non-finite OI reading, or an
	// OI value/timestamp pair that is only partially present.
	ErrInvalidTickerObservation = errors.New("invalid ticker observation")
)

// Engine aggregates Trade and TickerObservation values into per-symbol,
// per-minute Bars. It is not safe for concurrent use.
type Engine struct {
	states map[string]*symbolState
}

// New returns an empty Engine.
func New() *Engine {
	return &Engine{states: make(map[string]*symbolState)}
}

// AddTrade folds one trade into the current bar for its symbol, returning
// any bars (including synthetic gap-filler bars) that close as a result.
// A successful call marks the trade feed healthy going forward, but never
// retroactively un-interrupts the bar it lands in: if that bar's window
// already saw the feed unhealthy at any point, it stays TradesComplete=
// false for its own lifetime regardless of how quickly this trade follows.
func (e *Engine) AddTrade(t Trade) ([]Bar, error) {
	if t.Symbol == "" || t.EventAt.IsZero() || t.ReceivedAt.IsZero() || t.TradeID == "" ||
		!finitePositive(t.Price) || !finitePositive(t.Size) ||
		(t.Side != SideBuy && t.Side != SideSell) {
		return nil, ErrInvalidTrade
	}
	notional := t.Price * t.Size
	if math.IsInf(notional, 0) {
		return nil, ErrInvalidTrade
	}

	state, closed := e.advance(t.Symbol, t.EventAt)
	if t.EventAt.Before(state.bucketStart) {
		state.lateDropped++
		return closed, nil
	}
	if _, seen := state.dedup[t.TradeID]; seen {
		state.bar.DuplicateTradesDropped++
		return closed, nil
	}
	state.dedup[t.TradeID] = struct{}{}
	state.tradesHealthy = true

	state.bar.TradeCount++
	recordTradeDiagnostics(&state.bar, t)
	if t.Seq != 0 {
		if state.lastSeq != 0 && t.Seq < state.lastSeq {
			state.bar.OutOfOrderTradeCount++
		}
		state.lastSeq = t.Seq
		seq := t.Seq
		if state.bar.MinTradeSeq == nil || seq < *state.bar.MinTradeSeq {
			state.bar.MinTradeSeq = &seq
		}
		if state.bar.MaxTradeSeq == nil || seq > *state.bar.MaxTradeSeq {
			maxSeq := seq
			state.bar.MaxTradeSeq = &maxSeq
		}
	}

	side, burst := &state.bar.Buy, &state.buyBurst
	if t.Side == SideSell {
		side, burst = &state.bar.Sell, &state.sellBurst
	}
	switch {
	case t.IsBlockTrade:
		side.BlockTradeCount++
		side.BlockTradeNotionalUSD += notional
	case t.IsRPI:
		side.RPITradeCount++
		side.RPITradeNotionalUSD += notional
	default:
		side.TotalNotionalUSD += notional
		side.TradeCount++
		addToHistogram(side.Histogram, notional)
		side.TopNotionalsUSD = insertTopK(side.TopNotionalsUSD, notional)
		max10, max30 := burst.add(t.EventAt, notional)
		if max10 > side.Max10sNotionalUSD {
			side.Max10sNotionalUSD = max10
		}
		if max30 > side.Max30sNotionalUSD {
			side.Max30sNotionalUSD = max30
		}
	}
	return closed, nil
}

func recordTradeDiagnostics(bar *Bar, t Trade) {
	if bar.FirstTradeEventAt == nil {
		bar.FirstTradeEventAt = clonePtr(t.EventAt)
		bar.FirstTradeReceivedAt = clonePtr(t.ReceivedAt)
	}
	bar.LastTradeEventAt = clonePtr(t.EventAt)
	bar.LastTradeReceivedAt = clonePtr(t.ReceivedAt)
	lagMs := t.ReceivedAt.Sub(t.EventAt).Milliseconds()
	bar.TradeLagSumMs += lagMs
	if lagMs > bar.TradeLagMaxMs {
		bar.TradeLagMaxMs = lagMs
	}
	bar.TradeLagCount++
}

// AddTickerObservation folds one ticker/OI reading into the current bar for
// its symbol, returning any bars that close as a result. Later
// observations within the same minute overwrite earlier ones for
// close/last-bid/last-ask/OI (last-observed-in-bar). A successful call
// marks the ticker feed healthy going forward, with the same
// never-retroactively-repairs-the-current-bar rule as AddTrade.
func (e *Engine) AddTickerObservation(o TickerObservation) ([]Bar, error) {
	if o.Symbol == "" || o.EventAt.IsZero() || o.ObservedAt.IsZero() ||
		invalidOptionalPositive(o.LastPrice) || invalidOptionalPositive(o.BidPrice) ||
		invalidOptionalPositive(o.AskPrice) || invalidOptionalNonNegative(o.OpenInterest) ||
		invalidOptionalNonNegative(o.OpenInterestValue) ||
		!pairComplete(o.OpenInterest, o.OpenInterestEventAt, o.OpenInterestObservedAt) ||
		!pairComplete(o.OpenInterestValue, o.OpenInterestValueEventAt, o.OpenInterestValueObservedAt) {
		return nil, ErrInvalidTickerObservation
	}

	state, closed := e.advance(o.Symbol, o.EventAt)
	if o.EventAt.Before(state.bucketStart) {
		return closed, nil
	}
	state.tickerHealthy = true
	state.bar.TickerObservedThisMinute = true
	recordTickerDiagnostics(&state.bar, o)

	if o.LastPrice != nil {
		price := *o.LastPrice
		if state.bar.OpenPrice == nil {
			state.bar.OpenPrice = clonePtr(price)
		}
		if state.bar.HighPrice == nil || price > *state.bar.HighPrice {
			state.bar.HighPrice = clonePtr(price)
		}
		if state.bar.LowPrice == nil || price < *state.bar.LowPrice {
			state.bar.LowPrice = clonePtr(price)
		}
		state.bar.ClosePrice = clonePtr(price)
	}
	if o.BidPrice != nil {
		state.bar.LastBidPrice = clonePtr(*o.BidPrice)
	}
	if o.AskPrice != nil {
		state.bar.LastAskPrice = clonePtr(*o.AskPrice)
	}
	if o.OpenInterest != nil {
		state.bar.OpenInterest = clonePtr(*o.OpenInterest)
		state.bar.OpenInterestEventAt = cloneTimePtr(o.OpenInterestEventAt)
		state.bar.OpenInterestObservedAt = cloneTimePtr(o.OpenInterestObservedAt)
	}
	if o.OpenInterestValue != nil {
		state.bar.OpenInterestValue = clonePtr(*o.OpenInterestValue)
		state.bar.OpenInterestValueEventAt = cloneTimePtr(o.OpenInterestValueEventAt)
		state.bar.OpenInterestValueObservedAt = cloneTimePtr(o.OpenInterestValueObservedAt)
	}
	return closed, nil
}

func recordTickerDiagnostics(bar *Bar, o TickerObservation) {
	if bar.FirstTickerEventAt == nil {
		bar.FirstTickerEventAt = clonePtr(o.EventAt)
		bar.FirstTickerReceivedAt = clonePtr(o.ObservedAt)
	}
	bar.LastTickerEventAt = clonePtr(o.EventAt)
	bar.LastTickerReceivedAt = clonePtr(o.ObservedAt)
	lagMs := o.ObservedAt.Sub(o.EventAt).Milliseconds()
	bar.TickerLagSumMs += lagMs
	if lagMs > bar.TickerLagMaxMs {
		bar.TickerLagMaxMs = lagMs
	}
	bar.TickerLagCount++
}

// MarkTickerDiscontinuity tells the engine the ticker/OI feed for symbol
// had a gap starting at at. The current bar (whether pre-existing or
// created by this call's own advance) is immediately marked
// TickerComplete=false for its own remaining lifetime; the feed stays
// marked unhealthy, so every subsequent bar starts interrupted too, until a
// real TickerObservation is accepted again.
func (e *Engine) MarkTickerDiscontinuity(symbol string, at time.Time) []Bar {
	state, closed := e.advance(symbol, at)
	state.tickerHealthy = false
	state.barTickerInterrupted = true
	return closed
}

// MarkTradesDiscontinuity is MarkTickerDiscontinuity's trade-feed
// equivalent.
func (e *Engine) MarkTradesDiscontinuity(symbol string, at time.Time) []Bar {
	state, closed := e.advance(symbol, at)
	state.tradesHealthy = false
	state.barTradesInterrupted = true
	return closed
}

// Flush force-closes any bar (across all symbols) whose minute has fully
// elapsed as of now, even if no new event has arrived to trigger it.
func (e *Engine) Flush(now time.Time) []Bar {
	var out []Bar
	for symbol, state := range e.states {
		if !now.Truncate(time.Minute).After(state.bucketStart) {
			continue
		}
		_, closed := e.advance(symbol, now)
		out = append(out, closed...)
	}
	return out
}

// advance ensures the engine has an open bar for symbol covering at's
// minute, closing (and returning) the previous bar and any synthetic
// gap-filler bars in between. It never rewinds.
//
// Everything in carryForward survives every reset performed here,
// including every synthetic gap-filler bar: feed health, sequence
// continuity, burst-window tails, and the last known bid/ask/OI readings
// (with their own original timestamps) describe an ongoing condition, not
// bookkeeping scoped to one minute. Gap-filler bars are the one exception
// to health-based interruption: a multi-minute jump with literally zero
// calls to this engine is itself anomalous regardless of the last reported
// health, so every such bar is forced interrupted for both feeds.
func (e *Engine) advance(symbol string, at time.Time) (*symbolState, []Bar) {
	bucketStart := at.UTC().Truncate(time.Minute)
	state, ok := e.states[symbol]
	if !ok {
		state = newSymbolState(symbol, bucketStart, carryForward{})
		e.states[symbol] = state
		return state, nil
	}
	if !bucketStart.After(state.bucketStart) {
		return state, nil
	}

	cf := carryForward{
		tickerHealthy: state.tickerHealthy,
		tradesHealthy: state.tradesHealthy,
		lastSeq:       state.lastSeq,
		buyBurst:      state.buyBurst,
		sellBurst:     state.sellBurst,

		lastBidPrice: state.bar.LastBidPrice,
		lastAskPrice: state.bar.LastAskPrice,

		lastOpenInterest:             state.bar.OpenInterest,
		lastOpenInterestEventAt:      state.bar.OpenInterestEventAt,
		lastOpenInterestObservedAt:   state.bar.OpenInterestObservedAt,
		lastOpenInterestValue:        state.bar.OpenInterestValue,
		lastOpenInterestValueEventAt: state.bar.OpenInterestValueEventAt,
		lastOpenInterestValueObserve: state.bar.OpenInterestValueObservedAt,
	}

	totalGapMinutes := int(bucketStart.Sub(state.bucketStart)/time.Minute) - 1
	backfillCount := min(totalGapMinutes, maxSyntheticBackfill)

	closed := []Bar{finalizeBar(state)}
	next := state.bucketStart.Add(time.Minute)
	for range backfillCount {
		gap := newSymbolState(symbol, next, cf)
		gap.barTickerInterrupted = true
		gap.barTradesInterrupted = true
		closed = append(closed, finalizeBar(gap))
		next = next.Add(time.Minute)
	}

	*state = *newSymbolState(symbol, bucketStart, cf)
	if unbackfilled := totalGapMinutes - backfillCount; unbackfilled > 0 {
		from, to := next, bucketStart
		state.bar.UnbackfilledGapMinutes = unbackfilled
		state.bar.UnbackfilledGapFrom = &from
		state.bar.UnbackfilledGapTo = &to
	}
	return state, closed
}

func newSymbolState(symbol string, bucketStart time.Time, cf carryForward) *symbolState {
	return &symbolState{
		bucketStart:          bucketStart,
		dedup:                make(map[string]struct{}),
		tickerHealthy:        cf.tickerHealthy,
		tradesHealthy:        cf.tradesHealthy,
		barTickerInterrupted: !cf.tickerHealthy,
		barTradesInterrupted: !cf.tradesHealthy,
		lastSeq:              cf.lastSeq,
		buyBurst:             cf.buyBurst,
		sellBurst:            cf.sellBurst,
		bar: Bar{
			Symbol:      symbol,
			BucketStart: bucketStart,
			Buy:         newSideStats(),
			Sell:        newSideStats(),

			LastBidPrice: cf.lastBidPrice,
			LastAskPrice: cf.lastAskPrice,

			OpenInterest:                cf.lastOpenInterest,
			OpenInterestEventAt:         cf.lastOpenInterestEventAt,
			OpenInterestObservedAt:      cf.lastOpenInterestObservedAt,
			OpenInterestValue:           cf.lastOpenInterestValue,
			OpenInterestValueEventAt:    cf.lastOpenInterestValueEventAt,
			OpenInterestValueObservedAt: cf.lastOpenInterestValueObserve,
		},
	}
}

func finalizeBar(state *symbolState) Bar {
	state.bar.LateTradesDropped = state.lateDropped
	state.bar.TickerComplete = !state.barTickerInterrupted
	state.bar.TradesComplete = !state.barTradesInterrupted
	state.bar.Complete = state.bar.TickerComplete && state.bar.TradesComplete
	return state.bar
}

func (tracker *burstTracker) add(eventAt time.Time, notionalUSD float64) (float64, float64) {
	record := tradeRecord{eventAt: eventAt, notionalUSD: notionalUSD}
	if len(tracker.records) == 0 || !eventAt.Before(tracker.records[len(tracker.records)-1].eventAt) {
		tracker.records = append(tracker.records, record)
		tracker.sum10 += notionalUSD
		tracker.sum30 += notionalUSD

		for tracker.left10 < len(tracker.records) &&
			eventAt.Sub(tracker.records[tracker.left10].eventAt) > burstWindow10s {
			tracker.sum10 -= tracker.records[tracker.left10].notionalUSD
			tracker.left10++
		}
		for tracker.left30 < len(tracker.records) &&
			eventAt.Sub(tracker.records[tracker.left30].eventAt) > burstWindow30s {
			tracker.sum30 -= tracker.records[tracker.left30].notionalUSD
			tracker.left30++
		}
		tracker.compact()
		return tracker.sum10, tracker.sum30
	}

	// Preserve the previous exact behavior for the rare out-of-order path.
	// Logically pruned records are intentionally omitted: the old implementation
	// had already physically discarded them before this event arrived too.
	active := append([]tradeRecord(nil), tracker.records[tracker.left30:]...)
	insertSortedByEventAt(&active, record)
	max10 := slidingWindowMaxSum(active, burstWindow10s)
	max30 := slidingWindowMaxSum(active, burstWindow30s)
	tracker.reset(active)
	return max10, max30
}

func (tracker *burstTracker) compact() {
	if tracker.left30 < 1024 || tracker.left30*2 < len(tracker.records) {
		return
	}
	tracker.records = append([]tradeRecord(nil), tracker.records[tracker.left30:]...)
	tracker.left10 -= tracker.left30
	tracker.left30 = 0
}

func (tracker *burstTracker) reset(sorted []tradeRecord) {
	latest := sorted[len(sorted)-1].eventAt
	cutoff30 := latest.Add(-burstWindow30s)
	first30 := sort.Search(len(sorted), func(index int) bool {
		return !sorted[index].eventAt.Before(cutoff30)
	})
	tracker.records = append(tracker.records[:0], sorted[first30:]...)
	tracker.left30 = 0
	tracker.left10 = sort.Search(len(tracker.records), func(index int) bool {
		return latest.Sub(tracker.records[index].eventAt) <= burstWindow10s
	})
	tracker.sum10 = 0
	tracker.sum30 = 0
	for index, record := range tracker.records {
		tracker.sum30 += record.notionalUSD
		if index >= tracker.left10 {
			tracker.sum10 += record.notionalUSD
		}
	}
}

func insertSortedByEventAt(tail *[]tradeRecord, rec tradeRecord) {
	i := sort.Search(len(*tail), func(i int) bool { return !(*tail)[i].eventAt.Before(rec.eventAt) })
	*tail = append(*tail, tradeRecord{})
	copy((*tail)[i+1:], (*tail)[i:])
	(*tail)[i] = rec
}

// slidingWindowMaxSum returns the largest total notional summed over any
// contiguous span of sorted no longer than window. sorted must already be
// sorted by eventAt ascending.
func slidingWindowMaxSum(sorted []tradeRecord, window time.Duration) float64 {
	var maxSum, sum float64
	left := 0
	for right := range sorted {
		sum += sorted[right].notionalUSD
		for sorted[right].eventAt.Sub(sorted[left].eventAt) > window {
			sum -= sorted[left].notionalUSD
			left++
		}
		if sum > maxSum {
			maxSum = sum
		}
	}
	return maxSum
}

// insertTopK keeps at most topK largest values, descending.
func insertTopK(current []float64, value float64) []float64 {
	i := 0
	for i < len(current) && current[i] >= value {
		i++
	}
	current = append(current, 0)
	copy(current[i+1:], current[i:])
	current[i] = value
	if len(current) > topK {
		current = current[:topK]
	}
	return current
}

func finitePositive(v float64) bool {
	return !math.IsNaN(v) && !math.IsInf(v, 0) && v > 0
}

func invalidOptionalPositive(v *float64) bool {
	return v != nil && !finitePositive(*v)
}

func invalidOptionalNonNegative(v *float64) bool {
	return v != nil && (math.IsNaN(*v) || math.IsInf(*v, 0) || *v < 0)
}

// pairComplete enforces that a value and its event/observed timestamps are
// present together or not at all: a value with no timestamp, or a
// timestamp with no value, is a contract violation.
func pairComplete(value *float64, eventAt, observedAt *time.Time) bool {
	if value == nil {
		return eventAt == nil && observedAt == nil
	}
	return eventAt != nil && observedAt != nil
}

func clonePtr[T any](v T) *T { return &v }

func cloneTimePtr(t *time.Time) *time.Time {
	if t == nil {
		return nil
	}
	v := *t
	return &v
}

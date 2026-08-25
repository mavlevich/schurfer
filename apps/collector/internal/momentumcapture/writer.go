package momentumcapture

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mavlevich/schurfer/collector/internal/momentum"
)

// CaptureVersion pins the exact column set and histogram bucket boundaries a
// row was written under (see timeseries.bybit_momentum_bars_1m's primary
// key). Bump this whenever momentum.Bar's persisted shape changes in a way
// that would make an old row and a new row incomparable, so old and new
// rows never collide on the same primary key.
const CaptureVersion = "v1"

// DerivativesContextVersion versions the additive mark/index/funding
// contract independently from CaptureVersion. Existing price/OI/flow
// consumers can keep reading capture v1 across this addition, while a
// context consumer must require this explicit version and
// derivatives_complete=true. Historical rows remain NULL, never fake zero.
const DerivativesContextVersion = "derivatives_context_v1"

const (
	// MaxPendingBars bounds the writer's own queue independently of
	// whatever produced the backlog (a slow database, a network blip).
	// Overflow drops the OLDEST bars, the same choice hotset's writer
	// makes: a late bar for a closed minute is worth less than capacity
	// to keep accepting new ones.
	MaxPendingBars = 20000

	writeTimeout  = 5 * time.Second
	writeRetryMin = time.Second
	writeRetryMax = 30 * time.Second
)

// PersistRetryState is the same bounded exponential-backoff pattern as
// apps/collector/cmd/hotset/main.go's persistRetryState: a failed flush
// backs off instead of hot-looping against a struggling database, and a
// successful flush resets it immediately.
type PersistRetryState struct {
	nextAttempt time.Time
	backoff     time.Duration
}

// Ready reports whether enough time has passed since the last failure to
// attempt another flush.
func (s *PersistRetryState) Ready(now time.Time) bool {
	return s.nextAttempt.IsZero() || !now.Before(s.nextAttempt)
}

func (s *PersistRetryState) failed(now time.Time) time.Duration {
	if s.backoff == 0 {
		s.backoff = writeRetryMin
	} else {
		s.backoff = min(s.backoff*2, writeRetryMax)
	}
	s.nextAttempt = now.Add(s.backoff)
	return s.backoff
}

func (s *PersistRetryState) succeeded() {
	s.nextAttempt = time.Time{}
	s.backoff = 0
}

// writerDB is the slice of *pgxpool.Pool the writer needs, narrowed so
// tests can supply a fake instead of a real database (same shape as
// apps/notifier/internal/notifier/alert_recorder.go's alertDB).
type writerDB interface {
	SendBatch(ctx context.Context, b *pgx.Batch) pgx.BatchResults
	Close()
}

// WriterStats is a point-in-time snapshot of the writer's own counters, for
// folding into Health.
type WriterStats struct {
	QueueDepth               int
	QueuePeak                int
	QueueDropsTotal          uint64
	BarsPersistedTotal       uint64
	PersistErrorsTotal       uint64
	PersistRetriesTotal      uint64
	RowsWrittenTotal         uint64
	PayloadHashMismatchTotal uint64
	LastPersistAt            time.Time
}

// Writer batches Bars and persists them to timeseries.bybit_momentum_bars_1m.
// It owns no goroutine of its own: cmd/momentumcapture runs a dedicated
// writer goroutine that calls Enqueue as bars close and Flush on its own
// schedule, decoupled from the goroutine that owns momentum.Engine, so a
// slow or unavailable database never blocks trade/ticker ingestion. mu
// guards pending/peak/stats specifically because of that split: Enqueue and
// Flush are only ever called from the one writer goroutine (so they never
// race each other), but Stats is read from a different goroutine
// (health reporting) while a Flush may be in flight, which does race
// without it. mu is never held across the actual network I/O in
// flushBatch, only around the in-memory bookkeeping before and after it.
type Writer struct {
	db              writerDB
	exchange        string
	marketType      string
	universeVersion string

	mu      sync.Mutex
	pending []momentum.Bar
	peak    int
	retry   PersistRetryState
	stats   WriterStats
}

// NewWriter constructs a Writer bound to one frozen universe's identity.
// exchange/marketType/universeVersion are fixed for the writer's entire
// lifetime, matching the frozen-universe v1 contract (see Universe's doc
// comment): momentum-capture never rewrites a different universe_version
// into an already-running process.
func NewWriter(pool *pgxpool.Pool, exchange, marketType, universeVersion string) *Writer {
	return &Writer{
		db:              pool,
		exchange:        exchange,
		marketType:      marketType,
		universeVersion: universeVersion,
	}
}

// Enqueue appends bars to the pending batch, dropping the oldest entries if
// the bound is exceeded. Returns how many were dropped.
func (w *Writer) Enqueue(bars []momentum.Bar) int {
	if len(bars) == 0 {
		return 0
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.pending = append(w.pending, bars...)
	dropped := 0
	if extra := len(w.pending) - MaxPendingBars; extra > 0 {
		copy(w.pending, w.pending[extra:])
		w.pending = w.pending[:MaxPendingBars]
		dropped = extra
	}
	if len(w.pending) > w.peak {
		w.peak = len(w.pending)
	}
	if dropped > 0 {
		w.stats.QueueDropsTotal += uint64(dropped)
	}
	return dropped
}

// Ready reports whether enough time has passed since the last failed flush
// to try again. Callers should still call Flush unconditionally on a force
// path (e.g. shutdown drain).
func (w *Writer) Ready(now time.Time) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.retry.Ready(now)
}

// writerSubBatchSize bounds one pipelined batch: at MaxPendingBars (20000),
// a single all-or-nothing batch could take far longer than writeTimeout to
// process server-side, and would need to succeed in full before ANY of it
// is durable. Under exactly the backlog conditions the writer most needs to
// recover from (a slow or briefly unavailable database), a single giant
// batch that keeps missing its own deadline is a self-reinforcing failure,
// not a recovery path. Sub-batching makes forward progress incremental:
// each successful chunk shrinks pending even if a later chunk fails.
const writerSubBatchSize = 500

// Flush persists pending in bounded sub-batches (each one pipelined: one
// INSERT per row, but one network round trip per sub-batch, not one per
// row) and advances pending only past sub-batches that succeeded. A
// sub-batch failure (a single row's query erroring, or the batch's own
// Close failing) is treated as a full failure for that sub-batch: it is
// retried together with everything after it next time, since Bars carry no
// independent retry state of their own, but earlier successful sub-batches
// are not undone or re-sent. Every sub-batch is copied out from under mu
// before the network call, so mu is never held while waiting on the
// database.
func (w *Writer) Flush(ctx context.Context) error {
	persistedAny := false
	for {
		batch, ok := w.takeSubBatch()
		if !ok {
			break
		}
		if err := w.flushBatch(ctx, batch); err != nil {
			return err
		}
		w.commitSubBatch(len(batch))
		persistedAny = true
	}
	w.mu.Lock()
	w.retry.succeeded()
	if persistedAny {
		w.stats.LastPersistAt = time.Now()
	}
	w.mu.Unlock()
	return nil
}

func (w *Writer) takeSubBatch() ([]momentum.Bar, bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if len(w.pending) == 0 {
		return nil, false
	}
	n := min(len(w.pending), writerSubBatchSize)
	batch := append([]momentum.Bar(nil), w.pending[:n]...)
	return batch, true
}

func (w *Writer) commitSubBatch(n int) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.pending = w.pending[n:]
}

func (w *Writer) recordPersistFailure() time.Duration {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.stats.PersistErrorsTotal++
	retryAfter := w.retry.failed(time.Now())
	w.stats.PersistRetriesTotal++
	return retryAfter
}

func (w *Writer) flushBatch(ctx context.Context, bars []momentum.Bar) error {
	flushCtx, cancel := context.WithTimeout(ctx, writeTimeout)
	defer cancel()

	batch := &pgx.Batch{}
	hashes := make([][32]byte, len(bars))
	for i, bar := range bars {
		args, hash := w.rowArgs(bar)
		hashes[i] = hash
		batch.Queue(insertRowSQL, args...)
	}

	results := w.db.SendBatch(flushCtx, batch)

	for i, bar := range bars {
		var inserted bool
		var storedHash []byte
		if err := results.QueryRow().Scan(&inserted, &storedHash); err != nil {
			_ = results.Close()
			retryAfter := w.recordPersistFailure()
			return fmt.Errorf("flush bar %s@%s: %w (retry in %s)", bar.Symbol, bar.BucketStart, err, retryAfter)
		}
		if !inserted && len(storedHash) == 32 && !bytes.Equal(storedHash, hashes[i][:]) {
			w.mu.Lock()
			w.stats.PayloadHashMismatchTotal++
			w.mu.Unlock()
			slog.Error(
				"momentumcapture.writer.payload_hash_mismatch",
				"symbol", bar.Symbol,
				"bucket_start", bar.BucketStart,
				"reason", "same primary key, different computed content: not a harmless retry",
			)
		}
	}

	// Close is the definitive completion signal for a pgx batch: even if
	// every individual QueryRow above scanned cleanly, Close can still
	// surface a final synchronization error. Checking it before the caller
	// advances past this sub-batch (not just logged from a defer) means a
	// close failure is treated as a real flush failure, not silently
	// dropped from the writer's own backlog.
	if err := results.Close(); err != nil {
		retryAfter := w.recordPersistFailure()
		return fmt.Errorf("close batch: %w (retry in %s)", err, retryAfter)
	}

	w.mu.Lock()
	w.stats.RowsWrittenTotal += uint64(len(bars))
	w.stats.BarsPersistedTotal += uint64(len(bars))
	w.mu.Unlock()
	return nil
}

// Close releases the underlying pool. The writer goroutine is responsible
// for calling Flush with a bounded shutdown context first so this never
// discards unpersisted bars silently.
func (w *Writer) Close() {
	w.db.Close()
}

// Stats returns a snapshot for Health. QueueDepth/QueuePeak reflect bars
// waiting to be written, not bars already durably persisted. Safe to call
// from a different goroutine than the one calling Enqueue/Flush (see the
// Writer doc comment).
func (w *Writer) Stats() WriterStats {
	w.mu.Lock()
	defer w.mu.Unlock()
	stats := w.stats
	stats.QueueDepth = len(w.pending)
	stats.QueuePeak = w.peak
	return stats
}

const insertRowSQL = `
INSERT INTO timeseries.bybit_momentum_bars_1m (
	exchange, market_type, symbol, capture_version, bucket_start, universe_version,
	open_price, high_price, low_price, close_price, last_bid_price, last_ask_price,
	buy_total_notional_usd, buy_trade_count, buy_hist_counts, buy_hist_notional,
	buy_top_notional, buy_max_10s_notional_usd, buy_max_30s_notional_usd,
	buy_block_trade_count, buy_block_trade_notional_usd, buy_rpi_trade_count, buy_rpi_trade_notional_usd,
	sell_total_notional_usd, sell_trade_count, sell_hist_counts, sell_hist_notional,
	sell_top_notional, sell_max_10s_notional_usd, sell_max_30s_notional_usd,
	sell_block_trade_count, sell_block_trade_notional_usd, sell_rpi_trade_count, sell_rpi_trade_notional_usd,
	open_interest, open_interest_event_at, open_interest_observed_at,
	open_interest_value, open_interest_value_event_at, open_interest_value_observed_at,
	ticker_observed_this_minute,
	trade_count, duplicate_trades_dropped, late_trades_dropped,
	first_trade_event_at, last_trade_event_at, first_trade_received_at, last_trade_received_at,
	trade_lag_sum_ms, trade_lag_max_ms, trade_lag_count, min_trade_seq, max_trade_seq, out_of_order_trade_count,
	first_ticker_event_at, last_ticker_event_at, first_ticker_received_at, last_ticker_received_at,
	ticker_lag_sum_ms, ticker_lag_max_ms, ticker_lag_count,
	unbackfilled_gap_minutes, unbackfilled_gap_from, unbackfilled_gap_to,
	ticker_complete, trades_complete, complete,
	price_source, first_price_event_at, last_price_event_at,
	first_price_received_at, last_price_received_at, price_observed_this_minute,
	open_interest_complete, price_complete,
	derivatives_context_version,
	mark_price, mark_price_event_at, mark_price_observed_at,
	index_price, index_price_event_at, index_price_observed_at,
	funding_rate, funding_rate_event_at, funding_rate_observed_at,
	next_funding_at, next_funding_event_at, next_funding_observed_at,
	derivatives_observed_this_minute, derivatives_complete,
	payload_hash
)
VALUES (
	$1, $2, $3, $4, $5, $6,
	$7, $8, $9, $10, $11, $12,
	$13, $14, $15, $16,
	$17, $18, $19,
	$20, $21, $22, $23,
	$24, $25, $26, $27,
	$28, $29, $30,
	$31, $32, $33, $34,
	$35, $36, $37,
	$38, $39, $40,
	$41,
	$42, $43, $44,
	$45, $46, $47, $48,
	$49, $50, $51, $52, $53, $54,
	$55, $56, $57, $58,
	$59, $60, $61,
	$62, $63, $64,
	$65, $66, $67,
	$68, $69, $70,
	$71, $72, $73,
	$74, $75,
	$76,
	$77, $78, $79,
	$80, $81, $82,
	$83, $84, $85,
	$86, $87, $88,
	$89, $90,
	$91
)
ON CONFLICT (exchange, market_type, symbol, capture_version, bucket_start)
DO UPDATE SET created_at = bybit_momentum_bars_1m.created_at
RETURNING (xmax = 0) AS inserted, payload_hash
`

// canonicalRow mirrors insertRowSQL's column list (payload_hash and
// created_at excluded: payload_hash is derived FROM this, and created_at is
// write-time receipt metadata, not row content). Field order matters for
// determinism: encoding/json marshals struct fields in declaration order.
type canonicalRow struct {
	Exchange        string
	MarketType      string
	Symbol          string
	CaptureVersion  string
	BucketStart     time.Time
	UniverseVersion string

	OpenPrice, HighPrice, LowPrice, ClosePrice *float64
	LastBidPrice, LastAskPrice                 *float64

	BuyTotalNotionalUSD      float64
	BuyTradeCount            int
	BuyHistCounts            []int32
	BuyHistNotional          []float64
	BuyTopNotional           []float64
	BuyMax10sNotionalUSD     float64
	BuyMax30sNotionalUSD     float64
	BuyBlockTradeCount       int
	BuyBlockTradeNotionalUSD float64
	BuyRPITradeCount         int
	BuyRPITradeNotionalUSD   float64

	SellTotalNotionalUSD      float64
	SellTradeCount            int
	SellHistCounts            []int32
	SellHistNotional          []float64
	SellTopNotional           []float64
	SellMax10sNotionalUSD     float64
	SellMax30sNotionalUSD     float64
	SellBlockTradeCount       int
	SellBlockTradeNotionalUSD float64
	SellRPITradeCount         int
	SellRPITradeNotionalUSD   float64

	OpenInterest           *float64
	OpenInterestEventAt    *time.Time
	OpenInterestObservedAt *time.Time

	OpenInterestValue           *float64
	OpenInterestValueEventAt    *time.Time
	OpenInterestValueObservedAt *time.Time

	TickerObservedThisMinute bool

	TradeCount             int
	DuplicateTradesDropped int
	LateTradesDropped      int

	FirstTradeEventAt, LastTradeEventAt       *time.Time
	FirstTradeReceivedAt, LastTradeReceivedAt *time.Time
	TradeLagSumMs, TradeLagMaxMs              int64
	TradeLagCount                             int
	MinTradeSeq, MaxTradeSeq                  *int64
	OutOfOrderTradeCount                      int

	FirstTickerEventAt, LastTickerEventAt       *time.Time
	FirstTickerReceivedAt, LastTickerReceivedAt *time.Time
	TickerLagSumMs, TickerLagMaxMs              int64
	TickerLagCount                              int

	UnbackfilledGapMinutes int
	UnbackfilledGapFrom    *time.Time
	UnbackfilledGapTo      *time.Time

	TickerComplete bool
	TradesComplete bool
	Complete       bool

	PriceSource                               string
	FirstPriceEventAt, LastPriceEventAt       *time.Time
	FirstPriceReceivedAt, LastPriceReceivedAt *time.Time
	PriceObservedThisMinute                   bool
	OpenInterestComplete, PriceComplete       bool

	DerivativesContextVersion string
	MarkPrice                 *float64
	MarkPriceEventAt          *time.Time
	MarkPriceObservedAt       *time.Time
	IndexPrice                *float64
	IndexPriceEventAt         *time.Time
	IndexPriceObservedAt      *time.Time
	FundingRate               *float64
	FundingRateEventAt        *time.Time
	FundingRateObservedAt     *time.Time
	NextFundingAt             *time.Time
	NextFundingEventAt        *time.Time
	NextFundingObservedAt     *time.Time
	DerivativesObserved       bool
	DerivativesComplete       bool
}

// rowArgs builds the positional args for insertRowSQL and the payload_hash
// that goes with them, from one momentum.Bar plus the writer's fixed
// exchange/market_type/universe identity.
func (w *Writer) rowArgs(bar momentum.Bar) ([]any, [32]byte) {
	buyCounts, buyNotional := splitHistogram(bar.Buy.Histogram)
	sellCounts, sellNotional := splitHistogram(bar.Sell.Histogram)
	buyTop := nonNilFloats(bar.Buy.TopNotionalsUSD)
	sellTop := nonNilFloats(bar.Sell.TopNotionalsUSD)

	row := canonicalRow{
		Exchange:        w.exchange,
		MarketType:      w.marketType,
		Symbol:          bar.Symbol,
		CaptureVersion:  CaptureVersion,
		BucketStart:     bar.BucketStart,
		UniverseVersion: w.universeVersion,

		OpenPrice: bar.OpenPrice, HighPrice: bar.HighPrice, LowPrice: bar.LowPrice, ClosePrice: bar.ClosePrice,
		LastBidPrice: bar.LastBidPrice, LastAskPrice: bar.LastAskPrice,

		BuyTotalNotionalUSD: bar.Buy.TotalNotionalUSD, BuyTradeCount: bar.Buy.TradeCount,
		BuyHistCounts: buyCounts, BuyHistNotional: buyNotional, BuyTopNotional: buyTop,
		BuyMax10sNotionalUSD: bar.Buy.Max10sNotionalUSD, BuyMax30sNotionalUSD: bar.Buy.Max30sNotionalUSD,
		BuyBlockTradeCount: bar.Buy.BlockTradeCount, BuyBlockTradeNotionalUSD: bar.Buy.BlockTradeNotionalUSD,
		BuyRPITradeCount: bar.Buy.RPITradeCount, BuyRPITradeNotionalUSD: bar.Buy.RPITradeNotionalUSD,

		SellTotalNotionalUSD: bar.Sell.TotalNotionalUSD, SellTradeCount: bar.Sell.TradeCount,
		SellHistCounts: sellCounts, SellHistNotional: sellNotional, SellTopNotional: sellTop,
		SellMax10sNotionalUSD: bar.Sell.Max10sNotionalUSD, SellMax30sNotionalUSD: bar.Sell.Max30sNotionalUSD,
		SellBlockTradeCount: bar.Sell.BlockTradeCount, SellBlockTradeNotionalUSD: bar.Sell.BlockTradeNotionalUSD,
		SellRPITradeCount: bar.Sell.RPITradeCount, SellRPITradeNotionalUSD: bar.Sell.RPITradeNotionalUSD,

		OpenInterest: bar.OpenInterest, OpenInterestEventAt: bar.OpenInterestEventAt, OpenInterestObservedAt: bar.OpenInterestObservedAt,
		OpenInterestValue: bar.OpenInterestValue, OpenInterestValueEventAt: bar.OpenInterestValueEventAt, OpenInterestValueObservedAt: bar.OpenInterestValueObservedAt,
		TickerObservedThisMinute: bar.TickerObservedThisMinute,

		TradeCount: bar.TradeCount, DuplicateTradesDropped: bar.DuplicateTradesDropped, LateTradesDropped: bar.LateTradesDropped,

		FirstTradeEventAt: bar.FirstTradeEventAt, LastTradeEventAt: bar.LastTradeEventAt,
		FirstTradeReceivedAt: bar.FirstTradeReceivedAt, LastTradeReceivedAt: bar.LastTradeReceivedAt,
		TradeLagSumMs: bar.TradeLagSumMs, TradeLagMaxMs: bar.TradeLagMaxMs, TradeLagCount: bar.TradeLagCount,
		MinTradeSeq: bar.MinTradeSeq, MaxTradeSeq: bar.MaxTradeSeq, OutOfOrderTradeCount: bar.OutOfOrderTradeCount,

		FirstTickerEventAt: bar.FirstTickerEventAt, LastTickerEventAt: bar.LastTickerEventAt,
		FirstTickerReceivedAt: bar.FirstTickerReceivedAt, LastTickerReceivedAt: bar.LastTickerReceivedAt,
		TickerLagSumMs: bar.TickerLagSumMs, TickerLagMaxMs: bar.TickerLagMaxMs, TickerLagCount: bar.TickerLagCount,

		UnbackfilledGapMinutes: bar.UnbackfilledGapMinutes, UnbackfilledGapFrom: bar.UnbackfilledGapFrom, UnbackfilledGapTo: bar.UnbackfilledGapTo,

		TickerComplete: bar.TickerComplete, TradesComplete: bar.TradesComplete, Complete: bar.Complete,

		PriceSource:       string(bar.PriceSource),
		FirstPriceEventAt: bar.FirstPriceEventAt, LastPriceEventAt: bar.LastPriceEventAt,
		FirstPriceReceivedAt: bar.FirstPriceReceivedAt, LastPriceReceivedAt: bar.LastPriceReceivedAt,
		PriceObservedThisMinute: bar.PriceObservedThisMinute,
		OpenInterestComplete:    bar.OpenInterestComplete, PriceComplete: bar.PriceComplete,

		DerivativesContextVersion: DerivativesContextVersion,
		MarkPrice:                 bar.MarkPrice, MarkPriceEventAt: bar.MarkPriceEventAt,
		MarkPriceObservedAt: bar.MarkPriceObservedAt,
		IndexPrice:          bar.IndexPrice, IndexPriceEventAt: bar.IndexPriceEventAt,
		IndexPriceObservedAt: bar.IndexPriceObservedAt,
		FundingRate:          bar.FundingRate, FundingRateEventAt: bar.FundingRateEventAt,
		FundingRateObservedAt: bar.FundingRateObservedAt,
		NextFundingAt:         bar.NextFundingAt, NextFundingEventAt: bar.NextFundingEventAt,
		NextFundingObservedAt: bar.NextFundingObservedAt,
		DerivativesObserved:   bar.DerivativesObservedThisMinute,
		DerivativesComplete:   bar.DerivativesComplete,
	}

	hash := hashRow(row)
	args := []any{
		row.Exchange, row.MarketType, row.Symbol, row.CaptureVersion, row.BucketStart, row.UniverseVersion,
		row.OpenPrice, row.HighPrice, row.LowPrice, row.ClosePrice, row.LastBidPrice, row.LastAskPrice,
		row.BuyTotalNotionalUSD, row.BuyTradeCount, row.BuyHistCounts, row.BuyHistNotional,
		row.BuyTopNotional, row.BuyMax10sNotionalUSD, row.BuyMax30sNotionalUSD,
		row.BuyBlockTradeCount, row.BuyBlockTradeNotionalUSD, row.BuyRPITradeCount, row.BuyRPITradeNotionalUSD,
		row.SellTotalNotionalUSD, row.SellTradeCount, row.SellHistCounts, row.SellHistNotional,
		row.SellTopNotional, row.SellMax10sNotionalUSD, row.SellMax30sNotionalUSD,
		row.SellBlockTradeCount, row.SellBlockTradeNotionalUSD, row.SellRPITradeCount, row.SellRPITradeNotionalUSD,
		row.OpenInterest, row.OpenInterestEventAt, row.OpenInterestObservedAt,
		row.OpenInterestValue, row.OpenInterestValueEventAt, row.OpenInterestValueObservedAt,
		row.TickerObservedThisMinute,
		row.TradeCount, row.DuplicateTradesDropped, row.LateTradesDropped,
		row.FirstTradeEventAt, row.LastTradeEventAt, row.FirstTradeReceivedAt, row.LastTradeReceivedAt,
		row.TradeLagSumMs, row.TradeLagMaxMs, row.TradeLagCount, row.MinTradeSeq, row.MaxTradeSeq, row.OutOfOrderTradeCount,
		row.FirstTickerEventAt, row.LastTickerEventAt, row.FirstTickerReceivedAt, row.LastTickerReceivedAt,
		row.TickerLagSumMs, row.TickerLagMaxMs, row.TickerLagCount,
		row.UnbackfilledGapMinutes, row.UnbackfilledGapFrom, row.UnbackfilledGapTo,
		row.TickerComplete, row.TradesComplete, row.Complete,
		row.PriceSource, row.FirstPriceEventAt, row.LastPriceEventAt,
		row.FirstPriceReceivedAt, row.LastPriceReceivedAt, row.PriceObservedThisMinute,
		row.OpenInterestComplete, row.PriceComplete,
		row.DerivativesContextVersion,
		row.MarkPrice, row.MarkPriceEventAt, row.MarkPriceObservedAt,
		row.IndexPrice, row.IndexPriceEventAt, row.IndexPriceObservedAt,
		row.FundingRate, row.FundingRateEventAt, row.FundingRateObservedAt,
		row.NextFundingAt, row.NextFundingEventAt, row.NextFundingObservedAt,
		row.DerivativesObserved, row.DerivativesComplete,
		hash[:],
	}
	return args, hash
}

// hashRow computes payload_hash from a JSON encoding of row. JSON, not a
// hand-rolled delimiter format: struct field order is already fixed and
// documented above, and json.Marshal's float/time formatting is
// deterministic for a given value, which is all a "same content -> same
// hash" guarantee needs.
func hashRow(row canonicalRow) [32]byte {
	encoded, err := json.Marshal(row)
	if err != nil {
		// canonicalRow contains no channels/funcs/cyclic types, so this
		// cannot fail in practice; a hash collision from a zero-length
		// input is still safer than panicking mid-flush.
		slog.Error("momentumcapture.writer.hash_encode_failed", "err", err)
	}
	return sha256.Sum256(encoded)
}

// splitHistogram turns momentum's []HistogramBucket into the two parallel
// arrays the schema stores (counts, notional), matching bucket order.
func splitHistogram(buckets []momentum.HistogramBucket) ([]int32, []float64) {
	counts := make([]int32, len(buckets))
	notional := make([]float64, len(buckets))
	for i, b := range buckets {
		//nolint:gosec
		counts[i] = int32(b.Count)
		notional[i] = b.NotionalUSD
	}
	return counts, notional
}

// nonNilFloats normalizes a nil slice to an empty one: pgx encodes a nil Go
// slice as SQL NULL, not '{}', which would violate buy_top_notional/
// sell_top_notional's NOT NULL constraint whenever a side's top-K was never
// populated (e.g. a quiet minute with zero trades on that side).
func nonNilFloats(values []float64) []float64 {
	if values == nil {
		return []float64{}
	}
	return values
}

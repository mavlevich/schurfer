package burstprobe

import (
	"context"
	"fmt"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/burstengine"
)

// Market is what the probe asks the venue for; REST implements it.
type Market interface {
	Book(ctx context.Context, symbol string) (BookSnapshot, error)
	Funding(ctx context.Context, symbol string) (Funding, error)
	SettledBetween(ctx context.Context, symbol string, from, to time.Time) ([]Settled, error)
}

// Recorder takes sealed records; Sealed implements it.
type Recorder interface {
	Write(record any) error
}

// Config of the probe.
type Config struct {
	MaxOpen      int           // the design's portfolio limit (3)
	Hold         time.Duration // exit after the bar's end (60 minutes)
	QuoteTimeout time.Duration
}

// Stage names of the visible latency distributions, all durations.
const (
	StageLastTrade   = "bar_end_to_last_trade_received"
	StageEvaluated   = "bar_end_to_evaluation"
	StageRequestSent = "evaluation_to_book_request"
	StageBookRTT     = "book_request_to_response"
	StageBookStamp   = "bar_end_to_book_exchange_ts"
)

// Probe acts on signals. All of its visible state is counters and durations.
type Probe struct {
	market   Market
	recorder Recorder
	config   Config

	mu        sync.Mutex
	open      int
	counters  map[string]int64
	latencies map[string][]time.Duration
	wg        sync.WaitGroup
	failed    chan error
	stopped   atomic.Bool // set by the first sealing failure: nothing more is fetched
}

func New(market Market, recorder Recorder, config Config) *Probe {
	return &Probe{
		market: market, recorder: recorder, config: config,
		counters: map[string]int64{}, latencies: map[string][]time.Duration{},
		failed: make(chan error, 1),
	}
}

// Failed reports the first failure to seal a record. A measurement that cannot keep
// its records must stop: the caller ends the run with this error.
func (p *Probe) Failed() <-chan error { return p.failed }

func (p *Probe) count(name string) {
	p.mu.Lock()
	p.counters[name]++
	p.mu.Unlock()
}

func (p *Probe) observe(stage string, d time.Duration) {
	p.mu.Lock()
	p.latencies[stage] = append(p.latencies[stage], d)
	p.mu.Unlock()
}

// sealedSignal is the market content of a signal; it only ever goes to the recorder.
type sealedSignal struct {
	Contract     string    `json:"contract"`
	Symbol       string    `json:"symbol"`
	BarStart     time.Time `json:"bar_start"`
	Return       float64   `json:"return"`
	Turnover     float64   `json:"turnover"`
	Median       float64   `json:"median"`
	EvaluatedAt  time.Time `json:"evaluated_at"`
	LastTradeLag string    `json:"last_trade_lag"`
}

func sealSignal(s burstengine.Signal) sealedSignal {
	return sealedSignal{
		Contract: s.Contract, Symbol: s.Symbol, BarStart: s.BarStart, Return: s.Return,
		Turnover: s.Turnover, Median: s.Median, EvaluatedAt: s.EvaluatedAt,
		LastTradeLag: s.LastTradeLag.String(),
	}
}

// Handle takes one signal: blocked when every slot is busy, else an entry snapshot now
// and an exit snapshot and the settled funding after the hold.
func (p *Probe) Handle(ctx context.Context, signal burstengine.Signal) {
	if p.stopped.Load() {
		p.count("ignored_after_failure")
		return
	}
	p.count("signals")
	p.observe(StageLastTrade, signal.LastTradeLag)
	p.observe(StageEvaluated, signal.EvaluatedAt.Sub(signal.BarEnd))
	p.mu.Lock()
	blocked := p.open >= p.config.MaxOpen
	if !blocked {
		p.open++
	}
	p.mu.Unlock()
	if blocked {
		if p.write(map[string]any{"kind": "blocked", "signal": sealSignal(signal)}) {
			p.count("blocked")
		}
		return
	}
	p.wg.Add(1)
	go func() {
		defer p.wg.Done()
		defer func() {
			p.mu.Lock()
			p.open--
			p.mu.Unlock()
		}()
		p.run(ctx, signal)
	}()
}

func (p *Probe) run(ctx context.Context, signal burstengine.Signal) {
	requestAt := time.Now()
	p.observe(StageRequestSent, requestAt.Sub(signal.EvaluatedAt))
	entryCtx, cancel := context.WithTimeout(ctx, p.config.QuoteTimeout)
	book, bookErr := p.market.Book(entryCtx, signal.Symbol)
	cancel()
	record := map[string]any{"kind": "entry", "signal": sealSignal(signal)}
	if bookErr != nil {
		p.count("entry_quote_failed")
		record["book_error"] = bookErr.Error()
	} else {
		p.observe(StageBookRTT, book.ReceivedAt.Sub(book.RequestedAt))
		p.observe(StageBookStamp, time.UnixMilli(book.ExchangeTS).Sub(signal.BarEnd))
		record["book"] = book
	}
	fundingCtx, cancel := context.WithTimeout(ctx, p.config.QuoteTimeout)
	funding, fundingErr := p.market.Funding(fundingCtx, signal.Symbol)
	cancel()
	if fundingErr != nil {
		p.count("entry_funding_failed")
	} else {
		record["funding"] = funding
	}
	if !p.write(record) {
		return // the run is stopping: nothing more is fetched for an unsealed entry
	}

	exitAt := signal.BarEnd.Add(p.config.Hold)
	select {
	case <-ctx.Done():
		p.count("exit_missed_shutdown")
		return
	case <-time.After(time.Until(exitAt)):
	}
	if p.stopped.Load() {
		p.count("exit_skipped_after_failure")
		return
	}
	exitCtx, cancel := context.WithTimeout(ctx, p.config.QuoteTimeout)
	exitBook, exitErr := p.market.Book(exitCtx, signal.Symbol)
	cancel()
	exitRecord := map[string]any{"kind": "exit", "symbol": signal.Symbol, "bar_start": signal.BarStart}
	if exitErr != nil {
		p.count("exit_quote_failed")
		exitRecord["book_error"] = exitErr.Error()
	} else {
		exitRecord["book"] = exitBook
	}
	settleCtx, cancel := context.WithTimeout(ctx, p.config.QuoteTimeout)
	settled, settleErr := p.market.SettledBetween(settleCtx, signal.Symbol, requestAt, time.Now())
	cancel()
	if settleErr != nil {
		p.count("settled_funding_failed")
	} else {
		exitRecord["settled_funding"] = settled
	}
	if p.write(exitRecord) {
		p.count("completed")
	}
}

// write seals one record; a failure is reported once on Failed and counted.
func (p *Probe) write(record any) bool {
	if err := p.recorder.Write(record); err != nil {
		p.stopped.Store(true)
		p.count("sealed_write_failed")
		select {
		case p.failed <- fmt.Errorf("seal a record: %w", err):
		default:
		}
		return false
	}
	return true
}

// Wait blocks until every running entry and exit is done (after ctx is cancelled).
func (p *Probe) Wait() { p.wg.Wait() }

// Health is the visible state: counters and latency percentiles in milliseconds. It
// carries no instrument and no market value.
func (p *Probe) Health() map[string]int64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := map[string]int64{"open": int64(p.open)}
	for name, value := range p.counters {
		out[name] = value
	}
	for stage, values := range p.latencies {
		if len(values) == 0 {
			continue
		}
		sorted := slices.Clone(values)
		slices.Sort(sorted)
		at := func(q float64) int64 { return sorted[int(q*float64(len(sorted)-1))].Milliseconds() }
		out[stage+"_p50_ms"] = at(0.5)
		out[stage+"_p90_ms"] = at(0.9)
		out[stage+"_p99_ms"] = at(0.99)
		out[stage+"_max_ms"] = sorted[len(sorted)-1].Milliseconds()
		out[stage+"_n"] = int64(len(sorted))
	}
	return out
}

// Package burstengine builds 1-minute bars from the streaming runtime's ordered events
// and evaluates the 1-minute burst rule of docs/research/hyp030-burst-design-v1.md:
//
//   - a closed bar's return over the previous bar's close is at least ReturnThreshold;
//   - the previous bar and this bar are both complete;
//   - this bar's turnover is at least TurnoverMultiple times the median turnover of the
//     complete bars of the prior MedianWindow minutes, with at least MinMedianBars;
//   - at most one firing per instrument per Cooldown.
//
// Bars are bucketed by exchange time. A minute without trades becomes a bar with zero
// turnover and the last known price, as the research bars have one (their price comes
// from the ticker). A bar is evaluated when the clock passes its end plus Grace; a
// trade older than the newest bar is counted late and never added.
//
// Completeness is part of the data. A Disconnected or Overflow event marks the open
// bar and every bar until the next Connected incomplete, for the affected symbols; the
// bar of the minute a Connected happens in is incomplete too (it may have started
// before the subscription). No signal fires on an incomplete bar or an incomplete
// previous bar, and incomplete bars never enter the turnover median.
//
// The engine is single-goroutine and deterministic: the caller feeds events in order
// and calls Tick with the clock.
package burstengine

import (
	"slices"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/streamrt"
)

// Config holds the rule and the bar mechanics.
type Config struct {
	ReturnThreshold  float64
	TurnoverMultiple float64
	MedianWindow     int // minutes before the trigger bar
	MinMedianBars    int
	Cooldown         time.Duration
	Grace            time.Duration
}

// HYP030 is the rule exactly as designed.
func HYP030() Config {
	return Config{
		ReturnThreshold: 0.05, TurnoverMultiple: 5, MedianWindow: 60, MinMedianBars: 30,
		Cooldown: time.Hour, Grace: 250 * time.Millisecond,
	}
}

// Bar is one minute.
type Bar struct {
	Start      time.Time
	Open       float64
	High       float64
	Low        float64
	Close      float64
	Turnover   float64
	Trades     int
	Complete   bool
	LastRecvAt time.Time // receive time of the bar's last trade; zero for an empty bar
}

// Signal is one firing with its timing.
type Signal struct {
	Exchange     string
	Symbol       string
	BarStart     time.Time
	BarEnd       time.Time
	Return       float64
	Turnover     float64
	Median       float64
	EvaluatedAt  time.Time     // the Tick that produced it
	LastTradeLag time.Duration // the bar's last trade received, relative to the bar end
}

// Stats are counters only.
type Stats struct {
	Trades          int64
	LateTrades      int64
	Bars            int64
	EmptyBars       int64
	IncompleteBars  int64
	Signals         int64
	SuppressedByGap int64
}

type symbolState struct {
	exchange   string
	current    *Bar
	pending    []Bar // closed by a later trade, not yet evaluated
	history    []Bar // evaluated bars, oldest first
	lastFire   time.Time
	gapOpen    bool
	freshStart time.Time // the minute of the latest Connected
}

// Engine is the bar builder and rule evaluator.
type Engine struct {
	config  Config
	symbols map[string]*symbolState
	Stats   Stats
}

func New(config Config) *Engine {
	return &Engine{config: config, symbols: map[string]*symbolState{}}
}

func (e *Engine) state(symbol string) *symbolState {
	s, ok := e.symbols[symbol]
	if !ok {
		s = &symbolState{}
		e.symbols[symbol] = s
	}
	return s
}

// OnEvent consumes one runtime event.
func (e *Engine) OnEvent(event streamrt.Event) {
	switch {
	case event.Lifecycle != nil:
		e.onLifecycle(event.Lifecycle)
	case event.Trade != nil:
		e.onTrade(event.Trade)
	}
}

func (e *Engine) onLifecycle(l *streamrt.Lifecycle) {
	minute := l.At.Truncate(time.Minute)
	for _, symbol := range l.Symbols {
		s := e.state(symbol)
		switch l.Kind {
		case streamrt.Disconnected, streamrt.Overflow:
			s.gapOpen = true
			if s.current != nil {
				s.current.Complete = false
			}
		case streamrt.Connected:
			s.gapOpen = false
			s.freshStart = minute
			if s.current != nil && !s.current.Start.After(minute) {
				s.current.Complete = false
			}
		}
	}
}

func (e *Engine) newestStart(s *symbolState) (time.Time, bool) {
	switch {
	case s.current != nil:
		return s.current.Start, true
	case len(s.pending) > 0:
		return s.pending[len(s.pending)-1].Start, true
	case len(s.history) > 0:
		return s.history[len(s.history)-1].Start, true
	}
	return time.Time{}, false
}

func (e *Engine) lastClose(s *symbolState) float64 {
	switch {
	case s.current != nil:
		return s.current.Close
	case len(s.pending) > 0:
		return s.pending[len(s.pending)-1].Close
	case len(s.history) > 0:
		return s.history[len(s.history)-1].Close
	}
	return 0
}

// closeCurrent moves the open bar to the pending queue.
func (e *Engine) closeCurrent(s *symbolState) {
	if s.current != nil {
		s.pending = append(s.pending, *s.current)
		s.current = nil
	}
}

// fillTo adds empty bars for every minute after the newest bar and before start.
func (e *Engine) fillTo(s *symbolState, start time.Time) {
	newest, ok := e.newestStart(s)
	if !ok {
		return
	}
	price := e.lastClose(s)
	for minute := newest.Add(time.Minute); minute.Before(start); minute = minute.Add(time.Minute) {
		e.Stats.EmptyBars++
		s.pending = append(s.pending, Bar{
			Start: minute, Open: price, High: price, Low: price, Close: price,
			Complete: !s.gapOpen && !minute.Equal(s.freshStart),
		})
	}
}

func (e *Engine) onTrade(t *streamrt.Trade) {
	e.Stats.Trades++
	s := e.state(t.Symbol)
	s.exchange = t.Exchange
	start := t.EventAt.Truncate(time.Minute)
	if newest, ok := e.newestStart(s); ok && start.Before(newest) {
		e.Stats.LateTrades++
		return
	}
	if s.current == nil || start.After(s.current.Start) {
		e.closeCurrent(s)
		e.fillTo(s, start)
		s.current = &Bar{
			Start: start, Open: t.Price, High: t.Price, Low: t.Price,
			Complete: !s.gapOpen && !start.Equal(s.freshStart),
		}
	}
	bar := s.current
	bar.High = max(bar.High, t.Price)
	bar.Low = min(bar.Low, t.Price)
	bar.Close = t.Price
	bar.Turnover += t.Notional
	bar.Trades++
	bar.LastRecvAt = t.ReceivedAt
}

// Tick evaluates every bar whose end plus Grace is at or before now: pending bars,
// the open bar, and empty minutes up to now for symbols that went quiet. It returns
// the signals in symbol order.
func (e *Engine) Tick(now time.Time) []Signal {
	symbols := make([]string, 0, len(e.symbols))
	for symbol := range e.symbols {
		symbols = append(symbols, symbol)
	}
	slices.Sort(symbols)
	due := now.Add(-time.Minute - e.config.Grace)
	var out []Signal
	for _, symbol := range symbols {
		s := e.symbols[symbol]
		if s.current != nil && !s.current.Start.After(due) {
			e.closeCurrent(s)
		}
		if _, ok := e.newestStart(s); ok {
			e.fillTo(s, due.Truncate(time.Minute).Add(time.Minute))
		}
		for len(s.pending) > 0 && !s.pending[0].Start.After(due) {
			bar := s.pending[0]
			s.pending = s.pending[1:]
			if signal, ok := e.admit(symbol, s, bar, now); ok {
				out = append(out, signal)
			}
		}
	}
	return out
}

// admit appends an evaluated bar to the history and applies the rule to it.
func (e *Engine) admit(symbol string, s *symbolState, bar Bar, now time.Time) (Signal, bool) {
	e.Stats.Bars++
	if !bar.Complete {
		e.Stats.IncompleteBars++
	}
	s.history = append(s.history, bar)
	if keep := e.config.MedianWindow + 1; len(s.history) > keep {
		s.history = s.history[len(s.history)-keep:]
	}
	n := len(s.history)
	if n < 2 {
		return Signal{}, false
	}
	prev := s.history[n-2]
	if !prev.Start.Equal(bar.Start.Add(-time.Minute)) || prev.Close <= 0 || bar.Trades == 0 {
		return Signal{}, false
	}
	ret := bar.Close/prev.Close - 1
	if ret < e.config.ReturnThreshold {
		return Signal{}, false
	}
	if !bar.Complete || !prev.Complete {
		e.Stats.SuppressedByGap++
		return Signal{}, false
	}
	earliest := bar.Start.Add(-time.Duration(e.config.MedianWindow) * time.Minute)
	window := make([]float64, 0, e.config.MedianWindow)
	for _, past := range s.history[:n-1] {
		if past.Complete && !past.Start.Before(earliest) {
			window = append(window, past.Turnover)
		}
	}
	if len(window) < e.config.MinMedianBars {
		return Signal{}, false
	}
	median := medianOf(window)
	if median <= 0 || bar.Turnover < e.config.TurnoverMultiple*median {
		return Signal{}, false
	}
	if !s.lastFire.IsZero() && bar.Start.Before(s.lastFire.Add(e.config.Cooldown)) {
		return Signal{}, false
	}
	s.lastFire = bar.Start
	e.Stats.Signals++
	end := bar.Start.Add(time.Minute)
	return Signal{
		Exchange: s.exchange, Symbol: symbol, BarStart: bar.Start, BarEnd: end, Return: ret,
		Turnover: bar.Turnover, Median: median, EvaluatedAt: now,
		LastTradeLag: bar.LastRecvAt.Sub(end),
	}, true
}

func medianOf(values []float64) float64 {
	sorted := slices.Clone(values)
	slices.Sort(sorted)
	mid := len(sorted) / 2
	if len(sorted)%2 == 1 {
		return sorted[mid]
	}
	return (sorted[mid-1] + sorted[mid]) / 2
}

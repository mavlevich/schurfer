// Package burstengine builds 1-minute bars from the streaming runtime's ordered events
// and evaluates the 1-minute burst rule of docs/research/hyp030-burst-design-v1.md on
// its own data contract, ContractVersion. The research bars took OHLC from the ticker;
// these bars take it from trades and synthesize empty minutes, so this is a new
// contract and the historical flow and power are only approximations for it.
//
// The rule: a finalized bar's return over the previous bar's close is at least
// ReturnThreshold; both bars complete; the bar's turnover at least TurnoverMultiple
// times the median turnover of the complete bars of the prior MedianWindow minutes (at
// least MinMedianBars of them); at most one firing per instrument per Cooldown.
//
// Bars:
//
//   - A trade belongs to the minute of its exchange time. Open and Close are the
//     first and last trade by (exchange time, venue sequence, arrival order).
//   - A minute is finalized when the clock passes its end plus Grace. Until then a trade
//     of that minute updates it, even after trades of a later minute have arrived;
//     after it a trade of that minute (or earlier) is counted late and never added.
//   - A minute without trades becomes a bar with zero turnover and the previous bar's
//     close, as the research bars have one.
//   - A repeated trade id is dropped before it touches any price or turnover (a bounded
//     memory of recent ids per instrument). A trade without an id is refused.
//
// Completeness is part of the data, from gap intervals per instrument. Receive times
// are moved back by LagAllowance, because a trade executed shortly before the last
// received frame can be among the ones lost:
//
//   - from the start until the first Subscribed;
//   - from a Disconnected's last received frame until the next Subscribed;
//   - over an Overflow: the union of its dropped trades' exchange times and its
//     receive-time span; when a lifecycle event was dropped too, the connection state
//     is unknown, so the gap stays open until the next Subscribed.
//
// Completeness is evaluated against the gaps known at the time of the decision, for
// the bar, its previous bar and every bar of the median window, so a gap discovered
// after a minute was finalized still removes that minute. No signal fires on an
// incomplete bar or an incomplete previous bar, and incomplete bars never enter the
// turnover median.
//
// The engine is single-goroutine and deterministic: the caller feeds events in order
// and calls Tick with the clock.
package burstengine

import (
	"slices"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/streamrt"
)

// ContractVersion names this engine's data contract.
const ContractVersion = "burst_trade_bars_v1"

const recentIDs = 8192

// Config holds the rule and the bar mechanics.
type Config struct {
	ReturnThreshold  float64
	TurnoverMultiple float64
	MedianWindow     int // minutes before the trigger bar
	MinMedianBars    int
	Cooldown         time.Duration
	Grace            time.Duration
	LagAllowance     time.Duration // receive-time gap starts move back by this much
}

// HYP030 is the designed rule's thresholds on this engine's contract.
func HYP030() Config {
	return Config{
		ReturnThreshold: 0.05, TurnoverMultiple: 5, MedianWindow: 60, MinMedianBars: 30,
		Cooldown: time.Hour, Grace: 250 * time.Millisecond, LagAllowance: 2 * time.Second,
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
	LastRecvAt time.Time // the latest receive time among the bar's trades
	first      orderKey
	last       orderKey
}

type orderKey struct {
	at      time.Time
	seq     int64
	arrival uint64
}

func (k orderKey) before(o orderKey) bool {
	if !k.at.Equal(o.at) {
		return k.at.Before(o.at)
	}
	if k.seq != o.seq {
		return k.seq < o.seq
	}
	return k.arrival < o.arrival
}

// Signal is one firing with its timing.
type Signal struct {
	Contract     string
	Exchange     string
	Symbol       string
	BarStart     time.Time
	BarEnd       time.Time
	Return       float64
	Turnover     float64
	Median       float64
	EvaluatedAt  time.Time     // the Tick that produced it
	LastTradeLag time.Duration // the bar's latest trade received, relative to the bar end
}

// Stats are counters only.
type Stats struct {
	Trades          int64
	MissingID       int64
	Duplicates      int64
	LateTrades      int64
	Bars            int64
	EmptyBars       int64
	IncompleteBars  int64
	Signals         int64
	SuppressedByGap int64
}

type gap struct {
	from time.Time
	to   time.Time // zero while open
}

type symbolState struct {
	exchange     string
	open         []*Bar // unfinalized minutes, oldest first, contiguous
	history      []Bar  // finalized bars, oldest first
	finalThrough time.Time
	lastFire     time.Time
	gaps         []gap
	seen         map[string]struct{}
	seenRing     []string
	seenNext     int
}

// Engine is the bar builder and rule evaluator.
type Engine struct {
	config  Config
	symbols map[string]*symbolState
	arrival uint64
	Stats   Stats
}

func New(config Config) *Engine {
	return &Engine{config: config, symbols: map[string]*symbolState{}}
}

func (e *Engine) state(symbol string) *symbolState {
	s, ok := e.symbols[symbol]
	if !ok {
		// gap from the beginning until the first Subscribed
		s = &symbolState{gaps: []gap{{}}, seen: map[string]struct{}{}, seenRing: make([]string, recentIDs)}
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

func (s *symbolState) openGap(from time.Time) {
	if n := len(s.gaps); n > 0 && s.gaps[n-1].to.IsZero() {
		return // already open
	}
	s.gaps = append(s.gaps, gap{from: from})
}

func (s *symbolState) closeGap(at time.Time) {
	if n := len(s.gaps); n > 0 && s.gaps[n-1].to.IsZero() {
		s.gaps[n-1].to = at
	}
}

func (e *Engine) onLifecycle(l *streamrt.Lifecycle) {
	for _, symbol := range l.Symbols {
		s := e.state(symbol)
		switch l.Kind {
		case streamrt.Disconnected:
			from := l.LastFrameAt
			if from.IsZero() {
				from = l.At
			}
			s.openGap(from.Add(-e.config.LagAllowance))
		case streamrt.Subscribed:
			s.closeGap(l.At)
		case streamrt.Overflow:
			from, to := l.At, l.At
			if !l.Since.IsZero() {
				from = l.Since.Add(-e.config.LagAllowance)
			}
			if !l.DroppedFrom.IsZero() && l.DroppedFrom.Before(from) {
				from = l.DroppedFrom
			}
			if l.DroppedTo.After(to) {
				to = l.DroppedTo
			}
			if l.DroppedLifecycle {
				s.openGap(from) // the connection state is unknown until the next Subscribed
				continue
			}
			if n := len(s.gaps); n > 0 && s.gaps[n-1].to.IsZero() {
				continue // inside an open gap already
			}
			s.gaps = append(s.gaps, gap{from: from, to: to.Add(time.Nanosecond)})
		case streamrt.Connected:
			// data is complete only from Subscribed
		}
	}
}

// remember reports whether the id is new, keeping a bounded memory of recent ids.
func (s *symbolState) remember(id string) bool {
	if _, ok := s.seen[id]; ok {
		return false
	}
	if old := s.seenRing[s.seenNext]; old != "" {
		delete(s.seen, old)
	}
	s.seenRing[s.seenNext] = id
	s.seenNext = (s.seenNext + 1) % len(s.seenRing)
	s.seen[id] = struct{}{}
	return true
}

func (e *Engine) onTrade(t *streamrt.Trade) {
	e.Stats.Trades++
	if t.TradeID == "" {
		e.Stats.MissingID++
		return
	}
	s := e.state(t.Symbol)
	s.exchange = t.Exchange
	start := t.EventAt.Truncate(time.Minute)
	if !s.finalThrough.IsZero() && !start.After(s.finalThrough) {
		e.Stats.LateTrades++
		return
	}
	if !s.remember(t.TradeID) {
		e.Stats.Duplicates++
		return
	}
	bar := e.barFor(s, start)
	e.arrival++
	key := orderKey{at: t.EventAt, seq: t.Seq, arrival: e.arrival}
	if bar.Trades == 0 {
		bar.Open, bar.High, bar.Low, bar.Close = t.Price, t.Price, t.Price, t.Price
		bar.first, bar.last = key, key
	} else {
		bar.High = max(bar.High, t.Price)
		bar.Low = min(bar.Low, t.Price)
		if key.before(bar.first) {
			bar.first, bar.Open = key, t.Price
		}
		if bar.last.before(key) {
			bar.last, bar.Close = key, t.Price
		}
	}
	bar.Turnover += t.Notional
	bar.Trades++
	if t.ReceivedAt.After(bar.LastRecvAt) {
		bar.LastRecvAt = t.ReceivedAt
	}
}

// barFor returns the open bar of a minute after finalThrough, creating it and every
// minute between it and the open span, so the open span stays contiguous.
func (e *Engine) barFor(s *symbolState, start time.Time) *Bar {
	if len(s.open) > 0 && start.Before(s.open[0].Start) {
		// earlier than the open span (possible before anything was finalized)
		var prefix []*Bar
		for minute := start; minute.Before(s.open[0].Start); minute = minute.Add(time.Minute) {
			prefix = append(prefix, &Bar{Start: minute})
		}
		s.open = append(prefix, s.open...)
		return s.open[0]
	}
	for i := len(s.open) - 1; i >= 0; i-- {
		if s.open[i].Start.Equal(start) {
			return s.open[i]
		}
		if s.open[i].Start.Before(start) {
			break
		}
	}
	newest, ok := e.newest(s)
	if !ok {
		bar := &Bar{Start: start}
		s.open = append(s.open, bar)
		return bar
	}
	if start.Before(newest) {
		// inside the open span: every minute there exists already (contiguous)
		for _, bar := range s.open {
			if bar.Start.Equal(start) {
				return bar
			}
		}
	}
	for minute := newest.Add(time.Minute); !minute.After(start); minute = minute.Add(time.Minute) {
		s.open = append(s.open, &Bar{Start: minute})
	}
	return s.open[len(s.open)-1]
}

func (e *Engine) newest(s *symbolState) (time.Time, bool) {
	if n := len(s.open); n > 0 {
		return s.open[n-1].Start, true
	}
	if !s.finalThrough.IsZero() {
		return s.finalThrough, true
	}
	return time.Time{}, false
}

// complete reports whether no gap interval overlaps the minute [start, start+1m).
func (s *symbolState) complete(start time.Time) bool {
	end := start.Add(time.Minute)
	for _, g := range s.gaps {
		if g.from.Before(end) && (g.to.IsZero() || g.to.After(start)) {
			return false
		}
	}
	return true
}

// Tick finalizes every minute whose end plus Grace is at or before now, oldest first,
// including empty minutes up to that point for instruments that went quiet, and
// returns the signals in instrument order.
func (e *Engine) Tick(now time.Time) []Signal {
	symbols := make([]string, 0, len(e.symbols))
	for symbol := range e.symbols {
		symbols = append(symbols, symbol)
	}
	slices.Sort(symbols)
	due := now.Add(-time.Minute - e.config.Grace).Truncate(time.Minute)
	var out []Signal
	for _, symbol := range symbols {
		s := e.symbols[symbol]
		if newest, ok := e.newest(s); ok && newest.Before(due) {
			e.barFor(s, due) // empty minutes up to the due minute
		}
		for len(s.open) > 0 && !s.open[0].Start.After(due) {
			bar := *s.open[0]
			s.open = s.open[1:]
			s.finalThrough = bar.Start
			if signal, ok := e.admit(symbol, s, bar, now); ok {
				out = append(out, signal)
			}
		}
		e.pruneGaps(s)
	}
	return out
}

func (e *Engine) pruneGaps(s *symbolState) {
	horizon := s.finalThrough.Add(-time.Duration(e.config.MedianWindow+2) * time.Minute)
	kept := s.gaps[:0]
	for _, g := range s.gaps {
		if g.to.IsZero() || g.to.After(horizon) {
			kept = append(kept, g)
		}
	}
	s.gaps = kept
}

// admit finalizes a bar into the history and applies the rule to it.
func (e *Engine) admit(symbol string, s *symbolState, bar Bar, now time.Time) (Signal, bool) {
	e.Stats.Bars++
	if bar.Trades == 0 {
		e.Stats.EmptyBars++
		if n := len(s.history); n > 0 {
			price := s.history[n-1].Close
			bar.Open, bar.High, bar.Low, bar.Close = price, price, price, price
		}
	}
	if !s.complete(bar.Start) {
		e.Stats.IncompleteBars++
	}
	s.history = append(s.history, bar)
	if keep := e.config.MedianWindow + 1; len(s.history) > keep {
		s.history = s.history[len(s.history)-keep:]
	}
	n := len(s.history)
	if n < 2 || bar.Trades == 0 {
		return Signal{}, false
	}
	prev := s.history[n-2]
	if !prev.Start.Equal(bar.Start.Add(-time.Minute)) || prev.Close <= 0 {
		return Signal{}, false
	}
	ret := bar.Close/prev.Close - 1
	if ret < e.config.ReturnThreshold {
		return Signal{}, false
	}
	// completeness against the gaps known now, not when each bar was finalized
	if !s.complete(bar.Start) || !s.complete(prev.Start) {
		e.Stats.SuppressedByGap++
		return Signal{}, false
	}
	earliest := bar.Start.Add(-time.Duration(e.config.MedianWindow) * time.Minute)
	window := make([]float64, 0, e.config.MedianWindow)
	for _, past := range s.history[:n-1] {
		if !past.Start.Before(earliest) && s.complete(past.Start) {
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
		Contract: ContractVersion, Exchange: s.exchange, Symbol: symbol, BarStart: bar.Start,
		BarEnd: end, Return: ret, Turnover: bar.Turnover, Median: median, EvaluatedAt: now,
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

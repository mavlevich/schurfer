package hotset

import (
	"errors"
	"math"
	"strings"
	"time"
)

var (
	ErrInvalidTick    = errors.New("invalid ticker event")
	ErrOutOfOrderTick = errors.New("out-of-order ticker event")
)

type Config struct {
	BucketSize time.Duration
	Prebuffer  time.Duration
	HotTTL     time.Duration
	MaxSymbols int
}

func (cfg Config) validate() error {
	if cfg.BucketSize <= 0 {
		return errors.New("bucket size must be positive")
	}
	if cfg.Prebuffer < cfg.BucketSize {
		return errors.New("prebuffer must be at least one bucket")
	}
	if cfg.HotTTL <= 0 {
		return errors.New("hot TTL must be positive")
	}
	if cfg.MaxSymbols <= 0 {
		return errors.New("max symbols must be positive")
	}
	return nil
}

type Tick struct {
	Symbol      string
	EventAt     time.Time
	ReceivedAt  time.Time
	LastPrice   float64
	Bid         *float64
	Ask         *float64
	Volume24h   *float64
	Turnover24h *float64
}

type Activation struct {
	Symbol      string
	Base        string
	PumpEventID int64
	Reason      string
	ExpiresAt   time.Time
}

type Bar struct {
	SchemaVersion    int
	Exchange         string
	Symbol           string
	Base             string
	PumpEventID      int64
	Activation       string
	BucketStart      time.Time
	FirstEventAt     time.Time
	LastEventAt      time.Time
	LastReceivedAt   time.Time
	Open             float64
	High             float64
	Low              float64
	Close            float64
	Bid              *float64
	Ask              *float64
	VolumeDelta24h   *float64
	TurnoverDelta24h *float64
	EventCount       int
	MaxLag           time.Duration
}

type Engine struct {
	cfg    Config
	states map[string]*symbolState
	hot    map[string]Activation
}

type symbolState struct {
	current        *barBuilder
	prebuffer      []Bar
	emittedThrough time.Time
}

type barBuilder struct {
	start          time.Time
	firstEventAt   time.Time
	lastEventAt    time.Time
	lastReceivedAt time.Time
	open           float64
	high           float64
	low            float64
	close          float64
	bid            *float64
	ask            *float64
	firstVolume    *float64
	lastVolume     *float64
	firstTurnover  *float64
	lastTurnover   *float64
	eventCount     int
	maxLag         time.Duration
}

func New(cfg Config) (*Engine, error) {
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	return &Engine{
		cfg:    cfg,
		states: make(map[string]*symbolState),
		hot:    make(map[string]Activation),
	}, nil
}

func (e *Engine) Observe(tick Tick) ([]Bar, error) {
	tick.Symbol = normalizeSymbol(tick.Symbol)
	if tick.Symbol == "" || tick.EventAt.IsZero() || tick.ReceivedAt.IsZero() ||
		!finitePositive(tick.LastPrice) {
		return nil, ErrInvalidTick
	}
	if invalidOptional(tick.Bid) || invalidOptional(tick.Ask) ||
		invalidOptional(tick.Volume24h) || invalidOptional(tick.Turnover24h) {
		return nil, ErrInvalidTick
	}

	state := e.states[tick.Symbol]
	if state == nil {
		state = &symbolState{}
		e.states[tick.Symbol] = state
	}
	start := tick.EventAt.Truncate(e.cfg.BucketSize)
	if state.current == nil {
		state.current = newBar(start, tick, nil, nil)
		return nil, nil
	}
	if start.Before(state.current.start) || tick.EventAt.Before(state.current.lastEventAt) {
		return nil, ErrOutOfOrderTick
	}
	if start.Equal(state.current.start) {
		state.current.add(tick)
		return nil, nil
	}

	completed := state.current.finish(tick.Symbol)
	previousVolume := cloneFloat(state.current.lastVolume)
	previousTurnover := cloneFloat(state.current.lastTurnover)
	e.appendPrebuffer(state, completed)
	state.current = newBar(start, tick, previousVolume, previousTurnover)

	activation, active := e.active(tick.Symbol, tick.ReceivedAt)
	if !active || !completed.BucketStart.After(state.emittedThrough) {
		return nil, nil
	}
	state.emittedThrough = completed.BucketStart
	return []Bar{withActivation(completed, activation)}, nil
}

func (e *Engine) Activate(activation Activation, now time.Time) ([]Bar, bool) {
	activation.Symbol = normalizeSymbol(activation.Symbol)
	activation.Base = strings.TrimSpace(activation.Base)
	activation.Reason = strings.TrimSpace(activation.Reason)
	if activation.Symbol == "" || activation.Base == "" || activation.PumpEventID <= 0 ||
		activation.Reason == "" || now.IsZero() {
		return nil, false
	}
	if activation.ExpiresAt.IsZero() {
		activation.ExpiresAt = now.Add(e.cfg.HotTTL)
	}
	maxExpiry := now.Add(e.cfg.HotTTL)
	if activation.ExpiresAt.After(maxExpiry) {
		activation.ExpiresAt = maxExpiry
	}
	if !activation.ExpiresAt.After(now) {
		return nil, false
	}
	state := e.states[activation.Symbol]
	if state == nil {
		return nil, false
	}

	if _, ok := e.hot[activation.Symbol]; ok {
		e.hot[activation.Symbol] = activation
		return nil, true
	}
	e.expire(now)
	if len(e.hot) >= e.cfg.MaxSymbols {
		return nil, false
	}

	e.hot[activation.Symbol] = activation

	bars := make([]Bar, 0, len(state.prebuffer))
	for _, bar := range state.prebuffer {
		if !bar.BucketStart.After(state.emittedThrough) {
			continue
		}
		bars = append(bars, withActivation(bar, activation))
		state.emittedThrough = bar.BucketStart
	}
	return bars, true
}

func (e *Engine) HotCount(now time.Time) int {
	e.expire(now)
	return len(e.hot)
}

func (e *Engine) ObservedSymbols() int {
	return len(e.states)
}

func (e *Engine) IsHot(symbol string, now time.Time) bool {
	_, ok := e.active(normalizeSymbol(symbol), now)
	return ok
}

func (e *Engine) active(symbol string, now time.Time) (Activation, bool) {
	activation, ok := e.hot[symbol]
	if !ok {
		return Activation{}, false
	}
	if !activation.ExpiresAt.After(now) {
		delete(e.hot, symbol)
		return Activation{}, false
	}
	return activation, true
}

func (e *Engine) expire(now time.Time) {
	for symbol, activation := range e.hot {
		if !activation.ExpiresAt.After(now) {
			delete(e.hot, symbol)
		}
	}
}

func (e *Engine) appendPrebuffer(state *symbolState, bar Bar) {
	state.prebuffer = append(state.prebuffer, bar)
	limit := int(e.cfg.Prebuffer / e.cfg.BucketSize)
	if extra := len(state.prebuffer) - limit; extra > 0 {
		copy(state.prebuffer, state.prebuffer[extra:])
		state.prebuffer = state.prebuffer[:limit]
	}
}

func newBar(start time.Time, tick Tick, previousVolume, previousTurnover *float64) *barBuilder {
	lag := tick.ReceivedAt.Sub(tick.EventAt)
	if lag < 0 {
		lag = 0
	}
	return &barBuilder{
		start:          start,
		firstEventAt:   tick.EventAt,
		lastEventAt:    tick.EventAt,
		lastReceivedAt: tick.ReceivedAt,
		open:           tick.LastPrice,
		high:           tick.LastPrice,
		low:            tick.LastPrice,
		close:          tick.LastPrice,
		bid:            cloneFloat(tick.Bid),
		ask:            cloneFloat(tick.Ask),
		firstVolume:    firstAvailable(previousVolume, tick.Volume24h),
		lastVolume:     cloneFloat(tick.Volume24h),
		firstTurnover:  firstAvailable(previousTurnover, tick.Turnover24h),
		lastTurnover:   cloneFloat(tick.Turnover24h),
		eventCount:     1,
		maxLag:         lag,
	}
}

func (bar *barBuilder) add(tick Tick) {
	bar.high = max(bar.high, tick.LastPrice)
	bar.low = min(bar.low, tick.LastPrice)
	bar.close = tick.LastPrice
	bar.lastEventAt = tick.EventAt
	bar.lastReceivedAt = tick.ReceivedAt
	bar.eventCount++
	if tick.Bid != nil {
		bar.bid = cloneFloat(tick.Bid)
	}
	if tick.Ask != nil {
		bar.ask = cloneFloat(tick.Ask)
	}
	if tick.Volume24h != nil {
		if bar.firstVolume == nil {
			bar.firstVolume = cloneFloat(tick.Volume24h)
		}
		bar.lastVolume = cloneFloat(tick.Volume24h)
	}
	if tick.Turnover24h != nil {
		if bar.firstTurnover == nil {
			bar.firstTurnover = cloneFloat(tick.Turnover24h)
		}
		bar.lastTurnover = cloneFloat(tick.Turnover24h)
	}
	lag := tick.ReceivedAt.Sub(tick.EventAt)
	if lag > bar.maxLag {
		bar.maxLag = lag
	}
}

func (bar *barBuilder) finish(symbol string) Bar {
	return Bar{
		SchemaVersion:    1,
		Exchange:         "bybit",
		Symbol:           symbol,
		BucketStart:      bar.start,
		FirstEventAt:     bar.firstEventAt,
		LastEventAt:      bar.lastEventAt,
		LastReceivedAt:   bar.lastReceivedAt,
		Open:             bar.open,
		High:             bar.high,
		Low:              bar.low,
		Close:            bar.close,
		Bid:              cloneFloat(bar.bid),
		Ask:              cloneFloat(bar.ask),
		VolumeDelta24h:   nonNegativeDelta(bar.firstVolume, bar.lastVolume),
		TurnoverDelta24h: nonNegativeDelta(bar.firstTurnover, bar.lastTurnover),
		EventCount:       bar.eventCount,
		MaxLag:           bar.maxLag,
	}
}

func withActivation(bar Bar, activation Activation) Bar {
	bar.Base = activation.Base
	bar.PumpEventID = activation.PumpEventID
	bar.Activation = activation.Reason
	return bar
}

func normalizeSymbol(value string) string {
	return strings.ToUpper(strings.TrimSpace(value))
}

func finitePositive(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0) && value > 0
}

func invalidOptional(value *float64) bool {
	return value != nil && (math.IsNaN(*value) || math.IsInf(*value, 0) || *value < 0)
}

func cloneFloat(value *float64) *float64 {
	if value == nil {
		return nil
	}
	copyValue := *value
	return &copyValue
}

func firstAvailable(previous, current *float64) *float64 {
	if previous != nil {
		return cloneFloat(previous)
	}
	return cloneFloat(current)
}

func nonNegativeDelta(first, last *float64) *float64 {
	if first == nil || last == nil || *last < *first {
		return nil
	}
	value := *last - *first
	return &value
}

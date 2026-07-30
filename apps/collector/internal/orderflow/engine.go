package orderflow

import (
	"errors"
	"math"
	"slices"
	"strings"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/bybit"
)

var (
	ErrInvalidTrade      = errors.New("invalid public trade")
	ErrDuplicateTrade    = errors.New("duplicate public trade")
	ErrOutOfOrderTrade   = errors.New("out-of-order public trade")
	ErrPrebufferNotReady = errors.New("order-flow prebuffer is not ready")
	ErrCaptureCapacity   = errors.New("order-flow capture capacity reached")
)

const ContractVersion = "bybit_orderflow_pilot_v1"

type Config struct {
	BucketSize      time.Duration
	Prebuffer       time.Duration
	CaptureAfter    time.Duration
	Controls        int
	MaxSymbols      int
	MaxActiveEvents int
	RecentTradeIDs  int
}

func (cfg Config) validate() error {
	switch {
	case cfg.BucketSize <= 0:
		return errors.New("bucket size must be positive")
	case cfg.Prebuffer < cfg.BucketSize:
		return errors.New("prebuffer must cover at least one bucket")
	case cfg.CaptureAfter <= 0:
		return errors.New("capture duration must be positive")
	case cfg.Controls < 0:
		return errors.New("control count cannot be negative")
	case cfg.MaxSymbols <= 0:
		return errors.New("max symbols must be positive")
	case cfg.MaxActiveEvents <= 0:
		return errors.New("max active events must be positive")
	case cfg.RecentTradeIDs <= 0:
		return errors.New("recent trade-id capacity must be positive")
	default:
		return nil
	}
}

type Activation struct {
	PumpEventID      int64
	Base             string
	Symbol           string
	FirstObservedAt  time.Time
	ExcludedControls []string
}

type Bucket struct {
	SchemaVersion int     `json:"schema_version"`
	Exchange      string  `json:"exchange"`
	Symbol        string  `json:"symbol"`
	BucketStartMS int64   `json:"bucket_start_ms"`
	FirstEventMS  int64   `json:"first_event_at_ms"`
	LastEventMS   int64   `json:"last_event_at_ms"`
	LastReceiveMS int64   `json:"last_received_at_ms"`
	Open          float64 `json:"open"`
	High          float64 `json:"high"`
	Low           float64 `json:"low"`
	Close         float64 `json:"close"`
	BuyNotional   float64 `json:"buy_notional"`
	SellNotional  float64 `json:"sell_notional"`
	BuyQuantity   float64 `json:"buy_quantity"`
	SellQuantity  float64 `json:"sell_quantity"`
	BuyTrades     uint32  `json:"buy_trades"`
	SellTrades    uint32  `json:"sell_trades"`
	MaxLagMS      int64   `json:"max_lag_ms"`
}

type Record struct {
	ContractVersion   string `json:"contract_version"`
	PumpEventID       int64  `json:"pump_event_id"`
	EventBase         string `json:"event_base"`
	EventSymbol       string `json:"event_symbol"`
	ObservedSymbol    string `json:"observed_symbol"`
	Role              string `json:"role"`
	FirstObservedAtMS int64  `json:"first_observed_at_ms"`
	CaptureExpiresMS  int64  `json:"capture_expires_at_ms"`
	Bucket            Bucket `json:"bucket"`
}

type Engine struct {
	cfg              Config
	started          time.Time
	states           map[string]*symbolState
	captures         map[int64]*capture
	completedBuckets uint64
}

type symbolState struct {
	current   *bucketBuilder
	prebuffer []Bucket
	recent    map[string]struct{}
	recentIDs []string
}

type bucketBuilder struct {
	start        time.Time
	firstEvent   time.Time
	lastEvent    time.Time
	lastReceived time.Time
	open         float64
	high         float64
	low          float64
	close        float64
	buyNotional  float64
	sellNotional float64
	buyQuantity  float64
	sellQuantity float64
	buyTrades    uint32
	sellTrades   uint32
	maxLag       time.Duration
}

type capture struct {
	activation Activation
	expiresAt  time.Time
	subjects   map[string]string
}

type controlCandidate struct {
	symbol string
	score  float64
}

func New(cfg Config, startedAt time.Time) (*Engine, error) {
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	if startedAt.IsZero() {
		return nil, errors.New("started-at timestamp is required")
	}
	return &Engine{
		cfg:      cfg,
		started:  startedAt,
		states:   make(map[string]*symbolState),
		captures: make(map[int64]*capture),
	}, nil
}

func (engine *Engine) Observe(trade bybit.PublicTrade) ([]Record, error) {
	trade.Symbol = normalizeSymbol(trade.Symbol)
	if trade.Symbol == "" || trade.EventAt.IsZero() || trade.ReceivedAt.IsZero() ||
		!finitePositive(trade.Price) || !finitePositive(trade.Size) ||
		(trade.Side != "buy" && trade.Side != "sell") {
		return nil, ErrInvalidTrade
	}

	state := engine.states[trade.Symbol]
	if state == nil {
		if len(engine.states) >= engine.cfg.MaxSymbols {
			return nil, ErrCaptureCapacity
		}
		state = &symbolState{recent: make(map[string]struct{})}
		engine.states[trade.Symbol] = state
	}
	if trade.TradeID != "" {
		if _, exists := state.recent[trade.TradeID]; exists {
			return nil, ErrDuplicateTrade
		}
	}

	start := trade.EventAt.Truncate(engine.cfg.BucketSize)
	if state.current != nil && start.Before(state.current.start) {
		return nil, ErrOutOfOrderTrade
	}
	if trade.TradeID != "" {
		state.remember(trade.TradeID, engine.cfg.RecentTradeIDs)
	}
	if state.current == nil {
		state.current = newBucket(start, trade)
		return nil, nil
	}
	if start.Equal(state.current.start) {
		state.current.add(trade)
		return nil, nil
	}

	completed := state.current.finish(trade.Symbol)
	engine.completedBuckets++
	engine.appendPrebuffer(state, completed)
	state.current = newBucket(start, trade)
	engine.expire(trade.ReceivedAt)
	return engine.recordsFor(completed), nil
}

func (engine *Engine) Activate(activation Activation) ([]Record, []string, error) {
	activation.Symbol = normalizeSymbol(activation.Symbol)
	activation.Base = strings.TrimSpace(activation.Base)
	if activation.PumpEventID <= 0 || activation.Symbol == "" || activation.Base == "" ||
		activation.FirstObservedAt.IsZero() {
		return nil, nil, errors.New("activation identity is incomplete")
	}
	if _, exists := engine.captures[activation.PumpEventID]; exists {
		return nil, nil, nil
	}
	engine.expire(activation.FirstObservedAt)
	if len(engine.captures) >= engine.cfg.MaxActiveEvents {
		return nil, nil, ErrCaptureCapacity
	}
	if activation.FirstObservedAt.Sub(engine.started) < engine.cfg.Prebuffer {
		return nil, nil, ErrPrebufferNotReady
	}
	target := engine.states[activation.Symbol]
	if target == nil || len(target.prebuffer) == 0 {
		return nil, nil, ErrPrebufferNotReady
	}

	excluded := map[string]struct{}{activation.Symbol: {}}
	for _, symbol := range activation.ExcludedControls {
		if symbol = normalizeSymbol(symbol); symbol != "" {
			excluded[symbol] = struct{}{}
		}
	}
	subjects := map[string]string{activation.Symbol: "event"}
	controls := engine.selectControls(activation.FirstObservedAt, target, excluded)
	for _, symbol := range controls {
		subjects[symbol] = "control"
	}
	currentCapture := &capture{
		activation: activation,
		expiresAt:  activation.FirstObservedAt.Add(engine.cfg.CaptureAfter),
		subjects:   subjects,
	}
	engine.captures[activation.PumpEventID] = currentCapture

	orderedSubjects := append([]string{activation.Symbol}, controls...)
	records := make([]Record, 0, len(orderedSubjects)*len(target.prebuffer))
	cutoff := activation.FirstObservedAt.Add(-engine.cfg.Prebuffer)
	for _, symbol := range orderedSubjects {
		role := subjects[symbol]
		state := engine.states[symbol]
		for _, bucket := range state.prebuffer {
			start := time.UnixMilli(bucket.BucketStartMS)
			if start.Before(cutoff) || !start.Before(activation.FirstObservedAt) {
				continue
			}
			records = append(records, newRecord(currentCapture, symbol, role, bucket))
		}
	}
	return records, controls, nil
}

func (engine *Engine) Expire(now time.Time) {
	engine.expire(now)
}

func (engine *Engine) ObservedSymbols() int {
	return len(engine.states)
}

func (engine *Engine) ActiveCaptures() int {
	return len(engine.captures)
}

func (engine *Engine) BufferedBuckets() int {
	total := 0
	for _, state := range engine.states {
		total += len(state.prebuffer)
	}
	return total
}

func (engine *Engine) CompletedBuckets() uint64 {
	return engine.completedBuckets
}

func (engine *Engine) recordsFor(bucket Bucket) []Record {
	start := time.UnixMilli(bucket.BucketStartMS)
	records := make([]Record, 0, len(engine.captures))
	for _, currentCapture := range engine.captures {
		role, selected := currentCapture.subjects[bucket.Symbol]
		if !selected {
			continue
		}
		firstFutureBucket := currentCapture.activation.FirstObservedAt.
			Truncate(engine.cfg.BucketSize).
			Add(engine.cfg.BucketSize)
		if start.Before(firstFutureBucket) || !start.Before(currentCapture.expiresAt) {
			continue
		}
		records = append(
			records,
			newRecord(currentCapture, bucket.Symbol, role, bucket),
		)
	}
	return records
}

func (engine *Engine) selectControls(
	now time.Time,
	target *symbolState,
	excluded map[string]struct{},
) []string {
	if engine.cfg.Controls == 0 {
		return nil
	}
	targetNotional, targetReturn := stateMetrics(target, now.Add(-engine.cfg.Prebuffer))
	candidates := make([]controlCandidate, 0, len(engine.states))
	for symbol, state := range engine.states {
		if _, skip := excluded[symbol]; skip || len(state.prebuffer) == 0 {
			continue
		}
		notional, priceReturn := stateMetrics(state, now.Add(-engine.cfg.Prebuffer))
		if notional <= 0 {
			continue
		}
		score := math.Abs(math.Log((notional+1)/(targetNotional+1))) +
			5*math.Abs(priceReturn-targetReturn)
		candidates = append(candidates, controlCandidate{symbol: symbol, score: score})
	}
	slices.SortFunc(candidates, func(left, right controlCandidate) int {
		switch {
		case left.score < right.score:
			return -1
		case left.score > right.score:
			return 1
		case left.symbol < right.symbol:
			return -1
		case left.symbol > right.symbol:
			return 1
		default:
			return 0
		}
	})
	count := min(engine.cfg.Controls, len(candidates))
	controls := make([]string, count)
	for index := range count {
		controls[index] = candidates[index].symbol
	}
	return controls
}

func (engine *Engine) expire(now time.Time) {
	for eventID, currentCapture := range engine.captures {
		if !currentCapture.expiresAt.After(now) {
			delete(engine.captures, eventID)
		}
	}
}

func (engine *Engine) appendPrebuffer(state *symbolState, bucket Bucket) {
	state.prebuffer = append(state.prebuffer, bucket)
	cutoff := time.UnixMilli(bucket.BucketStartMS).Add(-engine.cfg.Prebuffer)
	first := 0
	for first < len(state.prebuffer) &&
		time.UnixMilli(state.prebuffer[first].BucketStartMS).Before(cutoff) {
		first++
	}
	if first > 0 {
		copy(state.prebuffer, state.prebuffer[first:])
		state.prebuffer = state.prebuffer[:len(state.prebuffer)-first]
	}
}

func (state *symbolState) remember(tradeID string, limit int) {
	state.recent[tradeID] = struct{}{}
	state.recentIDs = append(state.recentIDs, tradeID)
	if extra := len(state.recentIDs) - limit; extra > 0 {
		for _, expired := range state.recentIDs[:extra] {
			delete(state.recent, expired)
		}
		copy(state.recentIDs, state.recentIDs[extra:])
		state.recentIDs = state.recentIDs[:limit]
	}
}

func newBucket(start time.Time, trade bybit.PublicTrade) *bucketBuilder {
	lag := max(trade.ReceivedAt.Sub(trade.EventAt), time.Duration(0))
	builder := &bucketBuilder{
		start:        start,
		firstEvent:   trade.EventAt,
		lastEvent:    trade.EventAt,
		lastReceived: trade.ReceivedAt,
		open:         trade.Price,
		high:         trade.Price,
		low:          trade.Price,
		close:        trade.Price,
		maxLag:       lag,
	}
	builder.addSide(trade)
	return builder
}

func (builder *bucketBuilder) add(trade bybit.PublicTrade) {
	builder.high = max(builder.high, trade.Price)
	builder.low = min(builder.low, trade.Price)
	if trade.EventAt.Before(builder.firstEvent) {
		builder.firstEvent = trade.EventAt
		builder.open = trade.Price
	}
	if !trade.EventAt.Before(builder.lastEvent) {
		builder.lastEvent = trade.EventAt
		builder.close = trade.Price
	}
	builder.lastReceived = maxTime(builder.lastReceived, trade.ReceivedAt)
	builder.maxLag = max(
		builder.maxLag,
		max(trade.ReceivedAt.Sub(trade.EventAt), time.Duration(0)),
	)
	builder.addSide(trade)
}

func (builder *bucketBuilder) addSide(trade bybit.PublicTrade) {
	notional := trade.Price * trade.Size
	if trade.Side == "buy" {
		builder.buyNotional += notional
		builder.buyQuantity += trade.Size
		builder.buyTrades++
		return
	}
	builder.sellNotional += notional
	builder.sellQuantity += trade.Size
	builder.sellTrades++
}

func (builder *bucketBuilder) finish(symbol string) Bucket {
	return Bucket{
		SchemaVersion: 1,
		Exchange:      "bybit",
		Symbol:        symbol,
		BucketStartMS: builder.start.UnixMilli(),
		FirstEventMS:  builder.firstEvent.UnixMilli(),
		LastEventMS:   builder.lastEvent.UnixMilli(),
		LastReceiveMS: builder.lastReceived.UnixMilli(),
		Open:          builder.open,
		High:          builder.high,
		Low:           builder.low,
		Close:         builder.close,
		BuyNotional:   builder.buyNotional,
		SellNotional:  builder.sellNotional,
		BuyQuantity:   builder.buyQuantity,
		SellQuantity:  builder.sellQuantity,
		BuyTrades:     builder.buyTrades,
		SellTrades:    builder.sellTrades,
		MaxLagMS:      builder.maxLag.Milliseconds(),
	}
}

func newRecord(currentCapture *capture, symbol, role string, bucket Bucket) Record {
	return Record{
		ContractVersion:   ContractVersion,
		PumpEventID:       currentCapture.activation.PumpEventID,
		EventBase:         currentCapture.activation.Base,
		EventSymbol:       currentCapture.activation.Symbol,
		ObservedSymbol:    symbol,
		Role:              role,
		FirstObservedAtMS: currentCapture.activation.FirstObservedAt.UnixMilli(),
		CaptureExpiresMS:  currentCapture.expiresAt.UnixMilli(),
		Bucket:            bucket,
	}
}

func stateMetrics(state *symbolState, cutoff time.Time) (float64, float64) {
	totalNotional := 0.0
	firstPrice := 0.0
	lastPrice := 0.0
	for _, bucket := range state.prebuffer {
		if time.UnixMilli(bucket.BucketStartMS).Before(cutoff) {
			continue
		}
		totalNotional += bucket.BuyNotional + bucket.SellNotional
		if firstPrice == 0 {
			firstPrice = bucket.Open
		}
		lastPrice = bucket.Close
	}
	priceReturn := 0.0
	if firstPrice > 0 && lastPrice > 0 {
		priceReturn = lastPrice/firstPrice - 1
	}
	return totalNotional, priceReturn
}

func normalizeSymbol(value string) string {
	return strings.ToUpper(strings.TrimSpace(value))
}

func finitePositive(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0) && value > 0
}

func maxTime(left, right time.Time) time.Time {
	if right.After(left) {
		return right
	}
	return left
}

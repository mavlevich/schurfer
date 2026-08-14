package bybit

import (
	"context"
	"sync"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/momentumsource"
)

// MarketType is Bybit's own entry in the momentumvenue capability matrix
// ("linear_usdt_perpetual"), reused here rather than restated so this
// cannot silently drift from the matrix's own frozen value.
const MarketType = "linear_usdt_perpetual"

const exchangeName = "bybit"

// Adapter exposes an already-constructed *Source through the canonical
// momentumsource interfaces. Every method below calls straight into
// Source's own existing, already-tested streaming/fetch methods and only
// translates their output -- this file changes no behavior in bybit.go,
// trades.go, or ws.go, and the momentum-capture binary's own wiring is
// untouched by this package existing (see docs/research/momentum-venue-
// capability-matrix-v1.md's "Gate to the next PR": port without changing
// stored semantics).
//
// Adapter deliberately does NOT implement momentumsource.OpenInterestSource.
// Bybit's own OI reading arrives embedded in the same ticker push that
// TickerSource already streams (see ws.go's tickerState) -- a second,
// independent StreamOpenInterest call here would open a REDUNDANT physical
// WebSocket subscription to the same topics just to satisfy the interface,
// doubling Bybit-side connection load for data this adapter is already
// receiving. OpenInterestFromTicker below derives the same reading from an
// already-consumed TickerUpdate instead. A venue whose OI genuinely is a
// separate transport (Binance's REST poll, per the capability preflight)
// implements OpenInterestSource for real; Bybit does not need to pretend to.
type Adapter struct {
	source *Source
}

func NewAdapter(source *Source) *Adapter {
	return &Adapter{source: source}
}

var (
	_ momentumsource.TradeSource    = (*Adapter)(nil)
	_ momentumsource.TickerSource   = (*Adapter)(nil)
	_ momentumsource.UniverseSource = (*Adapter)(nil)
)

func (a *Adapter) StreamTrades(
	ctx context.Context,
	symbols []string,
	consume momentumsource.TradeConsumer,
) error {
	// Bybit's own PublicTrade carries no per-trade session id (only
	// TradeLifecycleEvent does, at the shard level) -- see trades.go's own
	// TradeLifecycleEvent doc comment. RunTradesWithLifecycle runs one
	// goroutine per shard (a fixed symbol subset on one physical
	// connection), each firing its own "connected" lifecycle event
	// synchronously before any trade for that shard's own symbols can
	// arrive; tracking the latest session per NATIVE MARKET ID (not a
	// single shared variable) is required because multiple shards run
	// concurrently, each with its own session id, and this callback pair
	// is shared across all of them.
	var mu sync.Mutex
	sessionBySymbol := make(map[string]string)

	onLifecycle := func(event TradeLifecycleEvent) {
		if !event.DisconnectedAt.IsZero() {
			// A disconnect event: the stale session id is harmless to leave
			// mapped, since no further trade for these symbols can arrive on
			// this shard until a fresh "connected" event overwrites it first.
			return
		}
		mu.Lock()
		defer mu.Unlock()
		for _, symbol := range event.Symbols {
			// Regression for the code-review finding: TradeLifecycleEvent.
			// Symbols preserves the CALLER's own casing verbatim (it is just
			// this StreamTrades call's own `symbols` argument, unmodified by
			// RunTradesWithLifecycle), but handleTradePayload always
			// normalizes PublicTrade.Symbol to upper case regardless of what
			// case the subscribe request used. Without matching that same
			// normalization here, a caller passing non-upper-case symbols
			// would silently get an empty SessionID on every trade for that
			// market -- a mismatched map key, not a visible error.
			sessionBySymbol[normalizeSymbol(symbol)] = event.ShardSessionID
		}
	}

	onTrade := func(ctx context.Context, trade PublicTrade) error {
		mu.Lock()
		session := sessionBySymbol[normalizeSymbol(trade.Symbol)]
		mu.Unlock()
		return consume(ctx, translateTrade(trade, session))
	}

	return a.source.RunTradesWithLifecycle(ctx, symbols, onTrade, onLifecycle)
}

func translateTrade(trade PublicTrade, sessionID string) momentumsource.Trade {
	return momentumsource.Trade{
		Envelope: momentumsource.Envelope{
			Exchange:       exchangeName,
			MarketType:     MarketType,
			NativeMarketID: trade.Symbol,
			EventAt:        trade.EventAt,
			ReceivedAt:     trade.ReceivedAt,
			SessionID:      sessionID,
		},
		TradeID: trade.TradeID,
		Side:    trade.Side,
		Price:   trade.Price,
		Size:    trade.Size,
	}
}

// StreamTicker wraps Source.Run unchanged, which surfaces an IMPORTANT
// asymmetry with StreamTrades: ws.go's own handleTicker only LOGS a
// publish error (slog.Warn) and keeps the connection running -- it does
// not return the error, so a non-nil error from consume here is silently
// swallowed rather than treated as a stream failure that triggers a
// reconnect. Contrast with StreamTrades, where trades.go's handleTradePayload
// DOES propagate a consume error and causes tradeStreamLoop to reconnect.
// This is existing bybit.go/ws.go behavior this adapter deliberately does
// not change (see this package's own "port without changing behavior"
// constraint) -- a future canonical consumer must not assume "a returned
// error stops/reconnects the stream" holds for TickerSource the way it
// does for TradeSource.
func (a *Adapter) StreamTicker(
	ctx context.Context,
	symbols []string,
	consume momentumsource.TickerConsumer,
) error {
	return a.source.Run(ctx, symbols, func(ctx context.Context, event TickerEvent) error {
		return consume(ctx, translateTicker(event))
	})
}

func translateTicker(event TickerEvent) momentumsource.TickerUpdate {
	update := momentumsource.TickerUpdate{
		Envelope: momentumsource.Envelope{
			Exchange:       exchangeName,
			MarketType:     MarketType,
			NativeMarketID: event.Symbol,
			EventAt:        time.UnixMilli(event.TS),
			ReceivedAt:     time.UnixMilli(event.ReceivedAtMs),
			SessionID:      event.StreamSessionID,
		},
		LastPrice:     event.LastPrice,
		Price24hPct:   event.Price24hPct,
		High24h:       event.High24h,
		Low24h:        event.Low24h,
		Volume24h:     event.Volume24h,
		Turnover24h:   event.Turnover24h,
		Bid:           event.Bid,
		Ask:           event.Ask,
		MessageType:   event.MessageType,
		CrossSequence: event.CrossSequence,
	}
	if reading, ok := OpenInterestFromTicker(event); ok {
		update.OpenInterest = &reading
	}
	return update
}

// OpenInterestFromTicker derives an OpenInterestReading from a single
// already-decoded Bybit TickerEvent, without any additional network
// activity -- the reading a caller wanting Bybit's own OI observations
// should use instead of a (redundant, unimplemented) OpenInterestSource;
// see Adapter's own doc comment on why. Returns ok=false when the event
// carries neither an amount nor a value reading (nothing observed yet in
// the current connection episode -- see TickerEvent's own doc comment on
// why OI state resets on reconnect).
func OpenInterestFromTicker(event TickerEvent) (momentumsource.OpenInterestReading, bool) {
	if event.OpenInterest == nil && event.OpenInterestValue == nil {
		return momentumsource.OpenInterestReading{}, false
	}
	reading := momentumsource.OpenInterestReading{
		// Regression for the code-review finding: a ticker delta can carry
		// only ONE of amount/value (tickerState.merge refreshes each field
		// independently, only when the message actually contains it) --
		// provenance must not be stamped "native" for a field that is
		// itself nil, or a consumer trusting "provenance == native" as
		// proof a value exists (per this package's own doc comment on
		// ValueProvenance) would be misled.
		AmountProvenance: provenanceIfPresent(event.OpenInterest),
		Amount:           event.OpenInterest,
		AmountEventAt:    millisPtrToTimePtr(event.OpenInterestEventAtMs),
		AmountObservedAt: millisPtrToTimePtr(event.OpenInterestObservedAtMs),
		ValueProvenance:  provenanceIfPresent(event.OpenInterestValue),
		Value:            event.OpenInterestValue,
		ValueEventAt:     millisPtrToTimePtr(event.OpenInterestValueEventAtMs),
		ValueObservedAt:  millisPtrToTimePtr(event.OpenInterestValueObservedAtMs),
	}
	return reading, true
}

func provenanceIfPresent(value *string) momentumsource.ValueProvenance {
	if value == nil {
		return ""
	}
	return momentumsource.ProvenanceNative
}

func (a *Adapter) FetchUniverse(ctx context.Context) (momentumsource.UniverseSnapshot, error) {
	catalog, err := a.source.FetchSymbolCatalog(ctx)
	if err != nil {
		return momentumsource.UniverseSnapshot{}, err
	}
	return translateUniverse(catalog), nil
}

func translateUniverse(catalog SymbolCatalog) momentumsource.UniverseSnapshot {
	counts := catalog.Counts
	return momentumsource.UniverseSnapshot{
		Exchange:          exchangeName,
		MarketType:        MarketType,
		IncludedSymbols:   catalog.CryptoPerpetualSymbols,
		TotalCatalogItems: counts.CatalogItemsTotal,
		ExclusionCounts: map[string]int{
			"dated_future":        counts.DatedFuturesExcluded,
			"stock_perpetual":     counts.StockPerpetualsExcluded,
			"commodity_perpetual": counts.CommodityPerpetualsExcluded,
			"unknown_contract":    counts.UnknownContractExcluded,
			"unknown_symbol_type": counts.UnknownSymbolTypeExcluded,
			"invalid_instrument":  counts.InvalidInstrumentExcluded,
			"non_usdt":            counts.NonUSDTExcluded,
			"non_trading":         counts.NonTradingExcluded,
		},
	}
}

func millisPtrToTimePtr(ms *int64) *time.Time {
	if ms == nil {
		return nil
	}
	t := time.UnixMilli(*ms)
	return &t
}

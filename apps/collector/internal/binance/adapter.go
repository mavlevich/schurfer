package binance

import (
	"context"
	"sync"

	"github.com/mavlevich/schurfer/collector/internal/momentumsource"
	"github.com/mavlevich/schurfer/collector/internal/wsstream"
)

// MarketType matches momentumvenue's own Binance matrix entry
// ("linear_usdt_perpetual").
const MarketType = "linear_usdt_perpetual"

const exchangeName = "binance"

// Adapter exposes an already-constructed *Source through the canonical
// momentumsource interfaces, mirroring bybit.Adapter's own shape and
// "translate, do not reimplement" discipline.
//
// Adapter deliberately does NOT implement momentumsource.TickerSource for
// v1. Binance's own price-carrying streams are semantically different from
// what TickerUpdate's fields promise: markPrice is a computed/smoothed
// value, not a last-trade price, and Bybit's TickerUpdate.LastPrice is
// documented (via bybit.Adapter) as the venue's own last trade price.
// Populating LastPrice from markPrice would silently conflate two
// different kinds of price, the same class of mistake the capability
// preflight's own OI-value finding warns against. bookTicker (best bid/
// ask) is a closer match for Bid/Ask specifically but still leaves no
// source for LastPrice/24h fields without a THIRD stream (!ticker@arr).
// Wiring a correct, non-misleading TickerSource for Binance needs its own
// deliberate design pass, not a rushed reuse of Bybit's field shape -- see
// docs/research/binance-momentum-source-v1.md's own "What this PR does
// not do" section.
type Adapter struct {
	source *Source
}

func NewAdapter(source *Source) *Adapter {
	return &Adapter{source: source}
}

var (
	_ momentumsource.TradeSource        = (*Adapter)(nil)
	_ momentumsource.OpenInterestSource = (*Adapter)(nil)
	_ momentumsource.UniverseSource     = (*Adapter)(nil)
)

func (a *Adapter) StreamTrades(
	ctx context.Context,
	symbols []string,
	consume momentumsource.TradeConsumer,
) error {
	// Binance's own PublicTrade carries no per-trade session id either
	// (same structural gap as Bybit's -- see trades.go's own
	// TradeLifecycleEvent doc comment); the same per-symbol session
	// tracking bybit.Adapter.StreamTrades uses is required here too, for
	// the identical reason (multiple shards run concurrently with their
	// own session ids, and this callback pair is shared across all of
	// them).
	var mu sync.Mutex
	sessionBySymbol := make(map[string]string)

	onLifecycle := func(event TradeLifecycleEvent) {
		if !event.DisconnectedAt.IsZero() {
			return
		}
		mu.Lock()
		defer mu.Unlock()
		for _, symbol := range event.Symbols {
			sessionBySymbol[wsstream.NormalizeSymbol(symbol)] = event.ShardSessionID
		}
	}

	onTrade := func(ctx context.Context, trade PublicTrade) error {
		mu.Lock()
		session := sessionBySymbol[wsstream.NormalizeSymbol(trade.Symbol)]
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
		TradeID: trade.AggTradeID,
		Side:    trade.Side,
		Price:   trade.Price,
		Size:    trade.Size,
	}
}

// StreamOpenInterest implements momentumsource.OpenInterestSource for
// real, unlike bybit.Adapter: Binance's OI genuinely is a separate REST
// poll (GET /fapi/v1/openInterest, weight 1), not something embedded in a
// push stream this adapter already consumes elsewhere -- see the
// capability preflight's own finding. Uses DefaultOpenInterestSchedulerConfig
// unless a caller needs a different worker count/rate limit; PollOpenInterest
// itself is exported on Source for that case. A consume error is logged and
// does not stop polling -- see PollOpenInterest's own doc comment.
func (a *Adapter) StreamOpenInterest(
	ctx context.Context,
	symbols []string,
	consume momentumsource.OpenInterestConsumer,
) error {
	return a.source.PollOpenInterest(ctx, symbols, DefaultOpenInterestSchedulerConfig(), func(ctx context.Context, reading OpenInterestReading) error {
		envelope := momentumsource.Envelope{
			Exchange:       exchangeName,
			MarketType:     MarketType,
			NativeMarketID: reading.Symbol,
			EventAt:        reading.EventAt,
			ReceivedAt:     reading.ObservedAt,
		}
		// reading is this closure's own by-value parameter -- a fresh copy
		// on every call, so taking its fields' addresses directly is safe
		// (no aliasing across calls); no intermediate locals needed just to
		// have something addressable.
		return consume(ctx, envelope, momentumsource.OpenInterestReading{
			AmountProvenance: momentumsource.ProvenanceNative,
			Amount:           &reading.Amount,
			AmountEventAt:    &reading.EventAt,
			AmountObservedAt: &reading.ObservedAt,
			// ValueProvenance/Value stay the zero value: this endpoint has
			// no value field (see OpenInterestReading's own doc comment in
			// openinterest.go) -- never stamp provenance on an absent value,
			// same rule bybit.Adapter's own provenanceIfPresent enforces.
		})
	})
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
			"non_perpetual_contract":  counts.NonPerpetualContractExcluded,
			"non_trading":             counts.NonTradingExcluded,
			"non_usdt":                counts.NonUSDTExcluded,
			"underlying_index":        counts.UnderlyingIndexExcluded,
			"unknown_underlying_type": counts.UnknownUnderlyingTypeExcluded,
			"invalid_instrument":      counts.InvalidInstrumentExcluded,
		},
	}
}

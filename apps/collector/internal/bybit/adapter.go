package bybit

import (
	"context"

	"github.com/mavlevich/schurfer/collector/internal/momentumsource"
)

// MarketType is Bybit's own entry in the momentumvenue capability matrix
// ("linear_usdt_perpetual"), reused here rather than restated so this
// cannot silently drift from the matrix's own frozen value.
const MarketType = "linear_usdt_perpetual"

const exchangeName = "bybit"

// Adapter exposes an already-constructed *Source's instrument catalog as a
// canonical momentumsource.UniverseSnapshot (burstprobe freezes its run
// universe from it). It only translates Source's own output and changes no
// behavior in bybit.go. Live trades and tickers go through streamrt's Bybit
// codec, not through this type.
type Adapter struct {
	source *Source
}

func NewAdapter(source *Source) *Adapter {
	return &Adapter{source: source}
}

var _ momentumsource.UniverseSource = (*Adapter)(nil)

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

package bybit

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestTranslateUniverseMapsExclusionCountsAndValidates(t *testing.T) {
	catalog := SymbolCatalog{
		CryptoPerpetualSymbols: []string{"BTCUSDT", "ETHUSDT"},
		Counts: SymbolCatalogCounts{
			CatalogItemsTotal:           6,
			CryptoPerpetualsIncluded:    2,
			StandardCryptoIncluded:      2,
			DatedFuturesExcluded:        1,
			StockPerpetualsExcluded:     1,
			CommodityPerpetualsExcluded: 1,
			UnknownContractExcluded:     1,
		},
	}

	got := translateUniverse(catalog)

	if err := got.Validate(); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
	if got.Exchange != "bybit" || got.MarketType != MarketType {
		t.Fatalf("translateUniverse() venue identity = %q/%q", got.Exchange, got.MarketType)
	}
	if len(got.IncludedSymbols) != 2 {
		t.Fatalf("IncludedSymbols = %v", got.IncludedSymbols)
	}
	if got.ExclusionCounts["dated_future"] != 1 || got.ExclusionCounts["stock_perpetual"] != 1 ||
		got.ExclusionCounts["commodity_perpetual"] != 1 || got.ExclusionCounts["unknown_contract"] != 1 {
		t.Fatalf("ExclusionCounts = %+v", got.ExclusionCounts)
	}
}

func TestAdapterFetchUniverseUsesTheSameStrictCryptoPerpetualCatalog(t *testing.T) {
	t.Parallel()
	items := []map[string]string{
		instrument("BTCUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", ""),
		instrument("AMCUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", "stock"),
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeInstrumentResponse(t, w, items, "")
	}))
	t.Cleanup(server.Close)

	adapter := NewAdapter(&Source{restURL: server.URL, httpClient: server.Client()})
	snapshot, err := adapter.FetchUniverse(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if err := snapshot.Validate(); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
	if len(snapshot.IncludedSymbols) != 1 || snapshot.IncludedSymbols[0] != "BTCUSDT" {
		t.Fatalf("IncludedSymbols = %v, want [BTCUSDT]", snapshot.IncludedSymbols)
	}
	if snapshot.ExclusionCounts["stock_perpetual"] != 1 {
		t.Fatalf("ExclusionCounts = %+v, want stock_perpetual=1", snapshot.ExclusionCounts)
	}
}

package bybit

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
)

func TestFetchSymbolCatalogIncludesOnlyUSDTSettledCryptoPerpetuals(t *testing.T) {
	t.Parallel()

	items := []map[string]string{
		instrument("BTCUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", ""),
		instrument("TUTUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", "innovation"),
		instrument("AMCUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", "stock"),
		instrument("XAUUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", "commodity"),
		instrument("BTCUSDT-26MAR27", "LinearFutures", "Trading", "USDT", "USDT", ""),
		instrument("FUTUREUSDT", "FutureContractType", "Trading", "USDT", "USDT", ""),
		instrument("NEWCLASSUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", "new-class"),
		instrument("", "LinearPerpetual", "Trading", "USDT", "USDT", ""),
		instrument("BTCUSDC", "LinearPerpetual", "Trading", "USDC", "USDC", ""),
		instrument("OLDUSDT", "LinearPerpetual", "Settling", "USDT", "USDT", ""),
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("category") != "linear" || r.URL.Query().Get("status") != "Trading" {
			t.Errorf("unexpected query: %s", r.URL.RawQuery)
			http.Error(w, "unexpected query", http.StatusBadRequest)
			return
		}
		writeInstrumentResponse(t, w, items, "")
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	catalog, err := source.FetchSymbolCatalog(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"BTCUSDT", "TUTUSDT"}
	if len(catalog.CryptoPerpetualSymbols) != len(want) {
		t.Fatalf("symbols = %v, want %v", catalog.CryptoPerpetualSymbols, want)
	}
	for i := range want {
		if catalog.CryptoPerpetualSymbols[i] != want[i] {
			t.Fatalf("symbols = %v, want %v", catalog.CryptoPerpetualSymbols, want)
		}
	}
	if len(catalog.AllUSDTLinearSymbols) != 7 {
		t.Fatalf("legacy linear symbols = %v, want 7 entries", catalog.AllUSDTLinearSymbols)
	}
	legacySymbols, err := source.FetchSymbols(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(legacySymbols) != 7 {
		t.Fatalf("FetchSymbols changed the legacy collector scope: %v", legacySymbols)
	}
	for _, symbol := range []string{"AMCUSDT", "XAUUSDT", "BTCUSDT-26MAR27", "NEWCLASSUSDT"} {
		if !slices.Contains(legacySymbols, symbol) {
			t.Fatalf("FetchSymbols dropped legacy symbol %s: %v", symbol, legacySymbols)
		}
	}
	if slices.Contains(legacySymbols, "BTCUSDC") || slices.Contains(legacySymbols, "OLDUSDT") {
		t.Fatalf("FetchSymbols admitted an out-of-scope legacy symbol: %v", legacySymbols)
	}

	counts := catalog.Counts
	if counts.CatalogItemsTotal != 10 || counts.CryptoPerpetualsIncluded != 2 {
		t.Fatalf("catalog totals = %+v", counts)
	}
	if counts.StandardCryptoIncluded != 1 || counts.InnovationCryptoIncluded != 1 {
		t.Fatalf("crypto classes = %+v", counts)
	}
	if counts.DatedFuturesExcluded != 1 || counts.StockPerpetualsExcluded != 1 ||
		counts.CommodityPerpetualsExcluded != 1 {
		t.Fatalf("known exclusions = %+v", counts)
	}
	if counts.UnknownContractExcluded != 1 || counts.UnknownSymbolTypeExcluded != 1 ||
		counts.InvalidInstrumentExcluded != 1 || counts.NonUSDTExcluded != 1 ||
		counts.NonTradingExcluded != 1 {
		t.Fatalf("fail-closed exclusions = %+v", counts)
	}
}

func TestFetchSymbolCatalogPaginatesWithoutLosingScopeCounts(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Query().Get("cursor") {
		case "":
			writeInstrumentResponse(t, w, []map[string]string{
				instrument("BTCUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", ""),
			}, "page-2")
		case "page-2":
			writeInstrumentResponse(t, w, []map[string]string{
				instrument("AMCUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", "stock"),
			}, "")
		default:
			t.Errorf("unexpected cursor %q", r.URL.Query().Get("cursor"))
			http.Error(w, "unexpected cursor", http.StatusBadRequest)
		}
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	catalog, err := source.fetchSymbolCatalog(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(catalog.CryptoPerpetualSymbols) != 1 || catalog.CryptoPerpetualSymbols[0] != "BTCUSDT" {
		t.Fatalf("symbols = %v", catalog.CryptoPerpetualSymbols)
	}
	if catalog.Counts.CatalogItemsTotal != 2 || catalog.Counts.StockPerpetualsExcluded != 1 {
		t.Fatalf("counts = %+v", catalog.Counts)
	}
}

func TestFetchSymbolCatalogRejectsRepeatedPaginationCursor(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeInstrumentResponse(t, w, []map[string]string{
			instrument("BTCUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", ""),
		}, "repeated")
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	_, err := source.fetchSymbolCatalog(context.Background())
	if err == nil || !strings.Contains(err.Error(), "repeated cursor") {
		t.Fatalf("fetchSymbolCatalog() error = %v, want repeated-cursor failure", err)
	}
}

func TestValidateCatalogRejectsEmptyAndDuplicateEligibleUniverse(t *testing.T) {
	t.Parallel()

	if err := validateCatalog(SymbolCatalog{}, true); err == nil {
		t.Fatal("empty eligible universe must fail closed")
	}
	duplicate := SymbolCatalog{
		CryptoPerpetualSymbols: []string{"BTCUSDT", "BTCUSDT"},
		AllUSDTLinearSymbols:   []string{"BTCUSDT", "BTCUSDT"},
		Counts: SymbolCatalogCounts{
			CatalogItemsTotal:        2,
			CryptoPerpetualsIncluded: 2,
			StandardCryptoIncluded:   2,
		},
	}
	if err := validateCatalog(duplicate, true); err == nil {
		t.Fatal("duplicate eligible symbol must fail closed")
	}
}

func TestValidateCatalogKeepsLegacyCollectorIndependentFromCryptoScope(t *testing.T) {
	t.Parallel()

	stockOnly := SymbolCatalog{
		AllUSDTLinearSymbols: []string{"AMCUSDT"},
		Counts: SymbolCatalogCounts{
			CatalogItemsTotal:       1,
			StockPerpetualsExcluded: 1,
		},
	}
	if err := validateCatalog(stockOnly, false); err != nil {
		t.Fatalf("legacy collector validation = %v", err)
	}
	if err := validateCatalog(stockOnly, true); err == nil {
		t.Fatal("momentum scope must reject a catalog with no eligible crypto perpetuals")
	}
}

func instrument(
	symbol string,
	contractType string,
	status string,
	quoteCoin string,
	settleCoin string,
	symbolType string,
) map[string]string {
	return map[string]string{
		"symbol":       symbol,
		"contractType": contractType,
		"status":       status,
		"quoteCoin":    quoteCoin,
		"settleCoin":   settleCoin,
		"symbolType":   symbolType,
	}
}

func writeInstrumentResponse(
	t *testing.T,
	w http.ResponseWriter,
	items []map[string]string,
	nextCursor string,
) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(map[string]any{
		"retCode": 0,
		"retMsg":  "OK",
		"result": map[string]any{
			"list":           items,
			"nextPageCursor": nextCursor,
		},
	}); err != nil {
		t.Errorf("encode instrument response: %v", err)
	}
}

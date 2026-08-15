package binance

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func instrument(symbol, contractType, status, quoteAsset, marginAsset, underlyingType string) map[string]any {
	return map[string]any{
		"symbol":         symbol,
		"contractType":   contractType,
		"status":         status,
		"quoteAsset":     quoteAsset,
		"marginAsset":    marginAsset,
		"underlyingType": underlyingType,
	}
}

func writeExchangeInfoResponse(t *testing.T, w http.ResponseWriter, symbols []map[string]any) {
	t.Helper()
	body := map[string]any{"timezone": "UTC", "serverTime": 1, "symbols": symbols}
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(body); err != nil {
		t.Fatal(err)
	}
}

func TestFetchSymbolCatalogIncludesOnlyUSDTCryptoPerpetuals(t *testing.T) {
	t.Parallel()
	items := []map[string]any{
		instrument("BTCUSDT", "PERPETUAL", "TRADING", "USDT", "USDT", "COIN"),
		instrument("ETHUSDT", "PERPETUAL", "TRADING", "USDT", "USDT", "COIN"),
		instrument("XAUUSDT", "TRADIFI_PERPETUAL", "TRADING", "USDT", "USDT", "COIN"),
		instrument("BTCDOMUSDT", "PERPETUAL", "TRADING", "USDT", "USDT", "INDEX"),
		instrument("OMGUSDT", "PERPETUAL", "SETTLING", "USDT", "USDT", "COIN"),
		instrument("GAIBUSDT", "PERPETUAL", "PENDING_TRADING", "USDT", "USDT", "COIN"),
		instrument("BTCUSDC", "PERPETUAL", "TRADING", "USDC", "USDC", "COIN"),
		instrument("BTCUSD_PERP", "PERPETUAL", "TRADING", "USD", "BTC", "COIN"),
		instrument("WEIRDUSDT", "PERPETUAL", "TRADING", "USDT", "USDT", "SOMETHING_NEW"),
		instrument("", "PERPETUAL", "TRADING", "USDT", "USDT", "COIN"),
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/fapi/v1/exchangeInfo") {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		writeExchangeInfoResponse(t, w, items)
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	catalog, err := source.FetchSymbolCatalog(context.Background())
	if err != nil {
		t.Fatal(err)
	}

	want := []string{"BTCUSDT", "ETHUSDT"}
	if len(catalog.CryptoPerpetualSymbols) != len(want) {
		t.Fatalf("symbols = %v, want %v", catalog.CryptoPerpetualSymbols, want)
	}
	for i := range want {
		if catalog.CryptoPerpetualSymbols[i] != want[i] {
			t.Fatalf("symbols = %v, want %v", catalog.CryptoPerpetualSymbols, want)
		}
	}

	counts := catalog.Counts
	if counts.CatalogItemsTotal != 10 || counts.CryptoPerpetualsIncluded != 2 {
		t.Fatalf("catalog totals = %+v", counts)
	}
	if counts.NonPerpetualContractExcluded != 1 {
		t.Fatalf("TRADIFI_PERPETUAL not excluded via non_perpetual_contract: %+v", counts)
	}
	if counts.UnderlyingIndexExcluded != 1 {
		t.Fatalf("INDEX-type not excluded via underlying_index: %+v", counts)
	}
	if counts.NonTradingExcluded != 2 {
		t.Fatalf("SETTLING/PENDING_TRADING not both excluded via non_trading: %+v", counts)
	}
	if counts.NonUSDTExcluded != 2 {
		t.Fatalf("non-USDT quote/margin not excluded: %+v", counts)
	}
	if counts.UnknownUnderlyingTypeExcluded != 1 {
		t.Fatalf("unknown underlyingType not excluded: %+v", counts)
	}
	if counts.InvalidInstrumentExcluded != 1 {
		t.Fatalf("empty symbol not excluded: %+v", counts)
	}
}

func TestValidateCatalogRejectsEmptyEligibleUniverse(t *testing.T) {
	// Exercises validateCatalog directly rather than through
	// FetchSymbolCatalog's own retry wrapper: this failure is permanent
	// (an empty eligible universe stays empty on every retry), so going
	// through all 5 retries here would only make the test slow without
	// covering anything FetchSymbolCatalogRetriesOnTransientFailure does
	// not already cover for the retry loop itself.
	var catalog SymbolCatalog
	classifyCatalogItem(&catalog, "XAUUSDT", "TRADIFI_PERPETUAL", "TRADING", "USDT", "USDT", "COIN")

	err := validateCatalog(catalog)
	if err == nil || !strings.Contains(err.Error(), "no eligible crypto perpetuals") {
		t.Fatalf("err = %v, want no-eligible-crypto-perpetuals failure", err)
	}
}

func TestClassifyCatalogItemAccountingIsExhaustive(t *testing.T) {
	// Regression for the same bug class the Bybit universe remediation
	// fixed once already (docs/research/momentum-venue-capability-matrix-
	// v1.md): every branch of classifyCatalogItem must land in exactly one
	// counted bucket, and validateCatalog's own accounting check must
	// actually catch it if a future edit adds a new branch without also
	// counting it.
	var catalog SymbolCatalog
	rows := []struct {
		symbol, contractType, status, quoteAsset, marginAsset, underlyingType string
	}{
		{"BTCUSDT", "PERPETUAL", "TRADING", "USDT", "USDT", "COIN"},
		{"XAUUSDT", "TRADIFI_PERPETUAL", "TRADING", "USDT", "USDT", "COIN"},
		{"OMGUSDT", "PERPETUAL", "SETTLING", "USDT", "USDT", "COIN"},
		{"BTCUSDC", "PERPETUAL", "TRADING", "USDC", "USDC", "COIN"},
		{"BTCDOMUSDT", "PERPETUAL", "TRADING", "USDT", "USDT", "INDEX"},
		{"WEIRDUSDT", "PERPETUAL", "TRADING", "USDT", "USDT", "SOMETHING_NEW"},
		{"", "PERPETUAL", "TRADING", "USDT", "USDT", "COIN"},
	}
	for _, row := range rows {
		classifyCatalogItem(&catalog, row.symbol, row.contractType, row.status, row.quoteAsset, row.marginAsset, row.underlyingType)
	}
	if err := validateCatalog(catalog); err != nil {
		t.Fatalf("validateCatalog() error = %v", err)
	}
}

func TestFetchSymbolCatalogRetriesOnTransientFailure(t *testing.T) {
	t.Parallel()
	var attempts int
	items := []map[string]any{instrument("BTCUSDT", "PERPETUAL", "TRADING", "USDT", "USDT", "COIN")}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts++
		if attempts < 2 {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		writeExchangeInfoResponse(t, w, items)
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	catalog, err := source.FetchSymbolCatalog(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(catalog.CryptoPerpetualSymbols) != 1 {
		t.Fatalf("symbols = %v", catalog.CryptoPerpetualSymbols)
	}
	if attempts < 2 {
		t.Fatalf("attempts = %d, want at least 2 (one failure then a retry)", attempts)
	}
}

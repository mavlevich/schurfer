// Package binance implements the Binance USD-M futures source: universe
// (exchangeInfo), public trades (aggTrade), and open interest (a REST
// poll, not a push -- see openinterest.go). Every classification and
// endpoint choice here traces to docs/research/binance-momentum-
// capability-preflight-v1.md's own live-verified findings, not
// documentation alone. Adapter (adapter.go) wraps this package's Source
// to satisfy the momentumsource contract; nothing here is wired into any
// running binary or Compose profile yet (see docs/research/binance-
// momentum-source-v1.md's own "What this PR is and is not" section).
package binance

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/momentumsource"
)

const defaultRESTURL = "https://fapi.binance.com"

const (
	// Per the capability preflight: Binance tags tokenized-stock/commodity
	// perpetuals with their own contractType, unlike Bybit's mixed
	// LinearPerpetual scope -- a single contractType check is the whole
	// exclusion, no symbolType heuristic needed.
	contractTypePerpetual = "PERPETUAL"
	statusTrading         = "TRADING"
	underlyingTypeCoin    = "COIN"
)

// SymbolCatalogCounts mirrors bybit.SymbolCatalogCounts's own shape and
// intent: every catalog item is accounted for exactly once, either
// included or excluded under a named, counted reason -- see Validate.
type SymbolCatalogCounts struct {
	CatalogItemsTotal             int
	CryptoPerpetualsIncluded      int
	NonPerpetualContractExcluded  int
	NonTradingExcluded            int
	NonUSDTExcluded               int
	UnderlyingIndexExcluded       int
	UnknownUnderlyingTypeExcluded int
	InvalidInstrumentExcluded     int
}

// SymbolCatalog is one point-in-time classification of Binance's USD-M
// futures catalog, restricted to the strict momentum-capture universe:
// PERPETUAL contractType, TRADING status, USDT quote AND margin asset,
// COIN underlyingType (excludes the 2 INDEX-type instruments the
// preflight found, e.g. BTCDOMUSDT/ALLUSDT). Instruments is additive
// (feat/momentum-universe-identity-foundation-v1): one momentumsource.
// Instrument per CryptoPerpetualSymbols entry, same order, same length --
// see validateCatalog. CryptoPerpetualSymbols itself is untouched.
type SymbolCatalog struct {
	CryptoPerpetualSymbols []string
	Instruments            []momentumsource.Instrument
	Counts                 SymbolCatalogCounts
}

// Source streams Binance USD-M futures market data.
type Source struct {
	restURL    string
	wsBaseURL  string
	httpClient *http.Client
}

func NewSource() *Source {
	return &Source{
		restURL:    defaultRESTURL,
		wsBaseURL:  wsBaseURL,
		httpClient: &http.Client{Timeout: 10 * time.Second},
	}
}

// httpClientOrDefault and restURLOrDefault centralize the fallback this
// package's REST calls (fetchSymbolCatalog, fetchOpenInterest) both need
// for a Source constructed directly with a zero value (as every test in
// this package does) rather than through NewSource -- a code-review
// finding after this same two-line block had been copy-pasted verbatim
// into both call sites.
func (s *Source) httpClientOrDefault() *http.Client {
	if s.httpClient != nil {
		return s.httpClient
	}
	return &http.Client{Timeout: 10 * time.Second}
}

func (s *Source) restURLOrDefault() string {
	if s.restURL != "" {
		return s.restURL
	}
	return defaultRESTURL
}

// FetchSymbolCatalog fetches and classifies the exchangeInfo catalog.
// Retries with exponential backoff on failure, mirroring bybit.Source's
// own retry shape.
func (s *Source) FetchSymbolCatalog(ctx context.Context) (SymbolCatalog, error) {
	var lastErr error
	for attempt := range 5 {
		if attempt > 0 {
			delay := time.Duration(1<<(attempt-1)) * time.Second
			slog.Warn("binance.rest.retry", "attempt", attempt, "delay", delay, "err", lastErr)
			select {
			case <-ctx.Done():
				return SymbolCatalog{}, ctx.Err()
			case <-time.After(delay):
			}
		}
		catalog, err := s.fetchSymbolCatalog(ctx)
		if err == nil {
			err = validateCatalog(catalog)
		}
		if err == nil {
			logCatalog(catalog)
			return catalog, nil
		}
		lastErr = err
	}
	return SymbolCatalog{}, fmt.Errorf("fetch symbol catalog after 5 attempts: %w", lastErr)
}

func (s *Source) fetchSymbolCatalog(ctx context.Context) (SymbolCatalog, error) {
	client := s.httpClientOrDefault()
	restURL := s.restURLOrDefault()
	endpoint := restURL + "/fapi/v1/exchangeInfo"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return SymbolCatalog{}, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return SymbolCatalog{}, err
	}
	if resp.StatusCode != http.StatusOK {
		_ = resp.Body.Close()
		return SymbolCatalog{}, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	b, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil {
		return SymbolCatalog{}, fmt.Errorf("read body: %w", err)
	}

	var body struct {
		Symbols []struct {
			Symbol            string   `json:"symbol"`
			ContractType      string   `json:"contractType"`
			Status            string   `json:"status"`
			BaseAsset         string   `json:"baseAsset"`
			QuoteAsset        string   `json:"quoteAsset"`
			MarginAsset       string   `json:"marginAsset"`
			UnderlyingType    string   `json:"underlyingType"`
			UnderlyingSubType []string `json:"underlyingSubType"`
			// OnboardDate is a pointer specifically so a JSON response that
			// omits the field entirely is distinguishable from one that
			// carries an explicit 0/negative sentinel -- both end up
			// IdentityStatusMissingOnboardedAt (see parseOnboardDateMs),
			// but only because that is checked explicitly, not because a
			// bare int64 zero value made the two indistinguishable.
			OnboardDate *int64 `json:"onboardDate"`
		} `json:"symbols"`
	}
	if err := json.Unmarshal(b, &body); err != nil {
		return SymbolCatalog{}, fmt.Errorf("decode: %w", err)
	}

	// observedAt is fixed once for this entire response: every Instrument
	// this catalog produces describes the same point-in-time snapshot.
	observedAt := time.Now()
	catalog := SymbolCatalog{}
	for _, item := range body.Symbols {
		classifyCatalogItem(
			&catalog, item.Symbol, item.ContractType, item.Status,
			item.BaseAsset, item.QuoteAsset, item.MarginAsset, item.UnderlyingType,
			item.OnboardDate, observedAt,
		)
	}
	return catalog, nil
}

func classifyCatalogItem(
	catalog *SymbolCatalog,
	symbol string,
	contractType string,
	status string,
	baseAsset string,
	quoteAsset string,
	marginAsset string,
	underlyingType string,
	onboardDateMs *int64,
	observedAt time.Time,
) {
	catalog.Counts.CatalogItemsTotal++
	if symbol == "" {
		catalog.Counts.InvalidInstrumentExcluded++
		return
	}
	if contractType != contractTypePerpetual {
		catalog.Counts.NonPerpetualContractExcluded++
		return
	}
	if status != statusTrading {
		catalog.Counts.NonTradingExcluded++
		return
	}
	if quoteAsset != "USDT" || marginAsset != "USDT" {
		catalog.Counts.NonUSDTExcluded++
		return
	}
	switch underlyingType {
	case underlyingTypeCoin:
		catalog.Counts.CryptoPerpetualsIncluded++
		catalog.CryptoPerpetualSymbols = append(catalog.CryptoPerpetualSymbols, symbol)
		// Settle: Binance's own USD-M futures settle in the margin asset,
		// same value marginAsset already carries -- no separate settle
		// field exists on this endpoint to decode.
		catalog.Instruments = append(catalog.Instruments, momentumsource.NewInstrument(
			exchangeName, symbol, baseAsset, quoteAsset, marginAsset,
			contractType, MarketType, parseOnboardDateMs(onboardDateMs), observedAt,
		))
	case "INDEX":
		catalog.Counts.UnderlyingIndexExcluded++
	default:
		catalog.Counts.UnknownUnderlyingTypeExcluded++
	}
}

// parseOnboardDateMs parses Binance's own onboardDate field: Unix
// milliseconds, carried as a JSON number (unlike Bybit's launchTime, a
// JSON string -- see bybit.parseLaunchTimeMs). A nil pointer means the
// field was absent from the response; anything else is real data the
// venue sent, decoded already. The absent/zero/negative/valid
// classification rule itself (0 is Binance's own "not recorded" sentinel;
// negative is present-but-semantically-impossible, not "missing") is
// shared with Bybit's own equivalent field -- see
// momentumsource.ClassifyOnboardedAtMs's own doc comment (a code-review
// finding: this and bybit.parseLaunchTimeMs originally reimplemented the
// same rule twice, and a negative-value fix had to be applied to both
// separately before this was factored out).
func parseOnboardDateMs(raw *int64) *time.Time {
	if raw == nil {
		return momentumsource.ClassifyOnboardedAtMs(0, false)
	}
	return momentumsource.ClassifyOnboardedAtMs(*raw, true)
}

func validateCatalog(catalog SymbolCatalog) error {
	counts := catalog.Counts
	if len(catalog.CryptoPerpetualSymbols) == 0 {
		return errors.New("instrument catalog contains no eligible crypto perpetuals")
	}
	seen := make(map[string]struct{}, len(catalog.CryptoPerpetualSymbols))
	for _, symbol := range catalog.CryptoPerpetualSymbols {
		if _, exists := seen[symbol]; exists {
			return fmt.Errorf("instrument catalog contains duplicate symbol %q", symbol)
		}
		seen[symbol] = struct{}{}
	}
	classified := counts.CryptoPerpetualsIncluded +
		counts.NonPerpetualContractExcluded +
		counts.NonTradingExcluded +
		counts.NonUSDTExcluded +
		counts.UnderlyingIndexExcluded +
		counts.UnknownUnderlyingTypeExcluded +
		counts.InvalidInstrumentExcluded
	if classified != counts.CatalogItemsTotal {
		return fmt.Errorf(
			"instrument catalog classification mismatch: total=%d classified=%d",
			counts.CatalogItemsTotal, classified,
		)
	}
	if len(catalog.CryptoPerpetualSymbols) != counts.CryptoPerpetualsIncluded {
		return fmt.Errorf(
			"instrument catalog inclusion mismatch: symbols=%d included=%d",
			len(catalog.CryptoPerpetualSymbols), counts.CryptoPerpetualsIncluded,
		)
	}
	if len(catalog.Instruments) != len(catalog.CryptoPerpetualSymbols) {
		return fmt.Errorf(
			"instrument catalog identity mismatch: instruments=%d symbols=%d",
			len(catalog.Instruments), len(catalog.CryptoPerpetualSymbols),
		)
	}
	return nil
}

func logCatalog(catalog SymbolCatalog) {
	slog.Info(
		"binance.symbols_loaded",
		"crypto_perpetual_count", len(catalog.CryptoPerpetualSymbols),
		"catalog_items_total", catalog.Counts.CatalogItemsTotal,
		"non_perpetual_contract_excluded", catalog.Counts.NonPerpetualContractExcluded,
		"non_trading_excluded", catalog.Counts.NonTradingExcluded,
		"non_usdt_excluded", catalog.Counts.NonUSDTExcluded,
		"underlying_index_excluded", catalog.Counts.UnderlyingIndexExcluded,
		"unknown_underlying_type_excluded", catalog.Counts.UnknownUnderlyingTypeExcluded,
		"invalid_instrument_excluded", catalog.Counts.InvalidInstrumentExcluded,
	)
}

func buildQueryURL(base string, path string, params url.Values) string {
	return base + path + "?" + params.Encode()
}

package bybit

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/momentumsource"
)

const defaultRESTURL = "https://api.bybit.com/v5/market/instruments-info"

const (
	contractTypeLinearPerpetual = "LinearPerpetual"
	contractTypeLinearFutures   = "LinearFutures"
	symbolTypeInnovation        = "innovation"
	symbolTypeStock             = "stock"
	symbolTypeCommodity         = "commodity"
	maxInstrumentCatalogPages   = 100
)

// SymbolCatalogCounts makes the instrument scope visible instead of
// silently dropping non-crypto products returned by Bybit's shared linear
// endpoint. Standard and innovation symbols are both crypto perpetuals;
// stocks, commodities, dated futures, and unknown future classifications
// stay outside this capture contract.
type SymbolCatalogCounts struct {
	CatalogItemsTotal           int
	CryptoPerpetualsIncluded    int
	StandardCryptoIncluded      int
	InnovationCryptoIncluded    int
	DatedFuturesExcluded        int
	StockPerpetualsExcluded     int
	CommodityPerpetualsExcluded int
	UnknownContractExcluded     int
	UnknownSymbolTypeExcluded   int
	InvalidInstrumentExcluded   int
	NonUSDTExcluded             int
	NonTradingExcluded          int
}

// SymbolCatalog is one point-in-time classification of Bybit's linear
// instrument catalog. CryptoPerpetualSymbols is the strict momentum-capture
// universe. AllUSDTLinearSymbols preserves the broader legacy ticker
// collector scope until that separate production contract is reviewed.
// Instruments is additive (feat/momentum-universe-identity-foundation-v1):
// one momentumsource.Instrument per CryptoPerpetualSymbols entry, same
// order, same length -- see validateCatalog. CryptoPerpetualSymbols itself
// is untouched by this addition; nothing that already reads it needs to
// change.
type SymbolCatalog struct {
	CryptoPerpetualSymbols []string
	AllUSDTLinearSymbols   []string
	Instruments            []momentumsource.Instrument
	Counts                 SymbolCatalogCounts
}

// TickerEvent is the normalized event published to NATS.
//
// OpenInterest/OpenInterestValue and everything below Ask are a schema v1
// backward-compatible extension (SchemaVersion is unchanged; all new fields
// are pointers or have zero-value-safe defaults): a consumer built against
// this contract must tolerate a rolling deploy where the collector binary
// publishing these events has not yet been upgraded and simply omits them.
// A missing OpenInterest must be read as "unknown", never as "zero" or "no
// change" beyond what OpenInterestObservedAtMs actually attests to.
type TickerEvent struct {
	SchemaVersion int     `json:"schema_version"`
	Source        string  `json:"source"`
	Symbol        string  `json:"symbol"`
	TS            int64   `json:"ts"`
	LastPrice     *string `json:"last_price"`
	Price24hPct   *string `json:"price_24h_pct"`
	High24h       *string `json:"high_24h"`
	Low24h        *string `json:"low_24h"`
	Volume24h     *string `json:"volume_24h"`
	Turnover24h   *string `json:"turnover_24h"`
	Bid           *string `json:"bid"`
	Ask           *string `json:"ask"`
	// OpenInterest is the contract quantity, OpenInterestValue its USD
	// notional. Each has two timestamps for the last message that actually
	// carried a fresh value for that specific field (Bybit ticker deltas
	// omit unchanged fields, so a delta that only changed price still
	// republishes the last known OI): EventAtMs is Bybit's own exchange-time
	// ts for that message; ObservedAtMs is this collector's own wall-clock
	// receive time for it. Neither is this event's own TS/ReceivedAtMs,
	// which describe the CURRENT message, not necessarily the one that last
	// changed OI. nil means no value has been observed yet in the current
	// connection episode (see StreamSessionID): OI state is deliberately
	// reset on every reconnect, unlike price/bid/ask, since it has no
	// existing consumer whose behavior this must not disturb.
	OpenInterest                  *string `json:"open_interest"`
	OpenInterestEventAtMs         *int64  `json:"open_interest_event_at_ms"`
	OpenInterestObservedAtMs      *int64  `json:"open_interest_observed_at_ms"`
	OpenInterestValue             *string `json:"open_interest_value"`
	OpenInterestValueEventAtMs    *int64  `json:"open_interest_value_event_at_ms"`
	OpenInterestValueObservedAtMs *int64  `json:"open_interest_value_observed_at_ms"`
	// ReceivedAtMs is the collector's own wall-clock receive time for this
	// message, independent of Bybit's TS, for event/receive lag diagnostics.
	ReceivedAtMs int64 `json:"received_at_ms"`
	// MessageType is Bybit's own "snapshot"/"delta" tag for this message.
	MessageType string `json:"message_type"`
	// CrossSequence is Bybit's own "cs" field, stored verbatim. Bybit's
	// public documentation gives no guarantee about its semantics beyond it
	// being an integer: it is not documented as contiguous, not documented
	// as stable for the life of a connection, and a change in it must NOT
	// by itself be read as a gap or as the exchange resyncing this topic.
	// Keep it only as raw ordering/diagnostic context, to be correlated
	// against MessageType, StreamSessionID, and any independently observed
	// time discontinuity; do not build gap-detection logic on cs alone.
	CrossSequence *int64 `json:"cross_sequence"`
	// ReconnectEpoch counts this connection's own reconnect attempts within
	// one process's lifetime, starting at 0 for the first successful
	// connection. It is local to one shard of the ticker subscription (see
	// chunkSlice) and, critically, resets to 0 on every process restart: it
	// cannot by itself distinguish a freshly started process from one that
	// has been running for days. Use it only as a human-readable ordinal
	// alongside StreamSessionID, never as the sole signal that a gap is
	// explained.
	ReconnectEpoch int `json:"reconnect_epoch"`
	// StreamSessionID is a random identifier generated fresh on every dial:
	// every reconnect within a process, and every process restart, gets a
	// new value. A change in StreamSessionID is the authoritative signal
	// that this is a different physical connection, which ReconnectEpoch
	// alone cannot provide across a restart.
	StreamSessionID string `json:"stream_session_id"`
}

// PublishFn publishes a TickerEvent to NATS.
type PublishFn func(ctx context.Context, event TickerEvent) error

type streamConfig struct {
	URL            string
	PingInterval   time.Duration
	ReadTimeout    time.Duration
	ReconnectDelay time.Duration
}

// StreamStats is a monotonic snapshot of Bybit WebSocket recovery activity.
type StreamStats struct {
	TickerReconnectTotal   uint64
	TickerReadTimeoutTotal uint64
	TradeReconnectTotal    uint64
	TradeReadTimeoutTotal  uint64
}

// Source streams Bybit linear market data. Callers choose either the
// broader legacy ticker scope or the strict crypto-perpetual catalog.
type Source struct {
	streamConfig streamConfig
	restURL      string
	httpClient   *http.Client

	tickerReconnectTotal   atomic.Uint64
	tickerReadTimeoutTotal atomic.Uint64
	tradeReconnectTotal    atomic.Uint64
	tradeReadTimeoutTotal  atomic.Uint64
}

func NewSource() *Source {
	return &Source{
		streamConfig: streamConfig{
			URL:            wsURL,
			PingInterval:   pingInterval,
			ReadTimeout:    readTimeout,
			ReconnectDelay: reconnDelay,
		},
		restURL:    defaultRESTURL,
		httpClient: &http.Client{Timeout: 10 * time.Second},
	}
}

// StreamStats returns a race-safe snapshot suitable for health telemetry.
func (s *Source) StreamStats() StreamStats {
	return StreamStats{
		TickerReconnectTotal:   s.tickerReconnectTotal.Load(),
		TickerReadTimeoutTotal: s.tickerReadTimeoutTotal.Load(),
		TradeReconnectTotal:    s.tradeReconnectTotal.Load(),
		TradeReadTimeoutTotal:  s.tradeReadTimeoutTotal.Load(),
	}
}

// FetchSymbols preserves the existing collector contract: every active
// USDT-quoted and USDT-settled instrument returned by Bybit's linear
// endpoint. Momentum capture uses FetchSymbolCatalog directly and applies
// its narrower crypto-perpetual scope without changing the pump scanner.
func (s *Source) FetchSymbols(ctx context.Context) ([]string, error) {
	catalog, err := s.fetchSymbolCatalogWithRetry(ctx, false)
	if err != nil {
		return nil, err
	}
	return catalog.AllUSDTLinearSymbols, nil
}

// FetchSymbolCatalog returns the filtered symbol universe together with
// explicit exclusion counts. Retries with exponential backoff on failure.
func (s *Source) FetchSymbolCatalog(ctx context.Context) (SymbolCatalog, error) {
	return s.fetchSymbolCatalogWithRetry(ctx, true)
}

func (s *Source) fetchSymbolCatalogWithRetry(
	ctx context.Context,
	requireCryptoPerpetuals bool,
) (SymbolCatalog, error) {
	var lastErr error
	for attempt := range 5 {
		if attempt > 0 {
			delay := time.Duration(1<<(attempt-1)) * time.Second
			slog.Warn("bybit.rest.retry", "attempt", attempt, "delay", delay, "err", lastErr)
			select {
			case <-ctx.Done():
				return SymbolCatalog{}, ctx.Err()
			case <-time.After(delay):
			}
		}
		catalog, err := s.fetchSymbolCatalog(ctx)
		if err == nil {
			err = validateCatalog(catalog, requireCryptoPerpetuals)
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
	client := s.httpClient
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	restURL := s.restURL
	if restURL == "" {
		restURL = defaultRESTURL
	}
	catalog := SymbolCatalog{}
	cursor := ""
	seenCursors := map[string]struct{}{"": {}}
	// observedAt is fixed once for this entire fetch (potentially several
	// paginated requests), not re-read per page: every Instrument this
	// catalog produces describes the same point-in-time snapshot.
	observedAt := time.Now()

	for page := 0; page < maxInstrumentCatalogPages; page++ {
		params := url.Values{
			"category": {"linear"},
			"limit":    {"1000"},
			"status":   {"Trading"},
		}
		if cursor != "" {
			params.Set("cursor", cursor)
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, restURL+"?"+params.Encode(), nil)
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
			RetCode int    `json:"retCode"`
			RetMsg  string `json:"retMsg"`
			Result  struct {
				List []struct {
					Symbol       string `json:"symbol"`
					ContractType string `json:"contractType"`
					Status       string `json:"status"`
					BaseCoin     string `json:"baseCoin"`
					QuoteCoin    string `json:"quoteCoin"`
					SettleCoin   string `json:"settleCoin"`
					SymbolType   string `json:"symbolType"`
					LaunchTime   string `json:"launchTime"`
				} `json:"list"`
				NextPageCursor string `json:"nextPageCursor"`
			} `json:"result"`
		}
		if err := json.Unmarshal(b, &body); err != nil {
			return SymbolCatalog{}, fmt.Errorf("decode: %w", err)
		}
		if body.RetCode != 0 {
			return SymbolCatalog{}, fmt.Errorf("bybit API error %d: %s", body.RetCode, body.RetMsg)
		}

		for _, item := range body.Result.List {
			classifyCatalogItem(
				&catalog, item.Symbol, item.ContractType, item.Status,
				item.BaseCoin, item.QuoteCoin, item.SettleCoin, item.SymbolType,
				item.LaunchTime, observedAt,
			)
		}

		nextCursor := body.Result.NextPageCursor
		if nextCursor == "" {
			break
		}
		if _, exists := seenCursors[nextCursor]; exists {
			return SymbolCatalog{}, fmt.Errorf("instrument catalog repeated cursor %q", nextCursor)
		}
		seenCursors[nextCursor] = struct{}{}
		cursor = nextCursor
		if page == maxInstrumentCatalogPages-1 {
			return SymbolCatalog{}, fmt.Errorf(
				"instrument catalog exceeded %d pages",
				maxInstrumentCatalogPages,
			)
		}
	}

	return catalog, nil
}

func logCatalog(catalog SymbolCatalog) {
	slog.Info(
		"bybit.symbols_loaded",
		"crypto_perpetual_count", len(catalog.CryptoPerpetualSymbols),
		"all_usdt_linear_count", len(catalog.AllUSDTLinearSymbols),
		"catalog_items_total", catalog.Counts.CatalogItemsTotal,
		"dated_futures_excluded", catalog.Counts.DatedFuturesExcluded,
		"stock_perpetuals_excluded", catalog.Counts.StockPerpetualsExcluded,
		"commodity_perpetuals_excluded", catalog.Counts.CommodityPerpetualsExcluded,
		"unknown_contract_excluded", catalog.Counts.UnknownContractExcluded,
		"unknown_symbol_type_excluded", catalog.Counts.UnknownSymbolTypeExcluded,
		"invalid_instrument_excluded", catalog.Counts.InvalidInstrumentExcluded,
	)
}

func classifyCatalogItem(
	catalog *SymbolCatalog,
	symbol string,
	contractType string,
	status string,
	baseCoin string,
	quoteCoin string,
	settleCoin string,
	symbolType string,
	launchTime string,
	observedAt time.Time,
) {
	catalog.Counts.CatalogItemsTotal++
	if symbol == "" {
		catalog.Counts.InvalidInstrumentExcluded++
		return
	}
	if status != "Trading" {
		catalog.Counts.NonTradingExcluded++
		return
	}
	if quoteCoin != "USDT" || settleCoin != "USDT" {
		catalog.Counts.NonUSDTExcluded++
		return
	}
	catalog.AllUSDTLinearSymbols = append(catalog.AllUSDTLinearSymbols, symbol)
	if contractType != contractTypeLinearPerpetual {
		if contractType == contractTypeLinearFutures {
			catalog.Counts.DatedFuturesExcluded++
		} else {
			catalog.Counts.UnknownContractExcluded++
		}
		return
	}
	switch symbolType {
	case "":
		catalog.Counts.StandardCryptoIncluded++
	case symbolTypeInnovation:
		catalog.Counts.InnovationCryptoIncluded++
	case symbolTypeStock:
		catalog.Counts.StockPerpetualsExcluded++
		return
	case symbolTypeCommodity:
		catalog.Counts.CommodityPerpetualsExcluded++
		return
	default:
		catalog.Counts.UnknownSymbolTypeExcluded++
		return
	}
	catalog.Counts.CryptoPerpetualsIncluded++
	catalog.CryptoPerpetualSymbols = append(catalog.CryptoPerpetualSymbols, symbol)
	catalog.Instruments = append(catalog.Instruments, momentumsource.NewInstrument(
		exchangeName, symbol, baseCoin, quoteCoin, settleCoin,
		contractType, MarketType, parseLaunchTimeMs(launchTime), observedAt,
	))
}

// parseLaunchTimeMs parses Bybit's own launchTime field: a Unix-milliseconds
// value carried as a JSON string, unlike Binance's onboardDate (a JSON
// number) -- this string can be genuinely unparseable, a failure mode
// momentumsource.ClassifyOnboardedAtMs's own int64-typed signature cannot
// represent, so that structural case is handled here before delegating
// the shared absent/zero/negative/valid classification rule to it.
func parseLaunchTimeMs(raw string) *time.Time {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return momentumsource.ClassifyOnboardedAtMs(0, false)
	}
	ms, err := strconv.ParseInt(trimmed, 10, 64)
	if err != nil {
		// Present but structurally unparseable: treated the same as a
		// negative value (present but semantically unusable), not as
		// "absent" -- see ClassifyOnboardedAtMs's own doc comment.
		invalid := time.Time{}
		return &invalid
	}
	return momentumsource.ClassifyOnboardedAtMs(ms, true)
}

func validateCatalog(catalog SymbolCatalog, requireCryptoPerpetuals bool) error {
	counts := catalog.Counts
	if len(catalog.AllUSDTLinearSymbols) == 0 {
		return errors.New("instrument catalog contains no active USDT linear symbols")
	}
	if requireCryptoPerpetuals && len(catalog.CryptoPerpetualSymbols) == 0 {
		return errors.New("instrument catalog contains no eligible crypto perpetuals")
	}
	seen := make(map[string]struct{}, len(catalog.AllUSDTLinearSymbols))
	for _, symbol := range catalog.AllUSDTLinearSymbols {
		if _, exists := seen[symbol]; exists {
			return fmt.Errorf("instrument catalog contains duplicate symbol %q", symbol)
		}
		seen[symbol] = struct{}{}
	}
	classified := counts.CryptoPerpetualsIncluded +
		counts.DatedFuturesExcluded +
		counts.StockPerpetualsExcluded +
		counts.CommodityPerpetualsExcluded +
		counts.UnknownContractExcluded +
		counts.UnknownSymbolTypeExcluded +
		counts.InvalidInstrumentExcluded +
		counts.NonUSDTExcluded +
		counts.NonTradingExcluded
	if classified != counts.CatalogItemsTotal {
		return fmt.Errorf(
			"instrument catalog classification mismatch: total=%d classified=%d",
			counts.CatalogItemsTotal,
			classified,
		)
	}
	if len(catalog.CryptoPerpetualSymbols) != counts.CryptoPerpetualsIncluded {
		return fmt.Errorf(
			"instrument catalog inclusion mismatch: symbols=%d included=%d",
			len(catalog.CryptoPerpetualSymbols),
			counts.CryptoPerpetualsIncluded,
		)
	}
	if len(catalog.Instruments) != len(catalog.CryptoPerpetualSymbols) {
		return fmt.Errorf(
			"instrument catalog identity mismatch: instruments=%d symbols=%d",
			len(catalog.Instruments),
			len(catalog.CryptoPerpetualSymbols),
		)
	}
	if counts.StandardCryptoIncluded+counts.InnovationCryptoIncluded !=
		counts.CryptoPerpetualsIncluded {
		return fmt.Errorf(
			"crypto catalog classification mismatch: standard=%d innovation=%d included=%d",
			counts.StandardCryptoIncluded,
			counts.InnovationCryptoIncluded,
			counts.CryptoPerpetualsIncluded,
		)
	}
	return nil
}

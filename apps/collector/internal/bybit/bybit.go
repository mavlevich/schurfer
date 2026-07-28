package bybit

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"time"
)

const restURL = "https://api.bybit.com/v5/market/instruments-info"

// TickerEvent is the normalized event published to NATS.
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
}

// PublishFn publishes a TickerEvent to NATS.
type PublishFn func(ctx context.Context, event TickerEvent) error

// Source streams Bybit linear perpetual tickers.
type Source struct{}

func NewSource() *Source { return &Source{} }

// FetchSymbols returns all active USDT-settled linear perp symbols.
// Retries with exponential backoff on failure.
func (s *Source) FetchSymbols(ctx context.Context) ([]string, error) {
	var lastErr error
	for attempt := range 5 {
		if attempt > 0 {
			delay := time.Duration(1<<(attempt-1)) * time.Second
			slog.Warn("bybit.rest.retry", "attempt", attempt, "delay", delay, "err", lastErr)
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(delay):
			}
		}
		symbols, err := s.fetchSymbols(ctx)
		if err == nil {
			return symbols, nil
		}
		lastErr = err
	}
	return nil, fmt.Errorf("fetch symbols after 5 attempts: %w", lastErr)
}

func (s *Source) fetchSymbols(ctx context.Context) ([]string, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	var symbols []string
	cursor := ""

	for {
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
			return nil, err
		}
		resp, err := client.Do(req)
		if err != nil {
			return nil, err
		}
		if resp.StatusCode != http.StatusOK {
			_ = resp.Body.Close()
			return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
		}
		b, err := io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if err != nil {
			return nil, fmt.Errorf("read body: %w", err)
		}

		var body struct {
			RetCode int    `json:"retCode"`
			RetMsg  string `json:"retMsg"`
			Result  struct {
				List []struct {
					Symbol     string `json:"symbol"`
					QuoteCoin  string `json:"quoteCoin"`
					SettleCoin string `json:"settleCoin"`
				} `json:"list"`
				NextPageCursor string `json:"nextPageCursor"`
			} `json:"result"`
		}
		if err := json.Unmarshal(b, &body); err != nil {
			return nil, fmt.Errorf("decode: %w", err)
		}
		if body.RetCode != 0 {
			return nil, fmt.Errorf("bybit API error %d: %s", body.RetCode, body.RetMsg)
		}

		for _, item := range body.Result.List {
			if item.QuoteCoin == "USDT" && item.SettleCoin == "USDT" {
				symbols = append(symbols, item.Symbol)
			}
		}

		cursor = body.Result.NextPageCursor
		if cursor == "" {
			break
		}
	}

	slog.Info("bybit.symbols_loaded", "count", len(symbols))
	return symbols, nil
}

package bybit

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"sync/atomic"
	"time"
)

const restURL = "https://api.bybit.com/v5/market/instruments-info"

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

// Source streams Bybit linear perpetual market data.
type Source struct {
	streamConfig streamConfig

	tickerReconnectTotal   atomic.Uint64
	tickerReadTimeoutTotal atomic.Uint64
	tradeReconnectTotal    atomic.Uint64
	tradeReadTimeoutTotal  atomic.Uint64
}

func NewSource() *Source {
	return &Source{streamConfig: streamConfig{
		URL:            wsURL,
		PingInterval:   pingInterval,
		ReadTimeout:    readTimeout,
		ReconnectDelay: reconnDelay,
	}}
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

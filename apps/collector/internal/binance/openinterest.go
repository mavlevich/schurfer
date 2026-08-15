package binance

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/wsstream"
)

// OpenInterestReading is one polled reading of GET /fapi/v1/openInterest --
// amount only. Per the capability preflight, this endpoint has no value
// field; a native but coarse (5-minute-or-worse) value exists on a
// SEPARATE endpoint (openInterestHist) this package deliberately does not
// poll for v1 (see docs/research/binance-momentum-source-v1.md's own
// "What this PR does not do" section) -- capturing it is a design choice
// left to a later PR, not settled here.
type OpenInterestReading struct {
	Symbol     string
	Amount     string
	EventAt    time.Time
	ObservedAt time.Time
}

type OpenInterestFn func(context.Context, OpenInterestReading) error

// PollOpenInterest polls GET /fapi/v1/openInterest for each of symbols in
// a round-robin loop, spaced by interval PER SYMBOL (not a burst of all
// symbols at once), until ctx is cancelled. At weight 1 per call (see the
// capability preflight's own rate-limit findings, 2400/min budget), the
// caller is responsible for choosing an interval that keeps total request
// weight well inside that budget for however many symbols it passes --
// this function does not itself enforce a budget across multiple
// concurrent callers or shards.
//
// The FIRST poll for the first symbol fires immediately, not after waiting
// one full perSymbolDelay: unlike Bybit's ticker push (OI arrives with the
// very first message), a poll-based source that waited a full interval
// before its first reading would leave every symbol with no OI data at all
// for up to that long after starting -- a real cold-start gap this
// deliberately avoids, not just a testing convenience.
//
// A consume error is logged and does NOT stop the loop (amended after a
// code-review finding, before any real wiring): unlike a WebSocket source
// where a fatal read error genuinely means the connection is gone and
// reconnecting is the only option, this is a stateless poll loop with
// nothing to reconnect -- treating one transient consumer failure (e.g. a
// downstream write hiccup) as a reason to permanently stop OPEN INTEREST
// collection for every symbol on this venue would be a strictly worse
// outcome than skipping that one reading and trying again next tick, the
// same fail-soft-on-a-single-reading philosophy bybit.Adapter.StreamTicker
// already documents for Bybit's own push path.
func (s *Source) PollOpenInterest(
	ctx context.Context,
	symbols []string,
	interval time.Duration,
	consume OpenInterestFn,
) error {
	if len(symbols) == 0 {
		return nil
	}
	if interval <= 0 {
		return fmt.Errorf("poll interval must be positive, got %s", interval)
	}
	perSymbolDelay := max(interval/time.Duration(len(symbols)), time.Millisecond)

	index := 0
	poll := func() {
		symbol := symbols[index%len(symbols)]
		index++
		reading, err := s.fetchOpenInterest(ctx, symbol)
		if err != nil {
			slog.Warn("binance.open_interest.poll_failed", "symbol", symbol, "err", err)
			return
		}
		if err := consume(ctx, reading); err != nil {
			slog.Warn("binance.open_interest.consume_failed", "symbol", symbol, "err", err)
		}
	}

	poll()

	ticker := time.NewTicker(perSymbolDelay)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			poll()
		}
	}
}

func (s *Source) fetchOpenInterest(ctx context.Context, symbol string) (OpenInterestReading, error) {
	client := s.httpClientOrDefault()
	restURL := s.restURLOrDefault()
	observedAt := time.Now()
	// Regression: Binance's REST API matches the symbol query param
	// case-exactly (it does not accept "btcusdt" the way it accepts
	// "BTCUSDT") -- a caller passing a non-normalized symbol must still
	// reach the venue correctly, not fail with an unrecognized-symbol
	// error. The response's own symbol is normalized too (below), but that
	// alone does not fix a request that was already sent with the wrong
	// case.
	endpoint := buildQueryURL(restURL, "/fapi/v1/openInterest", url.Values{"symbol": {wsstream.NormalizeSymbol(symbol)}})
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return OpenInterestReading{}, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return OpenInterestReading{}, err
	}
	if resp.StatusCode != http.StatusOK {
		_ = resp.Body.Close()
		return OpenInterestReading{}, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	b, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil {
		return OpenInterestReading{}, fmt.Errorf("read body: %w", err)
	}

	var body struct {
		Symbol       string `json:"symbol"`
		OpenInterest string `json:"openInterest"`
		Time         int64  `json:"time"`
	}
	if err := json.Unmarshal(b, &body); err != nil {
		return OpenInterestReading{}, fmt.Errorf("decode: %w", err)
	}
	if body.Symbol == "" || body.OpenInterest == "" || body.Time <= 0 {
		return OpenInterestReading{}, fmt.Errorf("incomplete open interest response for %s", symbol)
	}
	return OpenInterestReading{
		Symbol:     wsstream.NormalizeSymbol(body.Symbol),
		Amount:     body.OpenInterest,
		EventAt:    time.UnixMilli(body.Time),
		ObservedAt: observedAt,
	}, nil
}

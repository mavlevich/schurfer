package binance

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"sync"
	"sync/atomic"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/wsstream"
)

type LongShortRatioReading struct {
	Symbol       string
	Ratio        string
	LongAccount  string
	ShortAccount string
	EventAt      time.Time
	RequestedAt  time.Time
	ObservedAt   time.Time
	UsedWeight1m int
}

type LSRSchedulerConfig struct {
	Workers            int
	RateLimitPerMinute int
}

type LongShortRatioFn func(context.Context, LongShortRatioReading) error

func (s *Source) PollLongShortRatio(
	ctx context.Context,
	symbols []string,
	cfg LSRSchedulerConfig,
	consume LongShortRatioFn,
) error {
	if len(symbols) == 0 {
		return nil
	}
	if cfg.RateLimitPerMinute <= 0 {
		return fmt.Errorf("rate limit per minute must be positive, got %d", cfg.RateLimitPerMinute)
	}
	workers := cfg.Workers
	if workers < 1 {
		workers = 1
	}
	if workers > len(symbols) {
		workers = len(symbols)
	}

	refillInterval := max(time.Minute/time.Duration(cfg.RateLimitPerMinute), time.Millisecond)
	refillTicker := time.NewTicker(refillInterval)
	defer refillTicker.Stop()
	limiter := newTokenBucket(workers, refillTicker.C)
	defer limiter.stop()

	var nextIndex atomic.Uint64
	var pausedUntilNanos atomic.Int64

	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				if !waitOutPause(ctx, &pausedUntilNanos) {
					return
				}
				if !limiter.wait(ctx) {
					return
				}
				idx := nextIndex.Add(1) - 1
				symbol := symbols[idx%uint64(len(symbols))]

				reading, err := s.fetchLongShortRatio(ctx, symbol)
				if err != nil {
					if rle, ok := err.(*RateLimitError); ok {
						pauseUntilAtLeast(&pausedUntilNanos, time.Now().Add(rle.RetryAfter))
						level := slog.LevelWarn
						if rle.StatusCode == http.StatusTeapot {
							level = slog.LevelError
						}
						slog.Log(ctx, level, "binance.lsr.rate_limited",
							"symbol", symbol, "status", rle.StatusCode, "retry_after", rle.RetryAfter.String())
						continue
					}
					if ctx.Err() != nil {
						return
					}
					slog.Warn("binance.lsr.poll_failed", "symbol", symbol, "err", err)
					continue
				}
				if reading.UsedWeight1m >= 1920 {
					slog.Warn("binance.lsr.used_weight_high",
						"symbol", symbol, "used_weight_1m", reading.UsedWeight1m)
				}
				if err := consume(ctx, reading); err != nil {
					slog.Warn("binance.lsr.consume_failed", "symbol", symbol, "err", err)
				}
			}
		}()
	}
	wg.Wait()
	return nil
}

func (s *Source) fetchLongShortRatio(ctx context.Context, symbol string) (LongShortRatioReading, error) {
	client := s.httpClientOrDefault()
	restURL := s.restURLOrDefault()
	requestedAt := time.Now()

	params := url.Values{
		"symbol": {wsstream.NormalizeSymbol(symbol)},
		"period": {"5m"},
		"limit":  {"1"},
	}
	endpoint := buildQueryURL(restURL, "/futures/data/globalLongShortAccountRatio", params)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return LongShortRatioReading{}, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return LongShortRatioReading{}, err
	}
	if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode == http.StatusTeapot {
		retryAfter := parseRetryAfter(resp.Header.Get("Retry-After"))
		_ = resp.Body.Close()
		return LongShortRatioReading{}, &RateLimitError{StatusCode: resp.StatusCode, RetryAfter: retryAfter}
	}
	if resp.StatusCode != http.StatusOK {
		_ = resp.Body.Close()
		return LongShortRatioReading{}, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	usedWeight1m := parseUsedWeight(resp.Header.Get("X-Mbx-Used-Weight-1m"))
	b, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil {
		return LongShortRatioReading{}, fmt.Errorf("read body: %w", err)
	}

	var body []struct {
		Symbol         string `json:"symbol"`
		LongShortRatio string `json:"longShortRatio"`
		LongAccount    string `json:"longAccount"`
		ShortAccount   string `json:"shortAccount"`
		Timestamp      int64  `json:"timestamp"`
	}
	if err := json.Unmarshal(b, &body); err != nil {
		return LongShortRatioReading{}, fmt.Errorf("decode: %w", err)
	}
	if len(body) == 0 {
		return LongShortRatioReading{}, fmt.Errorf("empty response for %s", symbol)
	}
	latest := body[0]
	if latest.Symbol == "" || latest.LongShortRatio == "" || latest.Timestamp <= 0 {
		return LongShortRatioReading{}, fmt.Errorf("incomplete LSR response for %s", symbol)
	}
	return LongShortRatioReading{
		Symbol:       wsstream.NormalizeSymbol(latest.Symbol),
		Ratio:        latest.LongShortRatio,
		LongAccount:  latest.LongAccount,
		ShortAccount: latest.ShortAccount,
		EventAt:      time.UnixMilli(latest.Timestamp),
		RequestedAt:  requestedAt,
		ObservedAt:   time.Now(),
		UsedWeight1m: usedWeight1m,
	}, nil
}

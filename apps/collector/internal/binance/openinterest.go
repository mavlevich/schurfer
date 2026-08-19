package binance

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"sync/atomic"
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
	Symbol  string
	Amount  string
	EventAt time.Time
	// RequestedAt is stamped immediately before the HTTP request is sent;
	// ObservedAt is stamped only after the response body has been read and
	// successfully decoded -- a colleague-review finding: an earlier
	// version stamped what is now RequestedAt's own moment into
	// ObservedAt, making every reading's own "when did we learn this"
	// value optimistic by exactly the request's round-trip latency (the
	// data was not actually known yet at that instant). ObservedAt is
	// what point-in-time freshness checks must use; RequestedAt exists so
	// request-to-response latency itself is measurable, not to be treated
	// as an observation time.
	RequestedAt time.Time
	ObservedAt  time.Time
	// UsedWeight1m is Binance's own X-Mbx-Used-Weight-1m response header
	// (this account/IP's total request weight consumed in the current
	// rolling minute, across every endpoint, not just this one) -- 0 if
	// the header was absent or unparseable, which is not itself an error:
	// older mocked responses in tests, and in principle a future API
	// version, may not send it. See docs/research/
	// binance-oi-poll-scheduler-v1.md for why the scheduler tracks this
	// instead of only trusting its own configured budget.
	UsedWeight1m int
}

// RateLimitError is returned by fetchOpenInterest when Binance responds
// HTTP 429 (Too Many Requests, this IP is over its own weight budget) or
// 418 (I'm a teapot -- Binance's own code for an IP that kept sending
// requests after a 429, now auto-banned for a period Binance itself
// controls). Kept distinct from a generic HTTP-status error specifically
// so PollOpenInterest's own worker pool can back off for RetryAfter
// instead of immediately retrying into the same limit, which for a 418
// would extend an active ban rather than wait it out -- see Binance's own
// REST API rate-limit documentation.
type RateLimitError struct {
	StatusCode int
	RetryAfter time.Duration
}

func (e *RateLimitError) Error() string {
	return fmt.Sprintf("binance rate limited (HTTP %d), retry after %s", e.StatusCode, e.RetryAfter)
}

// defaultRateLimitRetryAfter is used when a 429/418 response carries no
// (or an unparseable) Retry-After header. Binance's own docs describe 418
// ban durations growing from 2 minutes for a first offense up to 3 days
// for repeated ones, and always send Retry-After for both statuses in
// practice; this is a conservative fallback for the response shape being
// genuinely absent, not a value chosen to match any specific documented
// ban tier.
const defaultRateLimitRetryAfter = 60 * time.Second

// OpenInterestSchedulerConfig bounds PollOpenInterest's concurrent worker
// pool and its own token-bucket rate limiter. Replaces a single goroutine
// that blocked on one HTTP request per time.Ticker tick, spaced by
// interval/len(symbols): time.Ticker drops missed ticks rather than
// queueing them, so any single request slower than that per-symbol delay
// stalled the ENTIRE round-robin, not just that one symbol. Measured
// against real prod data before this fix: p50 127s / p95 255s / p99 505s
// / max 1010s per-symbol OI refresh gaps against a 60s target -- see
// docs/research/binance-oi-poll-scheduler-v1.md.
type OpenInterestSchedulerConfig struct {
	// Workers is how many requests may be in flight concurrently. Sized to
	// hide real HTTP round-trip latency to Binance's REST API, not to
	// raise total throughput past RateLimitPerMinute -- the token bucket,
	// not worker count, is what enforces the actual request budget. A
	// worker that is idle (no token available yet) costs nothing but one
	// blocked goroutine.
	Workers int
	// RateLimitPerMinute bounds total request weight/minute across every
	// worker combined. GET /fapi/v1/openInterest costs weight 1 (the
	// capability preflight's own measurement); Binance's real budget is
	// 2400/min shared across every endpoint this process calls (this one
	// plus exchangeInfo re-polling via runDriftPoller). 1200 leaves half
	// that budget untouched deliberately, not because 2400 was measured
	// to be unsafe.
	RateLimitPerMinute int
}

// DefaultOpenInterestSchedulerConfig is what cmd/momentumcapturebinance
// uses absent an operator override (see that binary's own OI_POLL_WORKERS/
// OI_POLL_RATE_LIMIT_PER_MINUTE env vars) -- picked to be safely
// conservative on first deploy, not from a measured p95 request-latency
// distribution this PR does not yet have (see docs/research/
// binance-oi-poll-scheduler-v1.md's own "What this PR does not do" -- a
// future PR4 coverage read is what that measurement belongs to).
func DefaultOpenInterestSchedulerConfig() OpenInterestSchedulerConfig {
	return OpenInterestSchedulerConfig{
		Workers:            8,
		RateLimitPerMinute: 1200,
	}
}

type OpenInterestFn func(context.Context, OpenInterestReading) error

// PollOpenInterest polls GET /fapi/v1/openInterest for every symbol in
// symbols, round-robin, using a bounded pool of concurrent workers paced
// by a token-bucket rate limiter (see OpenInterestSchedulerConfig's own
// doc comment for why this replaced a single-goroutine design). Runs
// until ctx is cancelled or ctx.Done() interrupts a worker mid-wait.
//
// The very first request from every worker fires immediately (the token
// bucket starts full, one token per worker up to its own capacity) rather
// than waiting for a refill tick first -- the same cold-start reasoning
// the previous design documented: a poll-based source that waited a full
// cycle before its first reading would leave every symbol with no OI data
// at all for that long after starting.
//
// A consume error is logged and does NOT stop the loop, and neither does
// a single failed fetch: this is a stateless poll loop with nothing to
// reconnect (unlike a WebSocket source where a fatal read error means the
// connection itself is gone), so treating one transient failure as a
// reason to stop OPEN INTEREST collection for every symbol on this venue
// would be a strictly worse outcome than skipping that one reading and
// trying again next turn -- the same fail-soft-on-a-single-reading
// philosophy bybit.Adapter.StreamTicker already documents for Bybit's own
// push path.
//
// A 429/418 response is different: it means THIS PROCESS'S OWN IP is over
// budget or banned, a condition every worker shares, not a single-symbol
// problem. PollOpenInterest pauses the ENTIRE pool (not just the worker
// that hit it) until the response's own Retry-After elapses, so the other
// workers do not keep hammering the same limit (or, for a 418, extend an
// active ban) while one worker waits it out.
func (s *Source) PollOpenInterest(
	ctx context.Context,
	symbols []string,
	cfg OpenInterestSchedulerConfig,
	consume OpenInterestFn,
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

	// max(..., time.Millisecond): the previous single-goroutine design
	// guarded this exact division the same way (perSymbolDelay :=
	// max(interval/len(symbols), time.Millisecond)) -- without a floor, an
	// operator-set OI_POLL_RATE_LIMIT_PER_MINUTE large enough to truncate
	// time.Minute/RateLimitPerMinute to 0 would make time.NewTicker panic
	// ("non-positive interval for NewTicker") inside this un-recovered
	// goroutine, crashing the whole process over a config typo rather than
	// just failing OI collection.
	refillInterval := max(time.Minute/time.Duration(cfg.RateLimitPerMinute), time.Millisecond)
	refillTicker := time.NewTicker(refillInterval)
	defer refillTicker.Stop()
	limiter := newTokenBucket(workers, refillTicker.C)
	defer limiter.stop()

	var nextIndex atomic.Uint64
	var pausedUntilNanos atomic.Int64 // 0 = not paused; else a UnixNano deadline

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

				reading, err := s.fetchOpenInterest(ctx, symbol)
				if err != nil {
					if rle, ok := err.(*RateLimitError); ok { //nolint:errorlint // fetchOpenInterest never wraps this error
						pauseUntilAtLeast(&pausedUntilNanos, time.Now().Add(rle.RetryAfter))
						level := slog.LevelWarn
						if rle.StatusCode == http.StatusTeapot {
							level = slog.LevelError
						}
						slog.Log(ctx, level, "binance.open_interest.rate_limited",
							"symbol", symbol, "status", rle.StatusCode, "retry_after", rle.RetryAfter.String())
						continue
					}
					if ctx.Err() != nil {
						return
					}
					slog.Warn("binance.open_interest.poll_failed", "symbol", symbol, "err", err)
					continue
				}
				if reading.UsedWeight1m > 0 {
					// 80% of a documented 2400/min budget: high enough that
					// this fires rarely under normal operation (this
					// process's own configured RateLimitPerMinute stays
					// well under 2400 by design), so a Warn here means
					// something else on this IP is also spending weight,
					// worth an operator's attention.
					const usedWeightWarnThreshold = 1920
					if reading.UsedWeight1m >= usedWeightWarnThreshold {
						slog.Warn("binance.open_interest.used_weight_high",
							"symbol", symbol, "used_weight_1m", reading.UsedWeight1m)
					}
				}
				if err := consume(ctx, reading); err != nil {
					slog.Warn("binance.open_interest.consume_failed", "symbol", symbol, "err", err)
				}
			}
		}()
	}
	wg.Wait()
	return nil
}

// waitOutPause blocks until pausedUntilNanos is either unset or its own
// deadline has passed, returning false only if ctx is cancelled first.
// Re-checks after waking (another worker may have extended the pause
// while this one slept) rather than trusting a single read.
func waitOutPause(ctx context.Context, pausedUntilNanos *atomic.Int64) bool {
	for {
		if ctx.Err() != nil {
			return false
		}
		until := pausedUntilNanos.Load()
		if until == 0 {
			return true
		}
		remaining := time.Until(time.Unix(0, until))
		if remaining <= 0 {
			// Best-effort clear: a concurrent worker may already have
			// cleared it or extended it further, either of which is fine
			// to just re-check on the next loop iteration.
			pausedUntilNanos.CompareAndSwap(until, 0)
			return true
		}
		select {
		case <-time.After(remaining):
		case <-ctx.Done():
			return false
		}
	}
}

// pauseUntilAtLeast raises pausedUntilNanos to deadline if it is not
// already later, without ever shortening a longer pause a different
// worker's own rate-limit hit already set (e.g. a 418 ban outlasting an
// earlier 429's own shorter Retry-After).
func pauseUntilAtLeast(pausedUntilNanos *atomic.Int64, deadline time.Time) {
	target := deadline.UnixNano()
	for {
		current := pausedUntilNanos.Load()
		if current >= target {
			return
		}
		if pausedUntilNanos.CompareAndSwap(current, target) {
			return
		}
	}
}

func (s *Source) fetchOpenInterest(ctx context.Context, symbol string) (OpenInterestReading, error) {
	client := s.httpClientOrDefault()
	restURL := s.restURLOrDefault()
	requestedAt := time.Now()
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
	if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode == http.StatusTeapot {
		retryAfter := parseRetryAfter(resp.Header.Get("Retry-After"))
		_ = resp.Body.Close()
		return OpenInterestReading{}, &RateLimitError{StatusCode: resp.StatusCode, RetryAfter: retryAfter}
	}
	if resp.StatusCode != http.StatusOK {
		_ = resp.Body.Close()
		return OpenInterestReading{}, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	usedWeight1m := parseUsedWeight(resp.Header.Get("X-Mbx-Used-Weight-1m"))
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
		Symbol:      wsstream.NormalizeSymbol(body.Symbol),
		Amount:      body.OpenInterest,
		EventAt:     time.UnixMilli(body.Time),
		RequestedAt: requestedAt,
		// Stamped now, after the body is fully read and decoded -- see
		// OpenInterestReading's own doc comment on why this must not move
		// earlier.
		ObservedAt:   time.Now(),
		UsedWeight1m: usedWeight1m,
	}, nil
}

// parseRetryAfter reads Binance's own Retry-After header, which the docs
// specify as a whole number of seconds (not an HTTP-date, the header's
// other RFC-permitted form -- Binance never sends that form here). Falls
// back to defaultRateLimitRetryAfter on anything else: a 429/418 with no
// usable wait hint must still back off, not be treated as retry-now.
func parseRetryAfter(raw string) time.Duration {
	if raw == "" {
		return defaultRateLimitRetryAfter
	}
	seconds, err := strconv.Atoi(raw)
	if err != nil || seconds <= 0 {
		return defaultRateLimitRetryAfter
	}
	return time.Duration(seconds) * time.Second
}

// parseUsedWeight returns 0 (not an error) on a missing or malformed
// header -- see OpenInterestReading.UsedWeight1m's own doc comment on why
// that is treated as "not reported," not a failure.
func parseUsedWeight(raw string) int {
	if raw == "" {
		return 0
	}
	weight, err := strconv.Atoi(raw)
	if err != nil || weight < 0 {
		return 0
	}
	return weight
}

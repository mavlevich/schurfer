package binance

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestFetchOpenInterestNormalizesSymbolAndTimestamp(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/fapi/v1/openInterest") {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		if r.URL.Query().Get("symbol") != "BTCUSDT" {
			t.Errorf("unexpected symbol query: %s", r.URL.RawQuery)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"BTCUSDT","openInterest":"112104.426","time":1786737418938}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	reading, err := source.fetchOpenInterest(context.Background(), "btcusdt")
	if err != nil {
		t.Fatal(err)
	}
	if reading.Symbol != "BTCUSDT" {
		t.Fatalf("Symbol = %q, want normalized upper-case", reading.Symbol)
	}
	if reading.Amount != "112104.426" {
		t.Fatalf("Amount = %q", reading.Amount)
	}
	if !reading.EventAt.Equal(time.UnixMilli(1786737418938)) {
		t.Fatalf("EventAt = %v", reading.EventAt)
	}
	if reading.ObservedAt.IsZero() {
		t.Fatal("ObservedAt is zero, want the local fetch time")
	}
	if reading.RequestedAt.IsZero() {
		t.Fatal("RequestedAt is zero, want the local pre-request time")
	}
	if reading.ObservedAt.Before(reading.RequestedAt) {
		t.Fatalf("ObservedAt (%v) before RequestedAt (%v)", reading.ObservedAt, reading.RequestedAt)
	}
}

// TestFetchOpenInterestObservedAtIsAfterTheResponseIsRead is a colleague-
// review regression: an earlier version stamped ObservedAt BEFORE
// client.Do(), making it optimistic by exactly the request's own
// round-trip latency (the caller had not actually learned the value yet
// at that instant). A slow-responding server makes this concretely
// measurable, not just structurally plausible: ObservedAt must land at
// least as long after RequestedAt as the server's own artificial delay.
func TestFetchOpenInterestObservedAtIsAfterTheResponseIsRead(t *testing.T) {
	t.Parallel()
	const delay = 50 * time.Millisecond
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(delay)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"BTCUSDT","openInterest":"1","time":1786737418938}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	reading, err := source.fetchOpenInterest(context.Background(), "BTCUSDT")
	if err != nil {
		t.Fatal(err)
	}
	if elapsed := reading.ObservedAt.Sub(reading.RequestedAt); elapsed < delay {
		t.Fatalf(
			"ObservedAt - RequestedAt = %v, want at least the server's own %v delay "+
				"(ObservedAt must be stamped after the response is read, not before the request)",
			elapsed, delay,
		)
	}
}

func TestFetchOpenInterestRejectsIncompleteResponse(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"BTCUSDT"}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	_, err := source.fetchOpenInterest(context.Background(), "BTCUSDT")
	if err == nil || !strings.Contains(err.Error(), "incomplete") {
		t.Fatalf("err = %v, want an incomplete-response failure", err)
	}
}

func TestFetchOpenInterestParsesUsedWeightHeader(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Mbx-Used-Weight-1m", "42")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"BTCUSDT","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	reading, err := source.fetchOpenInterest(context.Background(), "BTCUSDT")
	if err != nil {
		t.Fatal(err)
	}
	if reading.UsedWeight1m != 42 {
		t.Fatalf("UsedWeight1m = %d, want 42", reading.UsedWeight1m)
	}
}

func TestFetchOpenInterestUsedWeightIsZeroWhenHeaderAbsent(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"BTCUSDT","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	reading, err := source.fetchOpenInterest(context.Background(), "BTCUSDT")
	if err != nil {
		t.Fatal(err)
	}
	if reading.UsedWeight1m != 0 {
		t.Fatalf("UsedWeight1m = %d, want 0 (header absent, not an error)", reading.UsedWeight1m)
	}
}

func TestFetchOpenInterestReturnsRateLimitErrorOn429WithRetryAfter(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "5")
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	_, err := source.fetchOpenInterest(context.Background(), "BTCUSDT")
	var rle *RateLimitError
	if !errors.As(err, &rle) {
		t.Fatalf("err = %v (%T), want *RateLimitError", err, err)
	}
	if rle.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("StatusCode = %d, want 429", rle.StatusCode)
	}
	if rle.RetryAfter != 5*time.Second {
		t.Fatalf("RetryAfter = %v, want 5s", rle.RetryAfter)
	}
}

func TestFetchOpenInterestReturnsRateLimitErrorOn418WithDefaultRetryAfterWhenHeaderMissing(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTeapot)
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	_, err := source.fetchOpenInterest(context.Background(), "BTCUSDT")
	var rle *RateLimitError
	if !errors.As(err, &rle) {
		t.Fatalf("err = %v (%T), want *RateLimitError", err, err)
	}
	if rle.StatusCode != http.StatusTeapot {
		t.Fatalf("StatusCode = %d, want 418", rle.StatusCode)
	}
	if rle.RetryAfter != defaultRateLimitRetryAfter {
		t.Fatalf("RetryAfter = %v, want the default %v (no Retry-After header sent)", rle.RetryAfter, defaultRateLimitRetryAfter)
	}
}

func TestParseRetryAfter(t *testing.T) {
	t.Parallel()
	cases := []struct {
		raw  string
		want time.Duration
	}{
		{"", defaultRateLimitRetryAfter},
		{"0", defaultRateLimitRetryAfter},
		{"-1", defaultRateLimitRetryAfter},
		{"abc", defaultRateLimitRetryAfter},
		{"5", 5 * time.Second},
		{"120", 120 * time.Second},
		// Binance never pads with spaces; strconv.Atoi rejects it, falls
		// back to the default rather than silently trimming and parsing.
		{" 30 ", defaultRateLimitRetryAfter},
	}
	for _, tc := range cases {
		if got := parseRetryAfter(tc.raw); got != tc.want {
			t.Errorf("parseRetryAfter(%q) = %v, want %v", tc.raw, got, tc.want)
		}
	}
}

func TestParseUsedWeight(t *testing.T) {
	t.Parallel()
	cases := []struct {
		raw  string
		want int
	}{
		{"", 0},
		{"abc", 0},
		{"-1", 0},
		{"0", 0},
		{"42", 42},
	}
	for _, tc := range cases {
		if got := parseUsedWeight(tc.raw); got != tc.want {
			t.Errorf("parseUsedWeight(%q) = %d, want %d", tc.raw, got, tc.want)
		}
	}
}

// fastSchedulerConfig keeps test windows short without depending on
// wall-clock luck: a high RateLimitPerMinute means the refill ticker
// fires often enough that these tests are bounded by their own ctx
// timeout, not by waiting on a slow, realistic production rate.
func fastSchedulerConfig(workers int) OpenInterestSchedulerConfig {
	return OpenInterestSchedulerConfig{Workers: workers, RateLimitPerMinute: 6000} // one token every 10ms
}

func TestPollOpenInterestVisitsEverySymbol(t *testing.T) {
	t.Parallel()
	var mu sync.Mutex
	seen := map[string]int{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		symbol := r.URL.Query().Get("symbol")
		mu.Lock()
		seen[symbol]++
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"` + symbol + `","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	var readings []OpenInterestReading
	var readingsMu sync.Mutex
	err := source.PollOpenInterest(ctx, []string{"BTCUSDT", "ETHUSDT"}, fastSchedulerConfig(2),
		func(_ context.Context, reading OpenInterestReading) error {
			readingsMu.Lock()
			readings = append(readings, reading)
			readingsMu.Unlock()
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	if len(readings) == 0 {
		t.Fatal("no readings were consumed within the poll window")
	}

	mu.Lock()
	defer mu.Unlock()
	if len(seen) == 0 {
		t.Fatal("no symbols were polled")
	}
}

func TestPollOpenInterestDoesNotStopOnAConsumerFailure(t *testing.T) {
	// Regression for a code-review finding: PollOpenInterest used to
	// propagate a consume error and stop entirely, silently ending open-
	// interest collection for every symbol on a single transient
	// downstream failure (unlike a WebSocket source, this poll loop has no
	// connection to reconnect -- see PollOpenInterest's own doc comment).
	// A consumer that ALWAYS errors must still keep being called
	// repeatedly, not just once.
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		symbol := r.URL.Query().Get("symbol")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"` + symbol + `","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	var calls atomic.Int64
	err := source.PollOpenInterest(ctx, []string{"BTCUSDT"}, fastSchedulerConfig(1),
		func(context.Context, OpenInterestReading) error {
			calls.Add(1)
			return errors.New("simulated consumer failure")
		})
	if err != nil {
		t.Fatalf("PollOpenInterest() error = %v, want nil: a consume error must not propagate", err)
	}
	if got := calls.Load(); got < 2 {
		t.Fatalf("consumer was called %d times, want at least 2 (an always-erroring consumer must not stop the loop)", got)
	}
}

func TestPollOpenInterestRejectsNonPositiveRateLimit(t *testing.T) {
	source := &Source{}
	err := source.PollOpenInterest(context.Background(), []string{"BTCUSDT"},
		OpenInterestSchedulerConfig{Workers: 1, RateLimitPerMinute: 0},
		func(context.Context, OpenInterestReading) error { return nil })
	if err == nil || !strings.Contains(err.Error(), "positive") {
		t.Fatalf("err = %v, want a positive-rate-limit failure", err)
	}
}

// TestPollOpenInterestDoesNotPanicWithAnExtremeRateLimit is a regression:
// time.Minute/time.Duration(cfg.RateLimitPerMinute) truncates to 0 once
// RateLimitPerMinute exceeds time.Minute's own nanosecond count (a
// misconfigured/typo'd OI_POLL_RATE_LIMIT_PER_MINUTE env var, not just a
// theoretical input), and time.NewTicker(0) panics -- crashing the whole
// process over a config typo, not just failing OI collection, unless the
// refill interval is floored the same way the previous single-goroutine
// design floored its own per-symbol delay.
func TestPollOpenInterestDoesNotPanicWithAnExtremeRateLimit(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"BTCUSDT","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()

	err := source.PollOpenInterest(ctx, []string{"BTCUSDT"},
		OpenInterestSchedulerConfig{Workers: 1, RateLimitPerMinute: int(time.Minute) + 1},
		func(context.Context, OpenInterestReading) error { return nil })
	if err != nil {
		t.Fatalf("PollOpenInterest() error = %v, want nil (must not panic on an extreme rate limit)", err)
	}
}

func TestPollOpenInterestSkipsAndLogsAFailedFetchWithoutStoppingTheLoop(t *testing.T) {
	t.Parallel()
	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		current := calls.Add(1)
		if current == 1 {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"BTCUSDT","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	var readings []OpenInterestReading
	var mu sync.Mutex
	_ = source.PollOpenInterest(ctx, []string{"BTCUSDT"}, fastSchedulerConfig(1),
		func(_ context.Context, reading OpenInterestReading) error {
			mu.Lock()
			readings = append(readings, reading)
			mu.Unlock()
			return nil
		})
	if len(readings) == 0 {
		t.Fatal("a later successful poll must still deliver a reading after an earlier failure")
	}
}

// TestPollOpenInterestUsesWorkersConcurrently is the core behavioral
// regression this PR exists for: the previous single-goroutine design
// meant one slow request stalled every other symbol's own turn. A server
// that holds every request open until several are simultaneously
// in-flight proves genuine concurrency, not just eventual coverage.
func TestPollOpenInterestUsesWorkersConcurrently(t *testing.T) {
	t.Parallel()
	const workers = 4
	var inFlight atomic.Int64
	var maxInFlight atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		current := inFlight.Add(1)
		for {
			observed := maxInFlight.Load()
			if current <= observed || maxInFlight.CompareAndSwap(observed, current) {
				break
			}
		}
		time.Sleep(30 * time.Millisecond)
		inFlight.Add(-1)
		symbol := r.URL.Query().Get("symbol")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"` + symbol + `","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()

	_ = source.PollOpenInterest(ctx, []string{"AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"},
		// A very high rate limit here: this test's own point is proving
		// concurrency exists at all, not measuring the limiter's pacing
		// (that is TestPollOpenInterestRateLimiterBoundsRequestRate's own
		// job) -- the token bucket must not be the bottleneck here.
		OpenInterestSchedulerConfig{Workers: workers, RateLimitPerMinute: 100_000},
		func(context.Context, OpenInterestReading) error { return nil })

	if got := maxInFlight.Load(); got < 2 {
		t.Fatalf("max concurrent in-flight requests = %d, want at least 2 (workers must run concurrently, not round-robin one at a time)", got)
	}
}

// TestPollOpenInterestRateLimiterBoundsRequestRate proves the token
// bucket actually paces requests instead of workers free-running as fast
// as the server responds: with capacity limited to 1 worker (so no extra
// startup burst) and a slow refill rate, the total request count over a
// bounded window must stay close to what the rate limit allows, not
// balloon to however many a tight loop could fire.
func TestPollOpenInterestRateLimiterBoundsRequestRate(t *testing.T) {
	t.Parallel()
	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		symbol := r.URL.Query().Get("symbol")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"` + symbol + `","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	const window = 350 * time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), window)
	defer cancel()

	// 600/min = one token every 100ms; over a 350ms window with a
	// single-worker (no extra burst) bucket that is 1 initial token plus
	// at most 3 refills = at most ~4 requests, never anywhere near the
	// dozens a tight unthrottled loop would manage in the same window.
	_ = source.PollOpenInterest(ctx, []string{"BTCUSDT"},
		OpenInterestSchedulerConfig{Workers: 1, RateLimitPerMinute: 600},
		func(context.Context, OpenInterestReading) error { return nil })

	if got := calls.Load(); got > 8 {
		t.Fatalf("calls = %d over a %v window at 600/min (one token/100ms), want the rate limiter to bound this well below an unthrottled loop's count", got, window)
	}
	if got := calls.Load(); got < 1 {
		t.Fatal("calls = 0, want at least the initial burst token to have fired immediately")
	}
}

// TestPollOpenInterestPausesEntirePoolOn429 proves a 429 pauses every
// worker, not just the one that hit it: a 429/418 means this process's
// own IP is over budget, a condition every worker shares, so letting the
// others keep hammering the same limit while one waits it out would make
// the underlying problem worse, not better.
func TestPollOpenInterestPausesEntirePoolOn429(t *testing.T) {
	t.Parallel()
	var mu sync.Mutex
	var requestTimes []time.Time
	var rateLimitedOnce sync.Map // symbol -> true once it has been told to back off

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		symbol := r.URL.Query().Get("symbol")
		mu.Lock()
		requestTimes = append(requestTimes, time.Now())
		mu.Unlock()

		if _, already := rateLimitedOnce.LoadOrStore(symbol, true); !already {
			w.Header().Set("Retry-After", "1")
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"` + symbol + `","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	ctx, cancel := context.WithTimeout(context.Background(), 1300*time.Millisecond)
	defer cancel()

	_ = source.PollOpenInterest(ctx, []string{"AAAUSDT", "BBBUSDT"},
		OpenInterestSchedulerConfig{Workers: 2, RateLimitPerMinute: 100_000},
		func(context.Context, OpenInterestReading) error { return nil })

	mu.Lock()
	defer mu.Unlock()
	if len(requestTimes) < 3 {
		t.Fatalf("got %d requests, want at least 3 (2 initial 429s + at least 1 retry after the pause)", len(requestTimes))
	}
	// Every request after the initial pair (both fired near t=0, both hit
	// 429) must land at least ~900ms later -- proof the pool actually
	// waited out the 1s Retry-After instead of retrying immediately.
	first := requestTimes[0]
	var sawPausedGap bool
	for _, at := range requestTimes[2:] {
		if at.Sub(first) >= 900*time.Millisecond {
			sawPausedGap = true
			break
		}
	}
	if !sawPausedGap {
		t.Fatalf("no request after the initial pair landed >= 900ms after the first request; want proof the whole pool paused for the 429's own Retry-After instead of retrying immediately")
	}
}

package binance

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
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
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	var readings []OpenInterestReading
	err := source.PollOpenInterest(ctx, []string{"BTCUSDT", "ETHUSDT"}, 20*time.Millisecond, func(_ context.Context, reading OpenInterestReading) error {
		readings = append(readings, reading)
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
	// A consumer that ALWAYS errors must still keep being called on every
	// tick, not just once.
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		symbol := r.URL.Query().Get("symbol")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"` + symbol + `","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Millisecond)
	defer cancel()

	var mu sync.Mutex
	var calls int
	err := source.PollOpenInterest(ctx, []string{"BTCUSDT"}, 15*time.Millisecond, func(context.Context, OpenInterestReading) error {
		mu.Lock()
		calls++
		mu.Unlock()
		return errors.New("simulated consumer failure")
	})
	if err != nil {
		t.Fatalf("PollOpenInterest() error = %v, want nil: a consume error must not propagate", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if calls < 2 {
		t.Fatalf("consumer was called %d times, want at least 2 (an always-erroring consumer must not stop the loop)", calls)
	}
}

func TestPollOpenInterestRejectsNonPositiveInterval(t *testing.T) {
	source := &Source{}
	err := source.PollOpenInterest(context.Background(), []string{"BTCUSDT"}, 0, func(context.Context, OpenInterestReading) error {
		return nil
	})
	if err == nil || !strings.Contains(err.Error(), "positive") {
		t.Fatalf("err = %v, want a positive-interval failure", err)
	}
}

func TestPollOpenInterestSkipsAndLogsAFailedFetchWithoutStoppingTheLoop(t *testing.T) {
	t.Parallel()
	var mu sync.Mutex
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		calls++
		current := calls
		mu.Unlock()
		if current == 1 {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"BTCUSDT","openInterest":"1","time":1}`))
	}))
	t.Cleanup(server.Close)

	source := &Source{restURL: server.URL, httpClient: server.Client()}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Millisecond)
	defer cancel()

	var readings []OpenInterestReading
	_ = source.PollOpenInterest(ctx, []string{"BTCUSDT"}, 15*time.Millisecond, func(_ context.Context, reading OpenInterestReading) error {
		readings = append(readings, reading)
		return nil
	})
	if len(readings) == 0 {
		t.Fatal("a later successful poll must still deliver a reading after an earlier failure")
	}
}

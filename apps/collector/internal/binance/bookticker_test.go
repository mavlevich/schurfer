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

	"github.com/gorilla/websocket"
)

func TestHandleBookTickerPayloadNormalizesValidRows(t *testing.T) {
	now := time.Now()
	payload := []byte(`{
		"stream": "btcusdt@bookTicker",
		"data": {"e":"bookTicker","E":` + itoa(now.UnixMilli()) + `,"T":` + itoa(now.UnixMilli()) + `,"s":"btcusdt","b":"64999.5","B":"1.2","a":"65000.5","A":"0.8"}
	}`)
	var got []PublicBookTicker
	err := handleBookTickerPayload(context.Background(), payload, now, func(_ context.Context, update PublicBookTicker) error {
		got = append(got, update)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("got %d updates, want 1", len(got))
	}
	update := got[0]
	if update.Symbol != "BTCUSDT" {
		t.Fatalf("Symbol = %q, want normalized upper-case", update.Symbol)
	}
	if update.BidPrice != 64999.5 || update.AskPrice != 65000.5 {
		t.Fatalf("BidPrice/AskPrice = %v/%v", update.BidPrice, update.AskPrice)
	}
	if !update.EventAt.Equal(time.UnixMilli(now.UnixMilli())) {
		t.Fatalf("EventAt = %v, want %v", update.EventAt, time.UnixMilli(now.UnixMilli()))
	}
}

func TestHandleBookTickerPayloadIgnoresNonBookTickerFrames(t *testing.T) {
	payload := []byte(`{"stream":"btcusdt@markPrice","data":{"e":"markPriceUpdate","s":"BTCUSDT"}}`)
	called := false
	err := handleBookTickerPayload(context.Background(), payload, time.Now(), func(context.Context, PublicBookTicker) error {
		called = true
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if called {
		t.Fatal("consume was called for a non-bookTicker frame")
	}
}

func TestHandleBookTickerPayloadSkipsInvalidPrices(t *testing.T) {
	now := time.Now()
	cases := []struct {
		name string
		bid  string
		ask  string
	}{
		{"unparseable bid", "not-a-number", "65000.5"},
		{"unparseable ask", "64999.5", "not-a-number"},
		{"zero bid", "0", "65000.5"},
		{"negative ask", "64999.5", "-1"},
		{"crossed book", "65001", "65000"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			payload := []byte(`{"stream":"btcusdt@bookTicker","data":{"e":"bookTicker","E":` + itoa(now.UnixMilli()) + `,"s":"BTCUSDT","b":"` + tc.bid + `","a":"` + tc.ask + `"}}`)
			called := false
			err := handleBookTickerPayload(context.Background(), payload, now, func(context.Context, PublicBookTicker) error {
				called = true
				return nil
			})
			if err != nil {
				t.Fatal(err)
			}
			if called {
				t.Fatalf("consume was called for %s", tc.name)
			}
		})
	}
}

func TestHandleBookTickerPayloadPropagatesConsumerFailure(t *testing.T) {
	now := time.Now()
	payload := []byte(`{"stream":"btcusdt@bookTicker","data":{"e":"bookTicker","E":` + itoa(now.UnixMilli()) + `,"s":"BTCUSDT","b":"1","a":"2"}}`)
	wantErr := errors.New("boom")
	err := handleBookTickerPayload(context.Background(), payload, now, func(context.Context, PublicBookTicker) error {
		return wantErr
	})
	if !errors.Is(err, wantErr) {
		t.Fatalf("err = %v, want %v", err, wantErr)
	}
}

func TestHandleBookTickerPayloadRejectsMalformedJSON(t *testing.T) {
	err := handleBookTickerPayload(context.Background(), []byte("not json"), time.Now(), func(context.Context, PublicBookTicker) error {
		return nil
	})
	if err == nil {
		t.Fatal("expected a decode error")
	}
}

// TestBookTickerStreamURLUsesTheUnroutedPublicEndpointByDefault is the
// bookTicker-side mirror of TestTradeStreamURLUsesTheRoutedMarketEndpoint
// ByDefault: same incident, opposite direction -- bookTicker must stay on
// the OLD unrouted /stream path (wsPublicBaseURL's own doc comment).
// Regressing this to /market/stream would reproduce the exact silent-
// handshake-no-frames failure mode that incident was about.
func TestBookTickerStreamURLUsesTheUnroutedPublicEndpointByDefault(t *testing.T) {
	t.Parallel()
	source := NewSource()
	got := source.bookTickerStreamURL([]string{"BTCUSDT", "ETHUSDT"})
	want := "wss://fstream.binance.com/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker"
	if got != want {
		t.Fatalf("bookTickerStreamURL() = %q, want %q", got, want)
	}
}

// bookTickerWebSocketServer mirrors tradeDataWebSocketServer's own shape.
func bookTickerWebSocketServer(t *testing.T, symbol string) string {
	t.Helper()
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		frame := map[string]any{
			"stream": strings.ToLower(symbol) + "@bookTicker",
			"data": map[string]any{
				"e": "bookTicker", "E": time.Now().UnixMilli(), "T": time.Now().UnixMilli(),
				"s": symbol, "b": "64999.5", "B": "1.2", "a": "65000.5", "A": "0.8",
			},
		}
		if err := conn.WriteJSON(frame); err != nil {
			return
		}
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}))
	t.Cleanup(server.Close)
	return "ws" + strings.TrimPrefix(server.URL, "http")
}

func testPublicSource(url string) *Source {
	return &Source{wsPublicBaseURL: url, httpClient: &http.Client{Timeout: 2 * time.Second}}
}

func TestBookTickerStreamFiresConnectedLifecycleEventAndDeliversAnUpdate(t *testing.T) {
	t.Parallel()
	serverURL := bookTickerWebSocketServer(t, "BTCUSDT")
	source := testPublicSource(serverURL)
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	var mu sync.Mutex
	var events []BookTickerLifecycleEvent
	var updates []PublicBookTicker
	_ = source.bookTickerStream(ctx, []string{"BTCUSDT"}, func(_ context.Context, update PublicBookTicker) error {
		mu.Lock()
		updates = append(updates, update)
		mu.Unlock()
		return nil
	}, func(event BookTickerLifecycleEvent) {
		mu.Lock()
		events = append(events, event)
		mu.Unlock()
	})

	mu.Lock()
	defer mu.Unlock()
	if len(events) == 0 || events[0].ShardSessionID == "" {
		t.Fatalf("events = %+v, want a connected event with a non-empty session id", events)
	}
	if len(updates) == 0 {
		t.Fatal("no book ticker update was consumed within the test window")
	}
	if updates[0].Symbol != "BTCUSDT" {
		t.Fatalf("updates[0] = %+v", updates[0])
	}
}

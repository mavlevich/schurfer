package binance

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func itoa(v int64) string {
	return strconv.FormatInt(v, 10)
}

func TestHandleTradePayloadNormalizesValidRowsAndDerivesSide(t *testing.T) {
	now := time.Now()
	payload := []byte(`{
		"stream": "btcusdt@aggTrade",
		"data": {"e":"aggTrade","E":` + itoa(now.UnixMilli()) + `,"s":"btcusdt","a":123,"p":"65000.5","q":"0.01","T":` + itoa(now.UnixMilli()) + `,"m":true}
	}`)
	var got []PublicTrade
	err := handleTradePayload(context.Background(), payload, now, func(_ context.Context, trade PublicTrade) error {
		got = append(got, trade)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("got %d trades, want 1", len(got))
	}
	trade := got[0]
	if trade.Symbol != "BTCUSDT" {
		t.Fatalf("Symbol = %q, want normalized upper-case", trade.Symbol)
	}
	if trade.AggTradeID != "123" {
		t.Fatalf("AggTradeID = %q", trade.AggTradeID)
	}
	if trade.Side != "sell" {
		t.Fatalf("Side = %q, want sell when buyer is maker (m=true)", trade.Side)
	}
	if trade.Price != 65000.5 || trade.Size != 0.01 {
		t.Fatalf("Price/Size = %v/%v", trade.Price, trade.Size)
	}
}

func TestHandleTradePayloadDerivesBuySideWhenBuyerIsNotMaker(t *testing.T) {
	now := time.Now()
	payload := []byte(`{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","E":1,"s":"BTCUSDT","a":1,"p":"1","q":"1","T":` + itoa(now.UnixMilli()) + `,"m":false}}`)
	var got []PublicTrade
	err := handleTradePayload(context.Background(), payload, now, func(_ context.Context, trade PublicTrade) error {
		got = append(got, trade)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Side != "buy" {
		t.Fatalf("got = %+v, want a single buy-side trade", got)
	}
}

func TestHandleTradePayloadIgnoresNonAggTradeFrames(t *testing.T) {
	payload := []byte(`{"stream":"btcusdt@markPrice","data":{"e":"markPriceUpdate","s":"BTCUSDT"}}`)
	called := false
	err := handleTradePayload(context.Background(), payload, time.Now(), func(context.Context, PublicTrade) error {
		called = true
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if called {
		t.Fatal("consume was called for a non-aggTrade frame")
	}
}

func TestHandleTradePayloadSkipsFutureTimestamp(t *testing.T) {
	receivedAt := time.Now()
	future := receivedAt.Add(time.Hour).UnixMilli()
	payload := []byte(`{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","E":1,"s":"BTCUSDT","a":1,"p":"1","q":"1","T":` + itoa(future) + `,"m":false}}`)
	called := false
	err := handleTradePayload(context.Background(), payload, receivedAt, func(context.Context, PublicTrade) error {
		called = true
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if called {
		t.Fatal("consume was called for a future-skewed trade timestamp")
	}
}

func TestHandleTradePayloadSkipsInvalidPriceOrSize(t *testing.T) {
	receivedAt := time.Now()
	payload := []byte(`{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","E":1,"s":"BTCUSDT","a":1,"p":"not-a-number","q":"1","T":` + itoa(receivedAt.UnixMilli()) + `,"m":false}}`)
	called := false
	err := handleTradePayload(context.Background(), payload, receivedAt, func(context.Context, PublicTrade) error {
		called = true
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if called {
		t.Fatal("consume was called for an unparseable price")
	}
}

func TestHandleTradePayloadPropagatesConsumerFailure(t *testing.T) {
	receivedAt := time.Now()
	payload := []byte(`{"stream":"btcusdt@aggTrade","data":{"e":"aggTrade","E":1,"s":"BTCUSDT","a":1,"p":"1","q":"1","T":` + itoa(receivedAt.UnixMilli()) + `,"m":false}}`)
	wantErr := errors.New("boom")
	err := handleTradePayload(context.Background(), payload, receivedAt, func(context.Context, PublicTrade) error {
		return wantErr
	})
	if !errors.Is(err, wantErr) {
		t.Fatalf("err = %v, want %v", err, wantErr)
	}
}

func TestHandleTradePayloadRejectsMalformedJSON(t *testing.T) {
	err := handleTradePayload(context.Background(), []byte("not json"), time.Now(), func(context.Context, PublicTrade) error {
		return nil
	})
	if err == nil {
		t.Fatal("expected a decode error")
	}
}

// tradeDataWebSocketServer upgrades exactly one connection and pushes one
// aggTrade combined-stream frame for symbol before blocking on further
// reads, mirroring bybit's own test WS harness shape.
func tradeDataWebSocketServer(t *testing.T, symbol string, aggTradeID int64) string {
	t.Helper()
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		frame := map[string]any{
			"stream": strings.ToLower(symbol) + "@aggTrade",
			"data": map[string]any{
				"e": "aggTrade", "E": time.Now().UnixMilli(), "s": symbol,
				"a": aggTradeID, "p": "65000.5", "q": "0.01", "T": time.Now().UnixMilli(), "m": false,
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

func testSource(url string) *Source {
	return &Source{wsBaseURL: url, httpClient: &http.Client{Timeout: 2 * time.Second}}
}

func TestTradeStreamFiresConnectedLifecycleEventAndDeliversATrade(t *testing.T) {
	t.Parallel()
	serverURL := tradeDataWebSocketServer(t, "BTCUSDT", 1)
	source := testSource(serverURL)
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	var mu sync.Mutex
	var events []TradeLifecycleEvent
	var trades []PublicTrade
	_ = source.tradeStream(ctx, []string{"BTCUSDT"}, func(_ context.Context, trade PublicTrade) error {
		mu.Lock()
		trades = append(trades, trade)
		mu.Unlock()
		return nil
	}, func(event TradeLifecycleEvent) {
		mu.Lock()
		events = append(events, event)
		mu.Unlock()
	})

	mu.Lock()
	defer mu.Unlock()
	if len(events) == 0 || events[0].ShardSessionID == "" {
		t.Fatalf("events = %+v, want a connected event with a non-empty session id", events)
	}
	if len(trades) == 0 {
		t.Fatal("no trade was consumed within the test window")
	}
	if trades[0].Symbol != "BTCUSDT" {
		t.Fatalf("trades[0] = %+v", trades[0])
	}
}

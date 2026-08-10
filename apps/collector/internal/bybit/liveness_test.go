package bybit

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestTickerStreamReconnectsAfterHalfOpenReadTimeout(t *testing.T) {
	t.Parallel()
	serverURL, reconnected := halfOpenWebSocketServer(t)
	source := testSource(serverURL)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		source.streamLoop(ctx, []string{"BTCUSDT"}, func(context.Context, TickerEvent) error {
			return nil
		})
	}()

	waitForReconnect(t, reconnected, cancel, done)
	stats := source.StreamStats()
	if stats.TickerReconnectTotal < 1 || stats.TickerReadTimeoutTotal < 1 {
		t.Fatalf("ticker stream stats = %+v, want a read-timeout reconnect", stats)
	}
}

func TestTradeStreamReconnectsAfterHalfOpenReadTimeout(t *testing.T) {
	t.Parallel()
	serverURL, reconnected := halfOpenWebSocketServer(t)
	source := testSource(serverURL)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		source.tradeStreamLoop(ctx, []string{"BTCUSDT"}, func(context.Context, PublicTrade) error {
			return nil
		}, func(TradeLifecycleEvent) {})
	}()

	waitForReconnect(t, reconnected, cancel, done)
	stats := source.StreamStats()
	if stats.TradeReconnectTotal < 1 || stats.TradeReadTimeoutTotal < 1 {
		t.Fatalf("trade stream stats = %+v, want a read-timeout reconnect", stats)
	}
}

func TestTradeStreamRenewsReadDeadlineOnControlFrames(t *testing.T) {
	t.Parallel()
	serverURL := renewingPongWebSocketServer(t)
	source := &Source{streamConfig: streamConfig{
		URL:            serverURL,
		PingInterval:   20 * time.Millisecond,
		ReadTimeout:    80 * time.Millisecond,
		ReconnectDelay: 5 * time.Millisecond,
	}}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	startedAt := time.Now()
	err := source.tradeStream(ctx, []string{"BTCUSDT"}, func(context.Context, PublicTrade) error {
		return nil
	}, func(TradeLifecycleEvent) {})
	if !isReadTimeout(err) {
		t.Fatalf("trade stream error = %v, want read timeout", err)
	}
	if elapsed := time.Since(startedAt); elapsed < 180*time.Millisecond {
		t.Fatalf("stream timed out after %s; control pongs did not renew the read deadline", elapsed)
	}
}

func testSource(url string) *Source {
	return &Source{streamConfig: streamConfig{
		URL:            url,
		PingInterval:   10 * time.Millisecond,
		ReadTimeout:    50 * time.Millisecond,
		ReconnectDelay: 5 * time.Millisecond,
	}}
}

func halfOpenWebSocketServer(t *testing.T) (string, <-chan struct{}) {
	t.Helper()
	var connections atomic.Int64
	reconnected := make(chan struct{})
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		connection, err := upgrader.Upgrade(writer, request, nil)
		if err != nil {
			return
		}
		defer func() { _ = connection.Close() }()
		if connections.Add(1) == 2 {
			close(reconnected)
		}
		if _, _, err := connection.ReadMessage(); err != nil {
			return
		}
		if err := connection.WriteJSON(map[string]any{"op": "subscribe", "success": true}); err != nil {
			return
		}
		// Read client pings but deliberately never answer. The TCP connection stays
		// open, reproducing a peer that is reachable but no longer sends frames.
		for {
			if _, _, err := connection.ReadMessage(); err != nil {
				return
			}
		}
	}))
	t.Cleanup(server.Close)
	return "ws" + strings.TrimPrefix(server.URL, "http"), reconnected
}

func renewingPongWebSocketServer(t *testing.T) string {
	t.Helper()
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		connection, err := upgrader.Upgrade(writer, request, nil)
		if err != nil {
			return
		}
		defer func() { _ = connection.Close() }()
		if _, _, err := connection.ReadMessage(); err != nil {
			return
		}
		if err := connection.WriteJSON(map[string]any{"op": "subscribe", "success": true}); err != nil {
			return
		}
		for range 3 {
			time.Sleep(50 * time.Millisecond)
			if err := connection.WriteControl(
				websocket.PongMessage,
				[]byte("alive"),
				time.Now().Add(50*time.Millisecond),
			); err != nil {
				return
			}
		}
		for {
			if _, _, err := connection.ReadMessage(); err != nil {
				return
			}
		}
	}))
	t.Cleanup(server.Close)
	return "ws" + strings.TrimPrefix(server.URL, "http")
}

func waitForReconnect(
	t *testing.T,
	reconnected <-chan struct{},
	cancel context.CancelFunc,
	done <-chan struct{},
) {
	t.Helper()
	select {
	case <-reconnected:
		cancel()
	case <-time.After(time.Second):
		cancel()
		t.Fatal("stream did not reconnect after half-open timeout")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("stream did not stop after cancellation")
	}
}

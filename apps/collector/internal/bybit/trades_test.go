package bybit

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

func TestHandleTradePayloadNormalizesValidRowsAndSkipsInvalidRows(t *testing.T) {
	t.Parallel()
	receivedAt := time.UnixMilli(1_700_000_001_000)
	payload := []byte(`{
		"topic":"publicTrade.BTCUSDT",
		"data":[
			{"T":1700000000000,"s":"BTCUSDT","S":"Buy","v":"0.5","p":"100","i":"a"},
			{"T":1700000000100,"s":"BTCUSDT","S":"Sell","v":"0.2","p":"101","i":"b"},
			{"T":1700000000200,"s":"BTCUSDT","S":"Other","v":"1","p":"102","i":"bad"}
		]
	}`)

	var trades []PublicTrade
	err := handleTradePayload(
		context.Background(),
		payload,
		receivedAt,
		func(_ context.Context, trade PublicTrade) error {
			trades = append(trades, trade)
			return nil
		},
	)
	if err != nil {
		t.Fatalf("handle trade payload: %v", err)
	}
	if len(trades) != 2 {
		t.Fatalf("expected 2 trades, got %d", len(trades))
	}
	if trades[0].Side != "buy" || trades[0].Price != 100 || trades[0].Size != 0.5 {
		t.Fatalf("unexpected first trade: %#v", trades[0])
	}
	if trades[1].Side != "sell" || trades[1].TradeID != "b" {
		t.Fatalf("unexpected second trade: %#v", trades[1])
	}
	if !trades[0].ReceivedAt.Equal(receivedAt) {
		t.Fatalf("received timestamp mismatch: %s", trades[0].ReceivedAt)
	}
}

func TestHandleTradePayloadRejectsSubscriptionNack(t *testing.T) {
	t.Parallel()
	err := handleTradePayload(
		context.Background(),
		[]byte(`{"op":"subscribe","success":false}`),
		time.Now(),
		func(context.Context, PublicTrade) error { return nil },
	)
	if err == nil {
		t.Fatal("expected subscription nack error")
	}
}

func TestHandleTradePayloadRejectsMalformedJSON(t *testing.T) {
	t.Parallel()
	err := handleTradePayload(
		context.Background(),
		[]byte(`{"topic":`),
		time.Now(),
		func(context.Context, PublicTrade) error { return nil },
	)
	if err == nil {
		t.Fatal("expected malformed payload error")
	}
}

func TestHandleTradePayloadSkipsFutureTimestamp(t *testing.T) {
	t.Parallel()
	receivedAt := time.UnixMilli(1_700_000_000_000)
	called := false
	err := handleTradePayload(
		context.Background(),
		[]byte(`{"topic":"publicTrade.BTCUSDT","data":[{"T":1700000010000,"s":"BTCUSDT","S":"Buy","v":"1","p":"10","i":"a"}]}`),
		receivedAt,
		func(context.Context, PublicTrade) error {
			called = true
			return nil
		},
	)
	if err != nil {
		t.Fatalf("handle future payload: %v", err)
	}
	if called {
		t.Fatal("future trade reached consumer")
	}
}

func TestHandleTradePayloadPropagatesConsumerFailure(t *testing.T) {
	t.Parallel()
	expected := errors.New("queue closed")
	err := handleTradePayload(
		context.Background(),
		[]byte(`{"topic":"publicTrade.ETHUSDT","data":[{"T":1700000000000,"s":"ETHUSDT","S":"Buy","v":"1","p":"10","i":"a"}]}`),
		time.Now(),
		func(context.Context, PublicTrade) error { return expected },
	)
	if !errors.Is(err, expected) {
		t.Fatalf("expected %v, got %v", expected, err)
	}
}

func TestHandleTradePayloadDecodesBlockAndRPIAndSeq(t *testing.T) {
	t.Parallel()
	payload := []byte(`{
		"topic":"publicTrade.BTCUSDT",
		"data":[
			{"T":1700000000000,"s":"BTCUSDT","S":"Buy","v":"0.5","p":"100","i":"a","BT":true,"RPI":false,"seq":42},
			{"T":1700000000100,"s":"BTCUSDT","S":"Sell","v":"0.2","p":"101","i":"b","BT":false,"RPI":true,"seq":43},
			{"T":1700000000200,"s":"BTCUSDT","S":"Buy","v":"0.1","p":"102","i":"c"}
		]
	}`)
	var trades []PublicTrade
	err := handleTradePayload(
		context.Background(),
		payload,
		time.UnixMilli(1_700_000_001_000),
		func(_ context.Context, trade PublicTrade) error {
			trades = append(trades, trade)
			return nil
		},
	)
	if err != nil {
		t.Fatalf("handle trade payload: %v", err)
	}
	if len(trades) != 3 {
		t.Fatalf("expected 3 trades, got %d", len(trades))
	}
	if !trades[0].BlockTrade || trades[0].RPI || trades[0].Seq != 42 {
		t.Fatalf("unexpected block trade decode: %#v", trades[0])
	}
	if trades[1].BlockTrade || !trades[1].RPI || trades[1].Seq != 43 {
		t.Fatalf("unexpected RPI trade decode: %#v", trades[1])
	}
	if trades[2].BlockTrade || trades[2].RPI || trades[2].Seq != 0 {
		t.Fatalf("a trade with neither flag nor seq present must default to false/0: %#v", trades[2])
	}
}

// --- per-shard lifecycle events ---

func TestTradeStreamFiresConnectedLifecycleEventOnce(t *testing.T) {
	t.Parallel()
	serverURL := renewingPongWebSocketServer(t)
	source := &Source{streamConfig: streamConfig{
		URL: serverURL, PingInterval: 20 * time.Millisecond,
		ReadTimeout: 500 * time.Millisecond, ReconnectDelay: 5 * time.Millisecond,
	}}
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()

	var events []TradeLifecycleEvent
	var mu sync.Mutex
	err := source.tradeStream(ctx, []string{"BTCUSDT", "ETHUSDT"}, func(context.Context, PublicTrade) error {
		return nil
	}, func(event TradeLifecycleEvent) {
		mu.Lock()
		defer mu.Unlock()
		events = append(events, event)
	})
	if err != nil && !errors.Is(err, context.DeadlineExceeded) && !isReadTimeout(err) {
		t.Fatalf("unexpected error: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(events) == 0 {
		t.Fatal("expected at least a connected event")
	}
	connected := events[0]
	if connected.ShardSessionID == "" {
		t.Fatal("connected event must carry a non-empty session id")
	}
	if len(connected.Symbols) != 2 || connected.Symbols[0] != "BTCUSDT" {
		t.Fatalf("connected event symbols = %v, want the shard's own symbols", connected.Symbols)
	}
	if connected.ConnectedAt.IsZero() {
		t.Fatal("connected event must carry a non-zero ConnectedAt")
	}
	if !connected.DisconnectedAt.IsZero() {
		t.Fatal("a connected event must have a zero DisconnectedAt")
	}
}

func TestTradeStreamFiresDisconnectedLifecycleEventWithReasonAndSameSession(t *testing.T) {
	t.Parallel()
	serverURL, reconnected := halfOpenWebSocketServer(t)
	source := testSource(serverURL)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	var events []TradeLifecycleEvent
	var mu sync.Mutex
	done := make(chan struct{})
	go func() {
		defer close(done)
		source.tradeStreamLoop(ctx, []string{"BTCUSDT"}, func(context.Context, PublicTrade) error {
			return nil
		}, func(event TradeLifecycleEvent) {
			mu.Lock()
			defer mu.Unlock()
			events = append(events, event)
		})
	}()

	waitForReconnect(t, reconnected, cancel, done)

	mu.Lock()
	defer mu.Unlock()
	if len(events) < 2 {
		t.Fatalf("expected at least a connected and a disconnected event, got %d", len(events))
	}
	connected, disconnected := events[0], events[1]
	if disconnected.DisconnectedAt.IsZero() {
		t.Fatal("disconnected event must carry a non-zero DisconnectedAt")
	}
	if disconnected.Reason == "" {
		t.Fatal("disconnected event must carry a non-empty reason")
	}
	if !disconnected.ReadTimeout {
		t.Fatal("this server reproduces a read timeout; ReadTimeout must be true")
	}
	if disconnected.ShardSessionID != connected.ShardSessionID {
		t.Fatalf(
			"disconnected event session id %q must match the connected event's %q for the same connection attempt",
			disconnected.ShardSessionID, connected.ShardSessionID,
		)
	}
}

func TestTradeStreamGeneratesAFreshSessionIDPerReconnect(t *testing.T) {
	t.Parallel()
	serverURL, reconnected := halfOpenWebSocketServer(t)
	source := testSource(serverURL)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	var connectedSessions []string
	var mu sync.Mutex
	done := make(chan struct{})
	go func() {
		defer close(done)
		source.tradeStreamLoop(ctx, []string{"BTCUSDT"}, func(context.Context, PublicTrade) error {
			return nil
		}, func(event TradeLifecycleEvent) {
			if event.DisconnectedAt.IsZero() {
				mu.Lock()
				connectedSessions = append(connectedSessions, event.ShardSessionID)
				mu.Unlock()
			}
		})
	}()

	waitForReconnect(t, reconnected, cancel, done)

	mu.Lock()
	defer mu.Unlock()
	if len(connectedSessions) < 2 {
		t.Fatalf("expected at least 2 connected events across the reconnect, got %d", len(connectedSessions))
	}
	if connectedSessions[0] == connectedSessions[1] {
		t.Fatalf("each connection attempt must get its own session id, got the same one twice: %q", connectedSessions[0])
	}
}

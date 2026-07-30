package bybit

import (
	"context"
	"errors"
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

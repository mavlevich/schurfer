package binance

import (
	"context"
	"errors"
	"math"
	"strconv"
	"testing"
	"time"
)

func TestHandleMarkPricePayloadNormalizesValidUpdate(t *testing.T) {
	t.Parallel()
	receivedAt := time.Date(2026, 8, 25, 10, 0, 1, 0, time.UTC)
	payload := []byte(`{"stream":"btcusdt@markPrice@1s","data":{"e":"markPriceUpdate","E":1787642400000,"s":"btcusdt","p":"65123.40","i":"65100.10","P":"65090","r":"-0.000125","T":1787654400000}}`)
	var got PublicMarkPrice
	called := false
	err := handleMarkPricePayload(context.Background(), payload, receivedAt, func(_ context.Context, message MarkPriceMessage) error {
		called = true
		if message.Update == nil || message.Invalid != nil {
			t.Fatalf("message = %+v, want one valid update", message)
		}
		got = *message.Update
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if !called {
		t.Fatal("consume was not called")
	}
	if got.Symbol != "BTCUSDT" || got.MarkPrice != 65123.40 || got.IndexPrice != 65100.10 || got.FundingRate != -0.000125 {
		t.Fatalf("unexpected update: %+v", got)
	}
	if !got.ReceivedAt.Equal(receivedAt) || got.NextFundingAt.UnixMilli() != 1787654400000 {
		t.Fatalf("unexpected timestamps: %+v", got)
	}
}

func TestHandleMarkPricePayloadReportsMalformedMarketValues(t *testing.T) {
	t.Parallel()
	cases := []string{
		`{"data":{"e":"markPriceUpdate","E":1,"s":"BTCUSDT","p":"0","i":"1","r":"0","T":2}}`,
		`{"data":{"e":"markPriceUpdate","E":1,"s":"BTCUSDT","p":"1","i":"NaN","r":"0","T":2}}`,
		`{"data":{"e":"markPriceUpdate","E":1,"s":"BTCUSDT","p":"1","i":"1","r":"Inf","T":2}}`,
		`{"data":{"e":"markPriceUpdate","E":0,"s":"BTCUSDT","p":"1","i":"1","r":"0","T":2}}`,
	}
	for _, payload := range cases {
		called := false
		if err := handleMarkPricePayload(context.Background(), []byte(payload), time.Now(), func(_ context.Context, message MarkPriceMessage) error {
			called = true
			if message.Invalid == nil || message.Update != nil {
				t.Fatalf("message = %+v, want one invalid observation", message)
			}
			return nil
		}); err != nil {
			t.Fatal(err)
		}
		if !called {
			t.Fatalf("invalid payload disappeared before accounting: %s", payload)
		}
	}
}

func TestHandleMarkPricePayloadReportsFutureSkewAsInvalid(t *testing.T) {
	t.Parallel()
	receivedAt := time.UnixMilli(1_000)
	future := receivedAt.Add(maxMarkPriceFutureSkew + time.Millisecond).UnixMilli()
	payload := []byte(`{"data":{"e":"markPriceUpdate","E":` + strconv.FormatInt(future, 10) + `,"s":"BTCUSDT","p":"1","i":"1","r":"0","T":9999999999999}}`)
	called := false
	if err := handleMarkPricePayload(context.Background(), payload, receivedAt, func(_ context.Context, message MarkPriceMessage) error {
		called = true
		if message.Invalid == nil || message.Update != nil {
			t.Fatalf("message = %+v, want invalid future-skew observation", message)
		}
		if !message.Invalid.EventAt.IsZero() || !message.Invalid.ReceivedAt.Equal(receivedAt) {
			t.Fatalf("future exchange timestamp remained trusted: %+v", message.Invalid)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if !called {
		t.Fatal("future-skewed event disappeared before accounting")
	}
}

func TestHandleMarkPricePayloadIgnoresUnrelatedEventType(t *testing.T) {
	t.Parallel()
	called := false
	payload := []byte(`{"data":{"e":"bookTicker","E":1,"s":"BTCUSDT"}}`)
	if err := handleMarkPricePayload(context.Background(), payload, time.Now(), func(context.Context, MarkPriceMessage) error {
		called = true
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if called {
		t.Fatal("an unrelated websocket event must not be classified as invalid mark-price data")
	}
}

func TestHandleMarkPricePayloadPropagatesConsumerFailure(t *testing.T) {
	t.Parallel()
	want := errors.New("queue full")
	payload := []byte(`{"data":{"e":"markPriceUpdate","E":1,"s":"BTCUSDT","p":"1","i":"1","r":"0","T":2}}`)
	err := handleMarkPricePayload(context.Background(), payload, time.Now(), func(context.Context, MarkPriceMessage) error { return want })
	if !errors.Is(err, want) {
		t.Fatalf("err = %v, want %v", err, want)
	}
}

func TestMarkPriceStreamUsesRoutedMarketEndpointAndDefaultThreeSecondCadence(t *testing.T) {
	t.Parallel()
	got := NewSource().markPriceStreamURL([]string{"BTCUSDT", "ETHUSDT"})
	want := "wss://fstream.binance.com/market/stream?streams=btcusdt@markPrice/ethusdt@markPrice"
	if got != want {
		t.Fatalf("URL = %q, want %q", got, want)
	}
}

func TestFinitePositiveRejectsNonFinite(t *testing.T) {
	t.Parallel()
	if finitePositive(0) || finitePositive(math.NaN()) || finitePositive(math.Inf(1)) || !finitePositive(1) {
		t.Fatal("finitePositive classification is wrong")
	}
}

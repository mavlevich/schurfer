package main

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/bybit"
	"github.com/mavlevich/schurfer/collector/internal/hotset"
)

func TestParseTicker(t *testing.T) {
	t.Parallel()
	last := "1.25"
	bid := "1.24"
	ask := "1.26"
	volume := "100"
	turnover := "125"
	raw, err := json.Marshal(bybit.TickerEvent{
		SchemaVersion: 1,
		Source:        "bybit",
		Symbol:        "AKEUSDT",
		TS:            1_000,
		LastPrice:     &last,
		Bid:           &bid,
		Ask:           &ask,
		Volume24h:     &volume,
		Turnover24h:   &turnover,
	})
	if err != nil {
		t.Fatal(err)
	}
	received := time.UnixMilli(1_020)
	tick, err := parseTicker(raw, received)
	if err != nil {
		t.Fatal(err)
	}
	if tick.Symbol != "AKEUSDT" || tick.LastPrice != 1.25 || !tick.EventAt.Equal(time.UnixMilli(1_000)) {
		t.Fatalf("unexpected tick: %+v", tick)
	}
}

func TestParseTickerRejectsMissingPriceAndTimestamp(t *testing.T) {
	t.Parallel()
	raw, err := json.Marshal(bybit.TickerEvent{Symbol: "AKEUSDT"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := parseTicker(raw, time.Now()); err == nil {
		t.Fatal("invalid ticker unexpectedly accepted")
	}
}

func TestParseTickerRejectsUnknownSchema(t *testing.T) {
	t.Parallel()
	last := "1"
	raw, err := json.Marshal(bybit.TickerEvent{
		SchemaVersion: 2,
		Symbol:        "AKEUSDT",
		TS:            1_000,
		LastPrice:     &last,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := parseTicker(raw, time.Now()); err == nil {
		t.Fatal("unknown schema unexpectedly accepted")
	}
}

func TestParseTickerRejectsWrongSourceAndFutureTimestamp(t *testing.T) {
	t.Parallel()
	last := "1"
	received := time.UnixMilli(1_000)
	for _, event := range []bybit.TickerEvent{
		{SchemaVersion: 1, Source: "binance", Symbol: "AKEUSDT", TS: 1_000, LastPrice: &last},
		{
			SchemaVersion: 1,
			Source:        "bybit",
			Symbol:        "AKEUSDT",
			TS:            received.Add(maxFutureSkew + time.Millisecond).UnixMilli(),
			LastPrice:     &last,
		},
	} {
		raw, err := json.Marshal(event)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := parseTicker(raw, received); err == nil {
			t.Fatalf("invalid event unexpectedly accepted: %+v", event)
		}
	}
}

func TestEnqueueIsBounded(t *testing.T) {
	t.Parallel()
	pending := make([]hotset.Bar, maxPendingBars)
	pending, dropped := enqueue(pending, []hotset.Bar{{}, {}}, 0)
	if len(pending) != maxPendingBars || dropped != 2 {
		t.Fatalf("queue = (%d, %d), want (%d, 2)", len(pending), dropped, maxPendingBars)
	}
}

func TestPersistRetryBacksOffAndResets(t *testing.T) {
	t.Parallel()
	now := time.Unix(1_000, 0).UTC()
	var retry persistRetryState
	if !retry.ready(now) {
		t.Fatal("new retry state should allow an attempt")
	}

	if delay := retry.failed(now); delay != persistRetryMin {
		t.Fatalf("first retry delay = %s, want %s", delay, persistRetryMin)
	}
	if retry.ready(now.Add(persistRetryMin - time.Millisecond)) {
		t.Fatal("retry became ready before the backoff elapsed")
	}
	if !retry.ready(now.Add(persistRetryMin)) {
		t.Fatal("retry did not become ready after the backoff elapsed")
	}

	attemptAt := now.Add(persistRetryMin)
	if delay := retry.failed(attemptAt); delay != 2*persistRetryMin {
		t.Fatalf("second retry delay = %s, want %s", delay, 2*persistRetryMin)
	}
	for range 10 {
		attemptAt = retry.nextAttempt
		retry.failed(attemptAt)
	}
	if retry.backoff != persistRetryMax {
		t.Fatalf("retry backoff = %s, want cap %s", retry.backoff, persistRetryMax)
	}

	retry.succeeded()
	if retry.backoff != 0 || !retry.nextAttempt.IsZero() || !retry.ready(now) {
		t.Fatalf("successful retry did not reset state: %+v", retry)
	}
}

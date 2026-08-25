package binance

import (
	"context"
	"testing"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/liquidationcapture"
)

func TestHandleLiquidationPayloadMapsSellOrderToLiquidatedLong(t *testing.T) {
	source := NewSource()
	payload := []byte(`{"stream":"!forceOrder@arr","data":{"e":"forceOrder","E":1568014460893,"o":{"s":"BTCUSDT","S":"SELL","o":"LIMIT","f":"IOC","q":"0.014","p":"9910","ap":"9910","X":"FILLED","l":"0.014","z":"0.014","T":1568014460893},"ps":"BTCUSDT","st":1}}`)
	allowed := map[string]struct{}{"BTCUSDT": {}}
	var got liquidationcapture.Event
	err := source.handleLiquidationPayload(context.Background(), payload, time.UnixMilli(1568014461000),
		"session", "universe", allowed, func(_ context.Context, event liquidationcapture.Event) error {
			got = event
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	if got.PositionSide != liquidationcapture.PositionLong {
		t.Fatalf("position side = %q, want long (SELL force-order closes a long)", got.PositionSide)
	}
	if got.CoverageKind != liquidationcapture.CoverageLatestPerSymbol1000ms {
		t.Fatalf("coverage = %q", got.CoverageKind)
	}
	if got.SourceContractVariant != "binance_merged_um_v1" {
		t.Fatalf("source contract variant = %q", got.SourceContractVariant)
	}
	if got.EstimatedLiquidationNotional == nil || *got.EstimatedLiquidationNotional != 138.74 {
		t.Fatalf("estimated notional = %v", got.EstimatedLiquidationNotional)
	}
}

func TestHandleLiquidationPayloadAcceptsLegacyUSDMPayloadOnlyThroughAllowlist(t *testing.T) {
	source := NewSource()
	payload := []byte(`{"stream":"!forceOrder@arr","data":{"e":"forceOrder","E":1568014460893,"o":{"s":"BTCUSDT","S":"BUY","o":"LIMIT","f":"IOC","q":"0.014","p":"9910","ap":"9910","X":"FILLED","l":"0.014","z":"0.014","T":1568014460893}}}`)
	allowed := map[string]struct{}{"BTCUSDT": {}}
	var got liquidationcapture.Event
	err := source.handleLiquidationPayload(context.Background(), payload, time.UnixMilli(1568014461000),
		"session", "universe", allowed, func(_ context.Context, event liquidationcapture.Event) error {
			got = event
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	if got.PositionSide != liquidationcapture.PositionShort {
		t.Fatalf("position side = %q, want short", got.PositionSide)
	}
	if got.SourceContractVariant != "binance_usdm_no_scope_tag_v1" {
		t.Fatalf("source contract variant = %q", got.SourceContractVariant)
	}
	if source.Stats().ScopeTagMissingAcceptedTotal != 1 {
		t.Fatalf("scope-tag-missing accepted = %d, want 1", source.Stats().ScopeTagMissingAcceptedTotal)
	}

	called := false
	err = source.handleLiquidationPayload(context.Background(), payload, time.UnixMilli(1568014461000),
		"session", "universe", map[string]struct{}{}, func(context.Context, liquidationcapture.Event) error {
			called = true
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	if called || source.Stats().EventsOutOfScopeTotal != 1 {
		t.Fatalf("outside allowlist called=%v stats=%+v", called, source.Stats())
	}
}

func TestHandleLiquidationPayloadRejectsMergedCoinMarginedEvent(t *testing.T) {
	source := NewSource()
	payload := []byte(`{"stream":"!forceOrder@arr","data":{"e":"forceOrder","E":1000,"o":{"s":"BTCUSD_PERP","S":"SELL","q":"1","p":"1","ap":"1","l":"1","z":"1","T":1000},"st":2}}`)
	called := false
	err := source.handleLiquidationPayload(context.Background(), payload, time.UnixMilli(1100),
		"session", "universe", map[string]struct{}{"BTCUSD_PERP": {}}, func(context.Context, liquidationcapture.Event) error {
			called = true
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	if called || source.Stats().EventsOutOfScopeTotal != 1 {
		t.Fatalf("CM event called=%v stats=%+v", called, source.Stats())
	}
}

func TestHandleLiquidationPayloadRejectsUnknownScopeTagAsInvalid(t *testing.T) {
	source := NewSource()
	payload := []byte(`{"stream":"!forceOrder@arr","data":{"e":"forceOrder","E":1000,"o":{"s":"BTCUSDT","S":"SELL","q":"1","p":"1","ap":"1","l":"1","z":"1","T":1000},"st":9}}`)
	called := false
	err := source.handleLiquidationPayload(context.Background(), payload, time.UnixMilli(1100),
		"session", "universe", map[string]struct{}{"BTCUSDT": {}}, func(context.Context, liquidationcapture.Event) error {
			called = true
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	if called || source.Stats().EventsInvalidTotal != 1 {
		t.Fatalf("unknown scope tag called=%v stats=%+v", called, source.Stats())
	}
}

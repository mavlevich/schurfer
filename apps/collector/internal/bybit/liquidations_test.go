package bybit

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/mavlevich/schurfer/collector/internal/liquidationcapture"
)

func TestHandleLiquidationPayloadMapsDocumentedPositionSideAndBankruptcyPrice(t *testing.T) {
	source := NewSource()
	receivedAt := time.UnixMilli(1739502303300)
	payload := []byte(`{"topic":"allLiquidation.ROSEUSDT","type":"snapshot","ts":1739502303204,"data":[{"T":1739502302929,"s":"ROSEUSDT","S":"Buy","v":"20000","p":"0.04499"}]}`)
	var got liquidationcapture.Event
	err := source.handleLiquidationPayload(context.Background(), payload, receivedAt, "session-a", "universe",
		func(_ context.Context, event liquidationcapture.Event) error { got = event; return nil })
	if err != nil {
		t.Fatal(err)
	}
	if got.PositionSide != liquidationcapture.PositionLong {
		t.Fatalf("position side = %q, want long (Bybit Buy means liquidated long)", got.PositionSide)
	}
	if got.BankruptcyPrice == nil || *got.BankruptcyPrice != 0.04499 {
		t.Fatalf("bankruptcy price = %v", got.BankruptcyPrice)
	}
	if got.EstimatedLiquidationNotional == nil || math.Abs(*got.EstimatedLiquidationNotional-899.8) > 1e-9 {
		t.Fatalf("estimated notional = %v", got.EstimatedLiquidationNotional)
	}
	if got.CoverageKind != liquidationcapture.CoverageCompleteStream {
		t.Fatalf("coverage = %q", got.CoverageKind)
	}
}

func TestHandleLiquidationPayloadReplayKeepsKeyAcrossSession(t *testing.T) {
	source := NewSource()
	payload := []byte(`{"topic":"allLiquidation.BTCUSDT","ts":1000,"data":[{"T":999,"s":"BTCUSDT","S":"Sell","v":"1","p":"100"}]}`)
	received := time.UnixMilli(1100)
	var keys [][32]byte
	for _, session := range []string{"one", "two"} {
		if err := source.handleLiquidationPayload(context.Background(), payload, received, session, "universe",
			func(_ context.Context, event liquidationcapture.Event) error {
				keys = append(keys, event.SourceEventKey)
				return nil
			}); err != nil {
			t.Fatal(err)
		}
	}
	if len(keys) != 2 || keys[0] != keys[1] {
		t.Fatalf("replay keys = %v, want identical", keys)
	}
}

func TestHandleLiquidationPayloadRebatchedEventDoesNotClaimNativeIdentity(t *testing.T) {
	source := NewSource()
	payloads := [][]byte{
		[]byte(`{"topic":"allLiquidation.BTCUSDT","ts":1000,"data":[{"T":999,"s":"BTCUSDT","S":"Sell","v":"1","p":"100"}]}`),
		[]byte(`{"topic":"allLiquidation.BTCUSDT","ts":1001,"data":[{"T":999,"s":"BTCUSDT","S":"Sell","v":"1","p":"100"}]}`),
	}
	var keys [][32]byte
	for _, payload := range payloads {
		if err := source.handleLiquidationPayload(context.Background(), payload, time.UnixMilli(1100),
			"session", "universe", func(_ context.Context, event liquidationcapture.Event) error {
				keys = append(keys, event.SourceEventKey)
				return nil
			}); err != nil {
			t.Fatal(err)
		}
	}
	if len(keys) != 2 || keys[0] == keys[1] {
		t.Fatal("re-batched events must remain distinct without a venue-native liquidation event id")
	}
}

func TestLiquidationStreamReportsConnectedOnlyAfterEverySubscriptionAck(t *testing.T) {
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	requestsReady := make(chan struct{})
	releaseAcks := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		connection, err := upgrader.Upgrade(writer, request, nil)
		if err != nil {
			return
		}
		defer func() { _ = connection.Close() }()
		requestIDs := make([]string, 0, 2)
		for range 2 {
			_, payload, err := connection.ReadMessage()
			if err != nil {
				return
			}
			var subscription struct {
				RequestID string `json:"req_id"`
			}
			if err := json.Unmarshal(payload, &subscription); err != nil || subscription.RequestID == "" {
				return
			}
			requestIDs = append(requestIDs, subscription.RequestID)
		}
		close(requestsReady)
		<-releaseAcks
		for _, requestID := range requestIDs {
			if err := connection.WriteJSON(map[string]any{
				"op": "subscribe", "success": true, "req_id": requestID,
			}); err != nil {
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

	source := &Source{streamConfig: streamConfig{
		URL:          "ws" + strings.TrimPrefix(server.URL, "http"),
		PingInterval: time.Second, ReadTimeout: 2 * time.Second,
		ReconnectDelay: 5 * time.Millisecond,
	}}
	symbols := make([]string, 11)
	for index := range symbols {
		symbols[index] = fmt.Sprintf("S%dUSDT", index)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	lifecycle := make(chan liquidationcapture.LifecycleEvent, 1)
	done := make(chan error, 1)
	go func() {
		done <- source.liquidationStream(ctx, symbols, "universe", func(
			context.Context, liquidationcapture.Event,
		) error {
			return nil
		}, func(event liquidationcapture.LifecycleEvent) {
			lifecycle <- event
		})
	}()

	select {
	case <-requestsReady:
	case <-time.After(time.Second):
		t.Fatal("server did not receive every subscription batch")
	}
	select {
	case event := <-lifecycle:
		t.Fatalf("connection reported ready before subscription ACKs: %+v", event)
	default:
	}
	close(releaseAcks)
	select {
	case event := <-lifecycle:
		if event.ConnectedAt.IsZero() || !event.DisconnectedAt.IsZero() {
			t.Fatalf("unexpected ready lifecycle event: %+v", event)
		}
	case <-time.After(time.Second):
		t.Fatal("connection was not reported ready after every subscription ACK")
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("stream shutdown failed: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("stream did not stop after cancellation")
	}
}

package bybit

import (
	"context"
	"crypto/rand"
	"errors"
	"testing"
	"time"
)

func TestTickerStateProducesVersionedCompleteEvent(t *testing.T) {
	t.Parallel()
	state := tickerState{}
	state.merge(tickerFields{
		Symbol:      "AKEUSDT",
		LastPrice:   "0.01",
		Price24hPct: "0.25",
		Bid:         "0.0099",
		Ask:         "0.0101",
	}, 1_000, 1_000)
	event := state.toEvent(1_000, 1_000, "snapshot", nil, 0, "session-a")
	if event.SchemaVersion != 1 {
		t.Fatalf("schema version = %d, want 1", event.SchemaVersion)
	}
	if event.Source != "bybit" || event.Symbol != "AKEUSDT" || event.TS != 1_000 {
		t.Fatalf("unexpected event identity: %+v", event)
	}
	if event.LastPrice == nil || *event.LastPrice != "0.01" {
		t.Fatalf("last price = %v, want 0.01", event.LastPrice)
	}
	if event.Volume24h != nil {
		t.Fatalf("missing delta field should remain nil: %+v", event.Volume24h)
	}
	if event.OpenInterest != nil || event.OpenInterestValue != nil {
		t.Fatalf("missing OI fields should remain nil: %+v", event)
	}
	if event.OpenInterestEventAtMs != nil || event.OpenInterestObservedAtMs != nil {
		t.Fatalf("missing OI should have no event-at/observed-at: %+v", event)
	}
	if event.StreamSessionID != "session-a" {
		t.Fatalf("stream session id = %q, want session-a", event.StreamSessionID)
	}
}

func TestTickerStateMergesOpenInterestFieldsWithEventAndObservedAt(t *testing.T) {
	t.Parallel()
	state := tickerState{}
	// eventAtMs (Bybit's own ts) and receivedAtMs (this collector's wall
	// clock) are deliberately different here to prove they are tracked
	// independently, not conflated into one timestamp.
	state.merge(tickerFields{
		Symbol:            "AKEUSDT",
		OpenInterest:      "1000000",
		OpenInterestValue: "12345.67",
	}, 4_000, 5_000)
	event := state.toEvent(4_000, 5_000, "snapshot", nil, 0, "session-a")
	if event.OpenInterest == nil || *event.OpenInterest != "1000000" {
		t.Fatalf("open interest = %v, want 1000000", event.OpenInterest)
	}
	if event.OpenInterestEventAtMs == nil || *event.OpenInterestEventAtMs != 4_000 {
		t.Fatalf("open interest event at = %v, want 4000 (Bybit's own ts)", event.OpenInterestEventAtMs)
	}
	if event.OpenInterestObservedAtMs == nil || *event.OpenInterestObservedAtMs != 5_000 {
		t.Fatalf("open interest observed at = %v, want 5000 (collector receive time)", event.OpenInterestObservedAtMs)
	}
	if event.OpenInterestValueEventAtMs == nil || *event.OpenInterestValueEventAtMs != 4_000 {
		t.Fatalf("open interest value event at = %v, want 4000", event.OpenInterestValueEventAtMs)
	}
	if event.OpenInterestValueObservedAtMs == nil || *event.OpenInterestValueObservedAtMs != 5_000 {
		t.Fatalf("open interest value observed at = %v, want 5000", event.OpenInterestValueObservedAtMs)
	}
}

func TestTickerStatePreservesOpenInterestAcrossDeltasMissingItButKeepsOldTimestamps(t *testing.T) {
	t.Parallel()
	state := tickerState{}
	state.merge(tickerFields{Symbol: "AKEUSDT", OpenInterest: "1000000", OpenInterestValue: "12345.67"}, 4_000, 5_000)
	// A later delta that omits OI (e.g. only price changed) must not erase
	// the last known value or its timestamps: merge is snapshot+delta, not
	// replace. The new message's own ts (9000) must NOT overwrite the OI
	// event-at, which stays pinned to the message that actually carried it.
	state.merge(tickerFields{Symbol: "AKEUSDT", LastPrice: "0.02"}, 9_000, 9_500)
	event := state.toEvent(9_000, 9_500, "delta", nil, 0, "session-a")
	if event.OpenInterest == nil || *event.OpenInterest != "1000000" {
		t.Fatalf("open interest should survive an unrelated delta: %v", event.OpenInterest)
	}
	if event.OpenInterestEventAtMs == nil || *event.OpenInterestEventAtMs != 4_000 {
		t.Fatalf(
			"OI event-at must stay at the message that actually carried OI, got %v (current message ts is 9000)",
			event.OpenInterestEventAtMs,
		)
	}
	if event.OpenInterestObservedAtMs == nil || *event.OpenInterestObservedAtMs != 5_000 {
		t.Fatalf(
			"OI observed-at must stay at the message that actually carried OI, got %v",
			event.OpenInterestObservedAtMs,
		)
	}
}

func TestTickerEventCarriesEnvelopeAndReconnectDiagnostics(t *testing.T) {
	t.Parallel()
	state := tickerState{}
	state.merge(tickerFields{Symbol: "AKEUSDT", LastPrice: "0.01"}, 1_000, 1_000)
	cs := int64(300633424)
	event := state.toEvent(1_000, 1_234, "snapshot", &cs, 3, "session-xyz")
	if event.ReceivedAtMs != 1_234 {
		t.Fatalf("received at ms = %d, want 1234", event.ReceivedAtMs)
	}
	if event.MessageType != "snapshot" {
		t.Fatalf("message type = %q, want snapshot", event.MessageType)
	}
	if event.CrossSequence == nil || *event.CrossSequence != cs {
		t.Fatalf("cross sequence = %v, want %d", event.CrossSequence, cs)
	}
	if event.ReconnectEpoch != 3 {
		t.Fatalf("reconnect epoch = %d, want 3", event.ReconnectEpoch)
	}
	if event.StreamSessionID != "session-xyz" {
		t.Fatalf("stream session id = %q, want session-xyz", event.StreamSessionID)
	}
}

func TestNewStreamSessionIDChangesEachCall(t *testing.T) {
	t.Parallel()
	a, err := newStreamSessionID(rand.Reader)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	b, err := newStreamSessionID(rand.Reader)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if a == "" || b == "" {
		t.Fatal("stream session id must not be empty")
	}
	if a == b {
		t.Fatalf("two calls produced the same session id: %q", a)
	}
}

// failingReader always errors, letting the fail-closed path be exercised
// without swapping the package-level crypto/rand.Reader.
type failingReader struct{}

func (failingReader) Read([]byte) (int, error) {
	return 0, errors.New("entropy source unavailable")
}

func TestNewStreamSessionIDFailsClosedOnReadError(t *testing.T) {
	t.Parallel()
	id, err := newStreamSessionID(failingReader{})
	if err == nil {
		t.Fatal("expected an error when the entropy source fails")
	}
	if id != "" {
		t.Fatalf("expected an empty id on failure, got %q", id)
	}
}

// --- handleTickerFrame: real raw-JSON decode path, not just direct merge ---

func TestHandleTickerFrameDecodesRealSnapshotPayload(t *testing.T) {
	t.Parallel()
	frame := []byte(`{
		"topic": "tickers.AKEUSDT",
		"type": "snapshot",
		"cs": 300633424,
		"ts": 1700000000000,
		"data": {
			"symbol": "AKEUSDT",
			"lastPrice": "0.01234",
			"openInterest": "1000000",
			"openInterestValue": "12345.67",
			"bid1Price": "0.01230",
			"ask1Price": "0.01235"
		}
	}`)
	state := make(map[string]tickerState)
	receivedAt := time.UnixMilli(1_700_000_000_500)

	var events []TickerEvent
	err := handleTickerFrame(context.Background(), frame, receivedAt, 0, "session-a", state,
		func(_ context.Context, event TickerEvent) error {
			events = append(events, event)
			return nil
		},
	)
	if err != nil {
		t.Fatalf("handle ticker frame: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}
	event := events[0]
	if event.Symbol != "AKEUSDT" || event.TS != 1_700_000_000_000 {
		t.Fatalf("unexpected identity: %+v", event)
	}
	if event.MessageType != "snapshot" {
		t.Fatalf("message type = %q, want snapshot", event.MessageType)
	}
	if event.CrossSequence == nil || *event.CrossSequence != 300633424 {
		t.Fatalf("cross sequence = %v, want 300633424", event.CrossSequence)
	}
	if event.ReceivedAtMs != receivedAt.UnixMilli() {
		t.Fatalf("received at ms = %d, want %d", event.ReceivedAtMs, receivedAt.UnixMilli())
	}
	if event.OpenInterest == nil || *event.OpenInterest != "1000000" {
		t.Fatalf("open interest = %v, want 1000000", event.OpenInterest)
	}
	if event.OpenInterestEventAtMs == nil || *event.OpenInterestEventAtMs != 1_700_000_000_000 {
		t.Fatalf("open interest event at = %v, want the message ts", event.OpenInterestEventAtMs)
	}
	if event.OpenInterestObservedAtMs == nil || *event.OpenInterestObservedAtMs != receivedAt.UnixMilli() {
		t.Fatalf("open interest observed at = %v, want %d", event.OpenInterestObservedAtMs, receivedAt.UnixMilli())
	}
	if event.StreamSessionID != "session-a" {
		t.Fatalf("stream session id = %q, want session-a", event.StreamSessionID)
	}
}

func TestHandleTickerFrameDeltaOmittingOIKeepsPriorValueAndTimestamps(t *testing.T) {
	t.Parallel()
	state := make(map[string]tickerState)
	snapshot := []byte(`{
		"topic": "tickers.AKEUSDT", "type": "snapshot", "cs": 1, "ts": 1000,
		"data": {"symbol": "AKEUSDT", "openInterest": "1000000", "openInterestValue": "12345.67"}
	}`)
	delta := []byte(`{
		"topic": "tickers.AKEUSDT", "type": "delta", "cs": 1, "ts": 2000,
		"data": {"symbol": "AKEUSDT", "lastPrice": "0.02"}
	}`)
	var events []TickerEvent
	consume := func(_ context.Context, event TickerEvent) error {
		events = append(events, event)
		return nil
	}
	err := handleTickerFrame(context.Background(), snapshot, time.UnixMilli(1_000), 0, "session-a", state, consume)
	if err != nil {
		t.Fatalf("snapshot: %v", err)
	}
	err = handleTickerFrame(context.Background(), delta, time.UnixMilli(2_000), 0, "session-a", state, consume)
	if err != nil {
		t.Fatalf("delta: %v", err)
	}
	if len(events) != 2 {
		t.Fatalf("expected 2 events, got %d", len(events))
	}
	deltaEvent := events[1]
	if deltaEvent.MessageType != "delta" {
		t.Fatalf("message type = %q, want delta", deltaEvent.MessageType)
	}
	if deltaEvent.TS != 2_000 {
		t.Fatalf("delta event ts = %d, want 2000", deltaEvent.TS)
	}
	if deltaEvent.OpenInterest == nil || *deltaEvent.OpenInterest != "1000000" {
		t.Fatalf("delta should still carry the last known OI: %v", deltaEvent.OpenInterest)
	}
	if deltaEvent.OpenInterestEventAtMs == nil || *deltaEvent.OpenInterestEventAtMs != 1_000 {
		t.Fatalf(
			"OI event-at must stay pinned to the snapshot's own ts (1000), not the delta's ts (2000), got %v",
			deltaEvent.OpenInterestEventAtMs,
		)
	}
	if deltaEvent.OpenInterestObservedAtMs == nil || *deltaEvent.OpenInterestObservedAtMs != 1_000 {
		t.Fatalf(
			"OI observed-at must stay at the snapshot's receive time, got %v",
			deltaEvent.OpenInterestObservedAtMs,
		)
	}
}

func TestHandleTickerFrameRejectsSubscriptionNack(t *testing.T) {
	t.Parallel()
	frame := []byte(`{"op": "subscribe", "success": false, "ret_msg": "nope"}`)
	err := handleTickerFrame(context.Background(), frame, time.Now(), 0, "session-a", map[string]tickerState{},
		func(context.Context, TickerEvent) error { return nil },
	)
	if err == nil {
		t.Fatal("expected an error for a subscribe nack")
	}
}

func TestHandleTickerFrameSkipsMalformedJSONWithoutError(t *testing.T) {
	t.Parallel()
	err := handleTickerFrame(context.Background(), []byte("not json"), time.Now(), 0, "session-a", map[string]tickerState{},
		func(context.Context, TickerEvent) error { return nil },
	)
	if err != nil {
		t.Fatalf("malformed frames must be skipped, not fatal: %v", err)
	}
}

func TestHandleTickerFrameIgnoresNonTickerTopics(t *testing.T) {
	t.Parallel()
	frame := []byte(`{"topic": "orderbook.AKEUSDT", "data": {}}`)
	var called bool
	err := handleTickerFrame(context.Background(), frame, time.Now(), 0, "session-a", map[string]tickerState{},
		func(context.Context, TickerEvent) error { called = true; return nil },
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if called {
		t.Fatal("publish should not be called for a non-ticker topic")
	}
}

// --- reconnect episode resets OI state, not price/bid/ask ---

func TestResetOpenInterestStateClearsOnlyOIFields(t *testing.T) {
	t.Parallel()
	state := map[string]tickerState{
		"AKEUSDT": {
			Symbol:                   "AKEUSDT",
			LastPrice:                "0.01",
			Bid:                      "0.0099",
			OpenInterest:             "1000000",
			OpenInterestEventAtMs:    4_000,
			OpenInterestObservedAtMs: 5_000,
		},
	}
	resetOpenInterestState(state)
	st := state["AKEUSDT"]
	if st.LastPrice != "0.01" || st.Bid != "0.0099" {
		t.Fatalf("price/bid must survive a reconnect reset: %+v", st)
	}
	if st.OpenInterest != "" || st.OpenInterestEventAtMs != 0 || st.OpenInterestObservedAtMs != 0 {
		t.Fatalf("OI state must be fully cleared on reconnect: %+v", st)
	}
}

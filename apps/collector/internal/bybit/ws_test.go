package bybit

import "testing"

func TestTickerStateProducesVersionedCompleteEvent(t *testing.T) {
	t.Parallel()
	state := tickerState{}
	state.merge(tickerFields{
		Symbol:      "AKEUSDT",
		LastPrice:   "0.01",
		Price24hPct: "0.25",
		Bid:         "0.0099",
		Ask:         "0.0101",
	})
	event := state.toEvent(1_000)
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
}

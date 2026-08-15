package binance

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/momentumsource"
)

func TestTranslateTradePreservesEnvelopeAndFields(t *testing.T) {
	eventAt := time.UnixMilli(1_700_000_000_000)
	receivedAt := eventAt.Add(50 * time.Millisecond)
	trade := PublicTrade{
		Symbol:     "BTCUSDT",
		AggTradeID: "12345",
		Side:       "sell",
		EventAt:    eventAt,
		ReceivedAt: receivedAt,
		Price:      65000.5,
		Size:       0.01,
	}

	got := translateTrade(trade, "session-1")

	want := momentumsource.Trade{
		Envelope: momentumsource.Envelope{
			Exchange:       "binance",
			MarketType:     MarketType,
			NativeMarketID: "BTCUSDT",
			EventAt:        eventAt,
			ReceivedAt:     receivedAt,
			SessionID:      "session-1",
		},
		TradeID: "12345",
		Side:    "sell",
		Price:   65000.5,
		Size:    0.01,
	}
	if got != want {
		t.Fatalf("translateTrade() = %+v, want %+v", got, want)
	}
}

func TestTranslateUniverseMapsExclusionCountsAndValidates(t *testing.T) {
	catalog := SymbolCatalog{
		CryptoPerpetualSymbols: []string{"BTCUSDT", "ETHUSDT"},
		Counts: SymbolCatalogCounts{
			CatalogItemsTotal:            6,
			CryptoPerpetualsIncluded:     2,
			NonPerpetualContractExcluded: 1,
			NonTradingExcluded:           1,
			NonUSDTExcluded:              1,
			UnderlyingIndexExcluded:      1,
		},
	}

	got := translateUniverse(catalog)

	if err := got.Validate(); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
	if got.Exchange != "binance" || got.MarketType != MarketType {
		t.Fatalf("translateUniverse() venue identity = %q/%q", got.Exchange, got.MarketType)
	}
	if got.ExclusionCounts["non_perpetual_contract"] != 1 || got.ExclusionCounts["underlying_index"] != 1 {
		t.Fatalf("ExclusionCounts = %+v", got.ExclusionCounts)
	}
}

func TestAdapterFetchUniverseUsesTheSameStrictCryptoPerpetualCatalog(t *testing.T) {
	t.Parallel()
	items := []map[string]any{
		instrument("BTCUSDT", "PERPETUAL", "TRADING", "USDT", "USDT", "COIN"),
		instrument("XAUUSDT", "TRADIFI_PERPETUAL", "TRADING", "USDT", "USDT", "COIN"),
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeExchangeInfoResponse(t, w, items)
	}))
	t.Cleanup(server.Close)

	adapter := NewAdapter(&Source{restURL: server.URL, httpClient: server.Client()})
	snapshot, err := adapter.FetchUniverse(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if err := snapshot.Validate(); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
	if len(snapshot.IncludedSymbols) != 1 || snapshot.IncludedSymbols[0] != "BTCUSDT" {
		t.Fatalf("IncludedSymbols = %v, want [BTCUSDT]", snapshot.IncludedSymbols)
	}
	if snapshot.ExclusionCounts["non_perpetual_contract"] != 1 {
		t.Fatalf("ExclusionCounts = %+v, want non_perpetual_contract=1", snapshot.ExclusionCounts)
	}
}

func TestAdapterStreamTradesThreadsTheShardsSessionIDOntoEachTrade(t *testing.T) {
	t.Parallel()
	serverURL := tradeDataWebSocketServer(t, "BTCUSDT", 1)
	adapter := NewAdapter(testSource(serverURL))
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	tradeCh := make(chan momentumsource.Trade, 1)
	err := adapter.StreamTrades(ctx, []string{"BTCUSDT"}, func(_ context.Context, trade momentumsource.Trade) error {
		select {
		case tradeCh <- trade:
		default:
		}
		return nil
	})
	if err != nil && ctx.Err() == nil {
		t.Fatalf("StreamTrades() error = %v", err)
	}

	select {
	case trade := <-tradeCh:
		if trade.SessionID == "" {
			t.Fatal("translated Trade.SessionID is empty, want the shard's own session id")
		}
		if trade.NativeMarketID != "BTCUSDT" {
			t.Fatalf("translated Trade = %+v", trade)
		}
		if trade.Exchange != "binance" || trade.MarketType != MarketType {
			t.Fatalf("translated Trade venue identity = %q/%q", trade.Exchange, trade.MarketType)
		}
	default:
		t.Fatal("no trade was consumed within the test window")
	}
}

func TestAdapterStreamTradesThreadsSessionIDDespiteSymbolCaseMismatch(t *testing.T) {
	t.Parallel()
	serverURL := tradeDataWebSocketServer(t, "btcusdt", 2)
	adapter := NewAdapter(testSource(serverURL))
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()

	tradeCh := make(chan momentumsource.Trade, 1)
	_ = adapter.StreamTrades(ctx, []string{"btcusdt"}, func(_ context.Context, trade momentumsource.Trade) error {
		select {
		case tradeCh <- trade:
		default:
		}
		return nil
	})

	select {
	case trade := <-tradeCh:
		if trade.SessionID == "" {
			t.Fatal("translated Trade.SessionID is empty: session lookup missed on a symbol case mismatch")
		}
	default:
		t.Fatal("no trade was consumed within the test window")
	}
}

func TestAdapterStreamOpenInterestPopulatesAmountOnlyWithCorrectProvenance(t *testing.T) {
	t.Parallel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"symbol":"BTCUSDT","openInterest":"1000.5","time":1786737418938}`))
	}))
	t.Cleanup(server.Close)

	adapter := NewAdapter(&Source{restURL: server.URL, httpClient: server.Client()})
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()

	readingCh := make(chan momentumsource.OpenInterestReading, 1)
	envelopeCh := make(chan momentumsource.Envelope, 1)
	_ = adapter.StreamOpenInterest(ctx, []string{"BTCUSDT"}, func(_ context.Context, envelope momentumsource.Envelope, reading momentumsource.OpenInterestReading) error {
		select {
		case readingCh <- reading:
			envelopeCh <- envelope
		default:
		}
		return nil
	})

	select {
	case reading := <-readingCh:
		envelope := <-envelopeCh
		if envelope.Exchange != "binance" || envelope.MarketType != MarketType || envelope.NativeMarketID != "BTCUSDT" {
			t.Fatalf("envelope = %+v", envelope)
		}
		if reading.AmountProvenance != momentumsource.ProvenanceNative {
			t.Fatalf("AmountProvenance = %q, want native", reading.AmountProvenance)
		}
		if reading.Amount == nil || *reading.Amount != "1000.5" {
			t.Fatalf("Amount = %v, want 1000.5", reading.Amount)
		}
		// Regression: this endpoint has no value field at all -- Value and
		// ValueProvenance must stay the zero value, never stamped "native"
		// for a field that was never observed (same rule bybit.Adapter's
		// own provenanceIfPresent enforces for Bybit's partial OI deltas).
		if reading.Value != nil {
			t.Fatalf("Value = %v, want nil: this endpoint has no value field", reading.Value)
		}
		if reading.ValueProvenance != "" {
			t.Fatalf("ValueProvenance = %q, want the zero value", reading.ValueProvenance)
		}
	default:
		t.Fatal("no open interest reading was consumed within the test window")
	}
}

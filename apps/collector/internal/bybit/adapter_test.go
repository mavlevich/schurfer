package bybit

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"github.com/mavlevich/schurfer/collector/internal/momentumsource"
)

func TestTranslateTradePreservesEnvelopeAndFields(t *testing.T) {
	eventAt := time.UnixMilli(1_700_000_000_000)
	receivedAt := eventAt.Add(50 * time.Millisecond)
	trade := PublicTrade{
		Symbol:     "BTCUSDT",
		TradeID:    "abc123",
		Side:       "buy",
		EventAt:    eventAt,
		ReceivedAt: receivedAt,
		Price:      65000.5,
		Size:       0.01,
	}

	got := translateTrade(trade, "session-1")

	want := momentumsource.Trade{
		Envelope: momentumsource.Envelope{
			Exchange:       "bybit",
			MarketType:     MarketType,
			NativeMarketID: "BTCUSDT",
			EventAt:        eventAt,
			ReceivedAt:     receivedAt,
			SessionID:      "session-1",
		},
		TradeID: "abc123",
		Side:    "buy",
		Price:   65000.5,
		Size:    0.01,
	}
	if got != want {
		t.Fatalf("translateTrade() = %+v, want %+v", got, want)
	}
}

func TestTranslateTickerEmbedsOpenInterestWhenPresent(t *testing.T) {
	oi := "1000.5"
	oiEventAt := int64(1_700_000_000_000)
	oiObservedAt := int64(1_700_000_000_100)
	price := "65000.5"
	event := TickerEvent{
		Symbol:                   "BTCUSDT",
		TS:                       1_700_000_000_200,
		ReceivedAtMs:             1_700_000_000_250,
		LastPrice:                &price,
		OpenInterest:             &oi,
		OpenInterestEventAtMs:    &oiEventAt,
		OpenInterestObservedAtMs: &oiObservedAt,
		MessageType:              "delta",
		StreamSessionID:          "session-2",
	}

	got := translateTicker(event)

	if got.Exchange != "bybit" || got.MarketType != MarketType || got.NativeMarketID != "BTCUSDT" {
		t.Fatalf("translateTicker() envelope = %+v", got.Envelope)
	}
	if got.SessionID != "session-2" || got.MessageType != "delta" {
		t.Fatalf("translateTicker() session/messageType = %q/%q", got.SessionID, got.MessageType)
	}
	if got.LastPrice == nil || *got.LastPrice != price {
		t.Fatalf("translateTicker() LastPrice = %v, want %q", got.LastPrice, price)
	}
	if got.OpenInterest == nil {
		t.Fatal("translateTicker() OpenInterest = nil, want an embedded reading")
	}
	if got.OpenInterest.AmountProvenance != momentumsource.ProvenanceNative {
		t.Fatalf("OpenInterest.AmountProvenance = %q, want native", got.OpenInterest.AmountProvenance)
	}
	if got.OpenInterest.Amount == nil || *got.OpenInterest.Amount != oi {
		t.Fatalf("OpenInterest.Amount = %v, want %q", got.OpenInterest.Amount, oi)
	}
	if got.OpenInterest.AmountEventAt == nil || !got.OpenInterest.AmountEventAt.Equal(time.UnixMilli(oiEventAt)) {
		t.Fatalf("OpenInterest.AmountEventAt = %v, want %v", got.OpenInterest.AmountEventAt, time.UnixMilli(oiEventAt))
	}
}

func TestTranslateTickerLeavesOpenInterestNilWhenNeverObserved(t *testing.T) {
	// Regression: a freshly reconnected episode with no OI seen yet (see
	// ws.go's resetOpenInterestState) must translate to a nil OpenInterest,
	// never a zero-value reading that looks like "OI is 0".
	event := TickerEvent{Symbol: "BTCUSDT", TS: 1, ReceivedAtMs: 1}

	got := translateTicker(event)

	if got.OpenInterest != nil {
		t.Fatalf("OpenInterest = %+v, want nil when never observed", got.OpenInterest)
	}
}

func TestOpenInterestFromTickerOnlyStampsProvenanceForFieldsActuallyPresent(t *testing.T) {
	// Regression for the code-review finding: a delta can carry only the
	// amount, not the value (tickerState.merge refreshes each field
	// independently) -- ValueProvenance must stay the zero value, not
	// "native", when Value itself is nil.
	amount := "1000.5"
	event := TickerEvent{Symbol: "BTCUSDT", OpenInterest: &amount}

	reading, ok := OpenInterestFromTicker(event)
	if !ok {
		t.Fatal("OpenInterestFromTicker() ok = false, want true")
	}
	if reading.AmountProvenance != momentumsource.ProvenanceNative {
		t.Fatalf("AmountProvenance = %q, want native", reading.AmountProvenance)
	}
	if reading.Value != nil {
		t.Fatalf("Value = %v, want nil", reading.Value)
	}
	if reading.ValueProvenance != "" {
		t.Fatalf("ValueProvenance = %q, want the zero value since Value is nil", reading.ValueProvenance)
	}
}

func TestOpenInterestFromTickerReturnsFalseWhenNeitherFieldObserved(t *testing.T) {
	_, ok := OpenInterestFromTicker(TickerEvent{Symbol: "BTCUSDT"})
	if ok {
		t.Fatal("OpenInterestFromTicker() ok = true, want false with no amount or value observed")
	}
}

func TestTranslateUniverseMapsExclusionCountsAndValidates(t *testing.T) {
	catalog := SymbolCatalog{
		CryptoPerpetualSymbols: []string{"BTCUSDT", "ETHUSDT"},
		Counts: SymbolCatalogCounts{
			CatalogItemsTotal:           6,
			CryptoPerpetualsIncluded:    2,
			StandardCryptoIncluded:      2,
			DatedFuturesExcluded:        1,
			StockPerpetualsExcluded:     1,
			CommodityPerpetualsExcluded: 1,
			UnknownContractExcluded:     1,
		},
	}

	got := translateUniverse(catalog)

	if err := got.Validate(); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
	if got.Exchange != "bybit" || got.MarketType != MarketType {
		t.Fatalf("translateUniverse() venue identity = %q/%q", got.Exchange, got.MarketType)
	}
	if len(got.IncludedSymbols) != 2 {
		t.Fatalf("IncludedSymbols = %v", got.IncludedSymbols)
	}
	if got.ExclusionCounts["dated_future"] != 1 || got.ExclusionCounts["stock_perpetual"] != 1 ||
		got.ExclusionCounts["commodity_perpetual"] != 1 || got.ExclusionCounts["unknown_contract"] != 1 {
		t.Fatalf("ExclusionCounts = %+v", got.ExclusionCounts)
	}
}

func TestAdapterFetchUniverseUsesTheSameStrictCryptoPerpetualCatalog(t *testing.T) {
	t.Parallel()
	items := []map[string]string{
		instrument("BTCUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", ""),
		instrument("AMCUSDT", "LinearPerpetual", "Trading", "USDT", "USDT", "stock"),
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeInstrumentResponse(t, w, items, "")
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
	if snapshot.ExclusionCounts["stock_perpetual"] != 1 {
		t.Fatalf("ExclusionCounts = %+v, want stock_perpetual=1", snapshot.ExclusionCounts)
	}
}

// tradeDataWebSocketServer upgrades exactly one connection, acks the
// subscribe, then pushes one publicTrade data frame for symbol before
// blocking on further reads (so the connection stays open until the test's
// own context is cancelled) -- enough to exercise StreamTrades' session
// threading end-to-end without a real Bybit connection.
func tradeDataWebSocketServer(t *testing.T, symbol string, tradeID string) string {
	t.Helper()
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		if _, _, err := conn.ReadMessage(); err != nil {
			return
		}
		if err := conn.WriteJSON(map[string]any{"op": "subscribe", "success": true}); err != nil {
			return
		}
		frame := map[string]any{
			"topic": "publicTrade." + symbol,
			"data": []map[string]any{
				{
					"T": time.Now().UnixMilli(),
					"s": symbol,
					"S": "Buy",
					"v": "0.01",
					"p": "65000.5",
					"i": tradeID,
				},
			},
		}
		if err := conn.WriteJSON(frame); err != nil {
			return
		}
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}))
	t.Cleanup(server.Close)
	return "ws" + strings.TrimPrefix(server.URL, "http")
}

func TestAdapterStreamTradesThreadsTheShardsSessionIDOntoEachTrade(t *testing.T) {
	t.Parallel()
	serverURL := tradeDataWebSocketServer(t, "BTCUSDT", "trade-1")
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
		if trade.NativeMarketID != "BTCUSDT" || trade.TradeID != "trade-1" {
			t.Fatalf("translated Trade = %+v", trade)
		}
		if trade.Exchange != "bybit" || trade.MarketType != MarketType {
			t.Fatalf("translated Trade venue identity = %q/%q", trade.Exchange, trade.MarketType)
		}
	default:
		t.Fatal("no trade was consumed within the test window")
	}
}

func TestAdapterStreamTradesThreadsSessionIDDespiteSymbolCaseMismatch(t *testing.T) {
	// Regression for the code-review finding: TradeLifecycleEvent.Symbols
	// preserves StreamTrades' own caller-supplied casing verbatim, but
	// handleTradePayload always upper-cases PublicTrade.Symbol from the
	// wire regardless of what case subscribed. A caller passing a
	// lower-case symbol must still get a non-empty SessionID, not a
	// silently mismatched map key.
	t.Parallel()
	serverURL := tradeDataWebSocketServer(t, "btcusdt", "trade-2")
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
		if trade.NativeMarketID != "BTCUSDT" {
			t.Fatalf("NativeMarketID = %q, want the wire-normalized upper-case form", trade.NativeMarketID)
		}
	default:
		t.Fatal("no trade was consumed within the test window")
	}
}

// tickerDataWebSocketServer upgrades exactly one connection, acks the
// subscribe, then pushes `count` ticker delta frames for symbol before
// blocking on further reads.
func tickerDataWebSocketServer(t *testing.T, symbol string, count int) string {
	t.Helper()
	upgrader := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		if _, _, err := conn.ReadMessage(); err != nil {
			return
		}
		if err := conn.WriteJSON(map[string]any{"op": "subscribe", "success": true}); err != nil {
			return
		}
		for i := 0; i < count; i++ {
			frame := map[string]any{
				"topic": "tickers." + symbol,
				"type":  "delta",
				"ts":    time.Now().UnixMilli(),
				"data":  map[string]any{"symbol": symbol, "lastPrice": "65000.5"},
			}
			if err := conn.WriteJSON(frame); err != nil {
				return
			}
			time.Sleep(5 * time.Millisecond)
		}
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}))
	t.Cleanup(server.Close)
	return "ws" + strings.TrimPrefix(server.URL, "http")
}

func TestAdapterStreamTickerSwallowsConsumerErrorsWithoutReconnecting(t *testing.T) {
	// Regression for the code-review finding: ws.go's own handleTicker only
	// logs a publish error and keeps the connection running -- it does not
	// propagate it the way handleTradePayload does for trades. A consumer
	// that always errors must still see every subsequent ticker message on
	// the SAME connection, not trigger a reconnect (which would generate a
	// fresh, different session id).
	t.Parallel()
	const messageCount = 3
	serverURL := tickerDataWebSocketServer(t, "BTCUSDT", messageCount)
	// A generous read timeout, unlike testSource()'s deliberately tight one:
	// this test isolates "does a consumer error itself cause a reconnect"
	// from ws.go's own unrelated ping/read-liveness reconnect path, which
	// this fake server (no pong handling) would otherwise trigger within
	// the same short window and confound the two.
	source := &Source{streamConfig: streamConfig{
		URL: serverURL, PingInterval: 20 * time.Millisecond,
		ReadTimeout: 5 * time.Second, ReconnectDelay: 5 * time.Millisecond,
	}}
	adapter := NewAdapter(source)
	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	var mu sync.Mutex
	var sessions []string
	_ = adapter.StreamTicker(ctx, []string{"BTCUSDT"}, func(_ context.Context, update momentumsource.TickerUpdate) error {
		mu.Lock()
		sessions = append(sessions, update.SessionID)
		mu.Unlock()
		return errors.New("simulated consumer failure")
	})

	mu.Lock()
	defer mu.Unlock()
	if len(sessions) < messageCount {
		t.Fatalf("received %d ticker updates, want at least %d despite every consume() call erroring", len(sessions), messageCount)
	}
	for _, session := range sessions {
		if session != sessions[0] {
			t.Fatalf("session ids = %v, want all messages on the SAME connection (no reconnect from a consumer error)", sessions)
		}
	}
}

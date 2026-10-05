package streamrt

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

const tradeFrame = `{"topic":"publicTrade.AAAUSDT","type":"snapshot","ts":1,"data":[` +
	`{"T":1790000000000,"s":"AAAUSDT","S":"Buy","v":"2","p":"1.5","i":"t1","seq":7},` +
	`{"T":1790000000001,"s":"AAAUSDT","S":"Sell","v":"0","p":"1.5","i":"bad"}]}`

func TestBybitCodecParsesTradesAndSkipsInvalidItems(t *testing.T) {
	got, err := BybitCodec{}.Parse([]byte(tradeFrame), time.UnixMilli(1790000000500))
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("trades %d", len(got))
	}
	trade := got[0]
	if trade.Symbol != "AAAUSDT" || trade.Side != "buy" || trade.Notional != 3 || trade.Seq != 7 {
		t.Fatalf("%+v", trade)
	}
	if none, err := (BybitCodec{}).Parse([]byte(`{"op":"pong","success":true}`), time.Now()); err != nil || none != nil {
		t.Fatalf("pong: %v %v", none, err)
	}
	if _, err := (BybitCodec{}).Parse([]byte(`{"op":"subscribe","success":false,"ret_msg":"x"}`), time.Now()); err == nil {
		t.Fatal("a refused subscription must end the session")
	}
}

func TestBybitCodecRejectsTradesFromTheFuture(t *testing.T) {
	got, _ := BybitCodec{}.Parse([]byte(tradeFrame), time.UnixMilli(1790000000000-10_000))
	if len(got) != 0 {
		t.Fatalf("a trade 10 s ahead of the receive time was accepted: %+v", got)
	}
}

func TestBybitSubscribeFramesChunkTopics(t *testing.T) {
	symbols := make([]string, 25)
	for i := range symbols {
		symbols[i] = "S" + string(rune('A'+i)) + "USDT"
	}
	frames, err := BybitCodec{}.SubscribeFrames(symbols)
	if err != nil || len(frames) != 3 {
		t.Fatalf("frames %d err %v", len(frames), err)
	}
	if !strings.Contains(string(frames[0]), `"publicTrade.SAUSDT"`) {
		t.Fatalf("%s", frames[0])
	}
}

// fakeVenue serves tradeFrame after each subscribe and drops the first session.
func fakeVenue(t *testing.T, dials *atomic.Int64, pings *atomic.Int64, trades int) *httptest.Server {
	t.Helper()
	upgrader := websocket.Upgrader{}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		n := dials.Add(1)
		if _, _, err := conn.ReadMessage(); err != nil { // the subscribe frame
			return
		}
		frame := strings.ReplaceAll(tradeFrame, "1790000000000", "1")
		for range trades {
			if conn.WriteMessage(websocket.TextMessage, []byte(frame)) != nil {
				return
			}
		}
		if n == 1 {
			return // drop the first session
		}
		for {
			_, msg, err := conn.ReadMessage()
			if err != nil {
				return
			}
			if strings.Contains(string(msg), "ping") {
				pings.Add(1)
			}
		}
	}))
}

type testCodec struct {
	BybitCodec
	ping time.Duration
}

func (c testCodec) PingInterval() time.Duration { return c.ping }

func TestRuntimeEmitsLifecycleInBandAndReconnects(t *testing.T) {
	var dials, pings atomic.Int64
	server := fakeVenue(t, &dials, &pings, 1)
	defer server.Close()
	codec := testCodec{BybitCodec{Endpoint: "ws" + strings.TrimPrefix(server.URL, "http")}, 50 * time.Millisecond}
	rt := New(codec, Config{BackoffInitial: 20 * time.Millisecond, BackoffMax: 50 * time.Millisecond})
	ctx, cancel := context.WithTimeout(context.Background(), 600*time.Millisecond)
	defer cancel()
	go func() { _ = rt.Run(ctx, []string{"AAAUSDT"}) }()
	var kinds []string
	sessions := map[string]bool{}
	for event := range rt.Events() {
		switch {
		case event.Lifecycle != nil:
			kinds = append(kinds, string(event.Lifecycle.Kind))
			sessions[event.Lifecycle.SessionID] = true
		case event.Trade != nil:
			kinds = append(kinds, "trade")
		}
	}
	got := strings.Join(kinds, ",")
	if !strings.HasPrefix(got, "connected,trade,disconnected,connected,trade") {
		t.Fatalf("event order %s", got)
	}
	if len(sessions) < 2 || dials.Load() < 2 || pings.Load() < 1 {
		t.Fatalf("sessions %d dials %d pings %d", len(sessions), dials.Load(), pings.Load())
	}
}

func TestOverflowIsReportedBeforeTheNextDeliveredEvent(t *testing.T) {
	rt := New(BybitCodec{}, Config{QueueSize: 2})
	s := &shard{index: 0, symbols: []string{"AAAUSDT"}}
	trade := func() Event { return Event{Trade: &Trade{Symbol: "AAAUSDT"}} }
	for range 5 {
		rt.emit(s, trade()) // 2 fit, 3 dropped
	}
	if rt.Stats.Dropped.Load() != 3 || s.owed != 3 {
		t.Fatalf("dropped %d owed %d", rt.Stats.Dropped.Load(), s.owed)
	}
	<-rt.events
	<-rt.events // the consumer catches up
	rt.emit(s, trade())
	first, second := <-rt.events, <-rt.events
	if first.Lifecycle == nil || first.Lifecycle.Kind != Overflow || first.Lifecycle.Dropped != 3 {
		t.Fatalf("expected an overflow report first, got %+v", first)
	}
	if second.Trade == nil || s.owed != 0 {
		t.Fatalf("expected the trade after the report, got %+v owed %d", second, s.owed)
	}
}

func TestRunRefusesAnEmptyUniverse(t *testing.T) {
	if err := New(BybitCodec{}, Config{}).Run(context.Background(), nil); err == nil {
		t.Fatal("an empty universe must be refused")
	}
}

func TestMexcCodecConvertsContractsToBaseUnits(t *testing.T) {
	codec := MexcCodec{ContractSizes: map[string]float64{"BTC_USDT": 0.0001}}
	frame := `{"channel":"push.deal","symbol":"BTC_USDT","ts":2,` +
		`"data":{"p":60000,"v":20,"T":2,"O":1,"M":2,"t":1790000000000,"i":42}}`
	got, err := codec.Parse([]byte(frame), time.UnixMilli(1790000000100))
	if err != nil || len(got) != 1 {
		t.Fatalf("trades %v err %v", got, err)
	}
	trade := got[0]
	if trade.Size != 0.002 || trade.Notional != 120 || trade.Side != "sell" || trade.TradeID != "42" {
		t.Fatalf("%+v", trade)
	}
	list := `{"channel":"push.deal","symbol":"BTC_USDT","data":[` +
		`{"p":1,"v":1,"T":1,"t":1},{"p":1,"v":1,"T":9,"t":1}]}`
	if got, _ := codec.Parse([]byte(list), time.UnixMilli(2)); len(got) != 1 {
		t.Fatalf("list payload: %v", got)
	}
}

func TestMexcCodecNeverGuessesAContractSize(t *testing.T) {
	codec := MexcCodec{ContractSizes: map[string]float64{"BTC_USDT": 0.0001}}
	if _, err := codec.SubscribeFrames([]string{"BTC_USDT", "NEW_USDT"}); err == nil {
		t.Fatal("a symbol without a contract size must be refused")
	}
	frame := `{"channel":"push.deal","symbol":"NEW_USDT","data":{"p":1,"v":1,"T":1,"t":1}}`
	if got, _ := codec.Parse([]byte(frame), time.Now()); got != nil {
		t.Fatalf("converted with a guessed size: %v", got)
	}
	if _, err := codec.Parse([]byte(`{"channel":"rs.error","data":"x"}`), time.Now()); err == nil {
		t.Fatal("rs.error must end the session")
	}
}

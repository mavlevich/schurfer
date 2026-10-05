package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestUniverseKeepsEnabledUSDTPerpetuals(t *testing.T) {
	body := `{"success":true,"data":[
		{"symbol":"BTC_USDT","state":0,"quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"futureType":1},
		{"symbol":"AAA_USDT","state":0,"quoteCoin":"USDT","settleCoin":"USDT","futureType":1},
		{"symbol":"BTC_USDT_241227","state":0,"quoteCoin":"USDT","settleCoin":"USDT","contractSize":1,"futureType":2},
		{"symbol":"NOTYPE_USDT","state":0,"quoteCoin":"USDT","settleCoin":"USDT","contractSize":1},
		{"symbol":"OLD_USDT","state":3,"quoteCoin":"USDT","settleCoin":"USDT","contractSize":1,"futureType":1},
		{"symbol":"BTC_USD","state":0,"quoteCoin":"USD","settleCoin":"BTC","contractSize":1,"futureType":1}]}`
	got, err := Universe([]byte(body))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(got.Symbols, ",") != "AAA_USDT,BTC_USDT" || got.MissingSize != 1 {
		t.Fatalf("got %v missing=%d", got.Symbols, got.MissingSize)
	}
	want := map[string]int{"not_perpetual": 1, "future_type_missing": 1, "not_enabled": 1, "not_usdt": 1}
	for reason, n := range want {
		if got.Excluded[reason] != n {
			t.Fatalf("excluded %v", got.Excluded)
		}
	}
	if _, err := Universe([]byte(`{"success":false}`)); err == nil {
		t.Fatal("a failed response must be an error")
	}
}

const (
	priceMarker  = "6866.53719"
	volumeMarker = "209671"
)

func frames(now time.Time) [][]byte {
	ms := now.UnixMilli()
	deal := `{"p":` + priceMarker + `,"v":` + volumeMarker + `,"T":1,"O":1,"M":2,"t":`
	return [][]byte{
		[]byte(`{"channel":"push.deal","data":` + deal + itoa(ms-40) + `},"symbol":"BTC_USDT","ts":` + itoa(ms-20) + `}`),
		[]byte(`{"channel":"push.deal","data":[` + deal + itoa(ms-50) + `},` + deal + itoa(ms-60) + `}],"symbol":"BTC_USDT","ts":` + itoa(ms-30) + `}`),
		[]byte(`{"channel":"push.deal","data":{"p":1,"v":2,"T":9,"t":3},"symbol":"X_USDT"}`),
		[]byte(`{"channel":"rs.sub.deal","data":"success"}`),
		[]byte(`{"channel":"pong","data":` + itoa(ms-5) + `}`),
		[]byte(`{"channel":"rs.error","code":1002,"data":"bad"}`),
		[]byte(`not json`),
	}
}

func itoa(v int64) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func TestObserveCountsShapesFieldsAndLags(t *testing.T) {
	stats := NewStats()
	now := time.Now()
	for _, f := range frames(now) {
		stats.Observe(f, now)
	}
	r := stats.Report(now.Add(-time.Minute), now, false, UniverseSnapshot{Symbols: []string{"a", "b"}}, 1, 50)
	if r.Trades != 3 || r.MalformedTrades != 1 {
		t.Fatalf("trades %d malformed %d", r.Trades, r.MalformedTrades)
	}
	if r.PayloadShape["object"] != 2 || r.PayloadShape["list"] != 1 {
		t.Fatalf("shape %v", r.PayloadShape)
	}
	if r.Acks != 1 || r.ErrorsByCode["1002"] != 1 || r.MessagesByChannel["unparseable"] != 1 {
		t.Fatalf("channels %v errors %v", r.MessagesByChannel, r.ErrorsByCode)
	}
	if r.FieldsSeen["O"] != 3 || r.FieldsSeen["M"] != 3 {
		t.Fatalf("fields %v", r.FieldsSeen)
	}
	if lag := r.LagMS["receive_minus_trade_time"]; lag["max"] != 60 || lag["n"] != 3 {
		t.Fatalf("lag %v", lag)
	}
}

func TestReportCarriesNoMarketValues(t *testing.T) {
	stats := NewStats()
	now := time.Now()
	for _, f := range frames(now) {
		stats.Observe(f, now)
	}
	body, err := json.Marshal(stats.Report(now.Add(-time.Minute), now, false, UniverseSnapshot{Symbols: []string{"a", "b"}}, 1, 50))
	if err != nil {
		t.Fatal(err)
	}
	for _, marker := range []string{priceMarker, volumeMarker, "BTC_USDT", `"buy"`, `"sell"`} {
		if strings.Contains(string(body), marker) {
			t.Fatalf("the report leaks %q", marker)
		}
	}
}

func TestReconnectsAreCountedAndPingsSent(t *testing.T) {
	var dials atomic.Int64
	var pings atomic.Int64
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		n := dials.Add(1)
		_, sub, _ := conn.ReadMessage()
		if !strings.Contains(string(sub), `"compress":false`) {
			return
		}
		_ = conn.WriteMessage(websocket.TextMessage, []byte(`{"channel":"rs.sub.deal","data":"success"}`))
		if n == 1 {
			return // drop the first session to force a reconnect
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
	defer server.Close()
	stats := NewStats()
	ctx, cancel := context.WithTimeout(context.Background(), 3500*time.Millisecond)
	defer cancel()
	url := "ws" + strings.TrimPrefix(server.URL, "http")
	runConnection(ctx, url, []string{"BTC_USDT"}, false, stats)
	r := stats.Report(time.Now().Add(-time.Minute), time.Now(), false, UniverseSnapshot{Symbols: []string{"a"}}, 1, 1)
	if dials.Load() < 2 || r.Reconnects < 1 || r.Acks < 2 {
		t.Fatalf("dials %d reconnects %d acks %d", dials.Load(), r.Reconnects, r.Acks)
	}
	if r.Sessions < 2 || r.SessionsFullyAcked != r.Sessions {
		t.Fatalf("sessions %d fully acked %d", r.Sessions, r.SessionsFullyAcked)
	}
}

func TestChunkShardsSymbols(t *testing.T) {
	got := chunk([]string{"a", "b", "c", "d", "e"}, 2)
	if len(got) != 3 || len(got[2]) != 1 {
		t.Fatalf("%v", got)
	}
}

func TestAcknowledgementsAreCountedPerSessionNotPooled(t *testing.T) {
	stats := NewStats()
	stats.SessionEnded(2, 3) // one instrument acknowledged twice, another maybe never
	stats.SessionEnded(2, 1)
	r := stats.Report(time.Now(), time.Now(), false, UniverseSnapshot{}, 2, 2)
	if r.Sessions != 2 || r.SessionsFullyAcked != 1 || r.ExcessAcks != 1 {
		t.Fatalf("sessions %d fully %d excess %d", r.Sessions, r.SessionsFullyAcked, r.ExcessAcks)
	}
}

func TestInstrumentsWithTradesAreCountedNotNamed(t *testing.T) {
	stats := NewStats()
	now := time.Now()
	for _, f := range frames(now) {
		stats.Observe(f, now)
	}
	r := stats.Report(now.Add(-time.Minute), now, false, UniverseSnapshot{}, 1, 1)
	if r.InstrumentsTrading != 2 {
		t.Fatalf("instruments %d", r.InstrumentsTrading)
	}
}

func TestReservoirKeepsEarlyValuesAsLikelyAsLateOnes(t *testing.T) {
	var r reservoir
	total := int64(4 * maxSamples)
	for i := range total {
		r.add(i)
	}
	early := 0
	for _, v := range r.values {
		if v < total/2 {
			early++
		}
	}
	share := float64(early) / float64(len(r.values))
	if r.seen != total || share < 0.47 || share > 0.53 {
		t.Fatalf("seen %d early share %.3f", r.seen, share)
	}
}

func TestPingRoundTripIsMeasuredFromThePingSent(t *testing.T) {
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer func() { _ = conn.Close() }()
		for {
			_, msg, err := conn.ReadMessage()
			if err != nil {
				return
			}
			if strings.Contains(string(msg), "ping") {
				time.Sleep(20 * time.Millisecond)
				_ = conn.WriteMessage(websocket.TextMessage, []byte(`{"channel":"pong","data":1}`))
			}
		}
	}))
	defer server.Close()
	pingInterval = 100 * time.Millisecond
	defer func() { pingInterval = 15 * time.Second }()
	stats := NewStats()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	url := "ws" + strings.TrimPrefix(server.URL, "http")
	runConnection(ctx, url, []string{"BTC_USDT"}, false, stats)
	rtt := stats.Report(time.Now(), time.Now(), false, UniverseSnapshot{}, 1, 1).LagMS["ping_round_trip"]
	if rtt["n"] < 1 || rtt["p50"] < 20 {
		t.Fatalf("rtt %v", rtt)
	}
}

package burstprobe

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/burstengine"
)

const (
	symbolMarker = "ZZZMARKERUSDT"
	priceMarker  = "12345.678"
	returnMarker = 0.0731
)

type fakeMarket struct {
	mu      sync.Mutex
	books   int
	settled int
	fail    bool
}

func (m *fakeMarket) Book(_ context.Context, symbol string) (BookSnapshot, error) {
	m.mu.Lock()
	m.books++
	m.mu.Unlock()
	if m.fail {
		return BookSnapshot{}, errors.New("down")
	}
	now := time.Now()
	return BookSnapshot{
		Symbol: symbol, Bids: [][2]string{{priceMarker, "1"}}, Asks: [][2]string{{priceMarker, "2"}},
		ExchangeTS: now.UnixMilli(), RequestedAt: now, ReceivedAt: now.Add(30 * time.Millisecond),
	}, nil
}

func (m *fakeMarket) Funding(context.Context, string) (Funding, error) {
	return Funding{Rate: "0.0001"}, nil
}

func (m *fakeMarket) SettledBetween(context.Context, string, time.Time, time.Time) ([]Settled, error) {
	m.mu.Lock()
	m.settled++
	m.mu.Unlock()
	return []Settled{{Rate: "0.0001", TimestampMs: 1}}, nil
}

type memRecorder struct {
	mu      sync.Mutex
	records []map[string]any
}

func (r *memRecorder) Write(record any) error {
	body, _ := json.Marshal(record)
	var m map[string]any
	_ = json.Unmarshal(body, &m)
	r.mu.Lock()
	r.records = append(r.records, m)
	r.mu.Unlock()
	return nil
}

func signal(barEnd time.Time) burstengine.Signal {
	return burstengine.Signal{
		Contract: burstengine.ContractVersion, Exchange: "bybit", Symbol: symbolMarker,
		BarStart: barEnd.Add(-time.Minute), BarEnd: barEnd, Return: returnMarker,
		Turnover: 98765, Median: 4321, EvaluatedAt: barEnd.Add(300 * time.Millisecond),
	}
}

func TestEntryExitAndSettledFundingAreSealed(t *testing.T) {
	market, rec := &fakeMarket{}, &memRecorder{}
	p := New(market, rec, Config{MaxOpen: 3, Hold: 100 * time.Millisecond, QuoteTimeout: time.Second})
	p.Handle(context.Background(), signal(time.Now()))
	p.Wait()
	kinds := []string{}
	for _, r := range rec.records {
		kinds = append(kinds, r["kind"].(string))
	}
	if strings.Join(kinds, ",") != "entry,exit" || market.books != 2 || market.settled != 1 {
		t.Fatalf("records %v books %d settled %d", kinds, market.books, market.settled)
	}
	if rec.records[1]["settled_funding"] == nil {
		t.Fatalf("exit without settled funding: %v", rec.records[1])
	}
	h := p.Health()
	if h["completed"] != 1 || h[StageBookRTT+"_p50_ms"] != 30 || h["open"] != 0 {
		t.Fatalf("health %v", h)
	}
}

func TestSignalsBeyondTheSlotsAreBlocked(t *testing.T) {
	market, rec := &fakeMarket{}, &memRecorder{}
	p := New(market, rec, Config{MaxOpen: 1, Hold: 200 * time.Millisecond, QuoteTimeout: time.Second})
	p.Handle(context.Background(), signal(time.Now()))
	p.Handle(context.Background(), signal(time.Now()))
	p.Wait()
	if h := p.Health(); h["blocked"] != 1 || h["completed"] != 1 {
		t.Fatalf("health %v", h)
	}
}

func TestQuoteFailuresAreCountedAndShutdownMissesTheExit(t *testing.T) {
	market, rec := &fakeMarket{fail: true}, &memRecorder{}
	p := New(market, rec, Config{MaxOpen: 3, Hold: time.Hour, QuoteTimeout: time.Second})
	ctx, cancel := context.WithCancel(context.Background())
	p.Handle(ctx, signal(time.Now()))
	time.Sleep(50 * time.Millisecond)
	cancel()
	p.Wait()
	if h := p.Health(); h["entry_quote_failed"] != 1 || h["exit_missed_shutdown"] != 1 {
		t.Fatalf("health %v", h)
	}
}

func TestHealthCarriesNoInstrumentAndNoMarketValue(t *testing.T) {
	market, rec := &fakeMarket{}, &memRecorder{}
	p := New(market, rec, Config{MaxOpen: 1, Hold: 50 * time.Millisecond, QuoteTimeout: time.Second})
	p.Handle(context.Background(), signal(time.Now()))
	p.Handle(context.Background(), signal(time.Now())) // blocked
	p.Wait()
	body, _ := json.Marshal(p.Health())
	for _, marker := range []string{symbolMarker, priceMarker, "0.0731", "98765", "4321", "0.0001"} {
		if strings.Contains(string(body), marker) {
			t.Fatalf("health leaks %q: %s", marker, body)
		}
	}
	sealed, _ := json.Marshal(rec.records)
	if !strings.Contains(string(sealed), symbolMarker) || !strings.Contains(string(sealed), priceMarker) {
		t.Fatal("the sealed records must hold the market content")
	}
}

func TestSealedFilesRotateByDayWithManifests(t *testing.T) {
	dir := t.TempDir()
	day := time.Date(2026, 10, 6, 23, 59, 0, 0, time.UTC)
	s, err := NewSealed(dir, burstengine.ContractVersion)
	if err != nil {
		t.Fatal(err)
	}
	s.clock = func() time.Time { return day }
	_ = s.Write(map[string]int{"a": 1})
	_ = s.Write(map[string]int{"a": 2})
	day = day.Add(2 * time.Minute) // the next UTC day closes the first
	_ = s.Write(map[string]int{"a": 3})
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	body, err := os.ReadFile(filepath.Join(dir, "sealed-2026-10-06.manifest.json")) //nolint:gosec // test temp dir
	if err != nil || json.Unmarshal(body, &m) != nil || m["lines"].(float64) != 2 {
		t.Fatalf("manifest %s %v", body, err)
	}
	info, _ := os.Stat(filepath.Join(dir, "sealed-2026-10-06.ndjson.gz"))
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", info.Mode().Perm())
	}
	// a restart on the same day appends a new gzip member; the manifest counts both
	s2, _ := NewSealed(dir, burstengine.ContractVersion)
	s2.clock = func() time.Time { return day }
	_ = s2.Write(map[string]int{"a": 4})
	_ = s2.Close()
	body, _ = os.ReadFile(filepath.Join(dir, "sealed-2026-10-07.manifest.json")) //nolint:gosec // test temp dir
	_ = json.Unmarshal(body, &m)
	if m["lines"].(float64) != 2 {
		t.Fatalf("manifest after restart %s", body)
	}
}

func TestRESTParsesTheVenueShapes(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v5/market/orderbook":
			_, _ = w.Write([]byte(`{"retCode":0,"result":{"s":"AAAUSDT","b":[["1.0","5"]],"a":[["1.1","6"]],"ts":1790000000000,"u":3,"seq":4,"cts":1790000000001}}`))
		case "/v5/market/tickers":
			_, _ = w.Write([]byte(`{"retCode":0,"result":{"list":[{"symbol":"AAAUSDT","fundingRate":"0.0002","nextFundingTime":"1790006400000","fundingIntervalHour":"8"}]}}`))
		case "/v5/market/funding/history":
			_, _ = w.Write([]byte(`{"retCode":0,"result":{"list":[{"symbol":"AAAUSDT","fundingRate":"0.0002","fundingRateTimestamp":"1790000000000"},{"symbol":"AAAUSDT","fundingRate":"0.1","fundingRateTimestamp":"1"}]}}`))
		}
	}))
	defer server.Close()
	rest := REST{Base: server.URL}
	ctx := context.Background()
	book, err := rest.Book(ctx, "AAAUSDT")
	if err != nil || book.ExchangeTS != 1790000000000 || book.Asks[0][0] != "1.1" {
		t.Fatalf("book %+v %v", book, err)
	}
	if _, err := rest.Book(ctx, "OTHER"); err == nil {
		t.Fatal("a book for another symbol must be refused")
	}
	funding, err := rest.Funding(ctx, "AAAUSDT")
	if err != nil || funding.IntervalHours != 8 || funding.NextFundingMs != 1790006400000 {
		t.Fatalf("funding %+v %v", funding, err)
	}
	settled, err := rest.SettledBetween(ctx, "AAAUSDT", time.UnixMilli(1789999999000), time.UnixMilli(1790000001000))
	if err != nil || len(settled) != 1 {
		t.Fatalf("settled %+v %v (only settlements inside the hold)", settled, err)
	}
}

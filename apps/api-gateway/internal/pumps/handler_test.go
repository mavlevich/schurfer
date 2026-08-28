package pumps

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/redis/go-redis/v9"
)

func ptr(v int64) *int64 { return &v }

func TestIsValidBase(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		base string
		want bool
	}{
		{name: "ASCII symbol", base: "BROCCOLIF3B", want: true},
		{name: "Unicode symbol", base: "草根文化", want: true},
		{name: "Unicode decimal digits", base: "ＴＯＫＥＮ１２", want: true},
		{name: "empty", base: "", want: false},
		{name: "hyphen", base: "BTC-USDT", want: false},
		{name: "slash", base: "BTC/USDT", want: false},
		{name: "encoded slash text", base: "BTC%2FUSDT", want: false},
		{name: "path traversal", base: "../BTC", want: false},
		{name: "whitespace", base: "BTC USDT", want: false},
		{name: "underscore", base: "草根_文化", want: false},
		{name: "zero width separator", base: "BTC\u200bUSDT", want: false},
		{name: "too long", base: strings.Repeat("A", maxBaseRunes+1), want: false},
		{name: "too many Unicode runes", base: strings.Repeat("草", maxBaseRunes+1), want: false},
		{name: "invalid UTF-8", base: string([]byte{0xff}), want: false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := isValidBase(tc.base); got != tc.want {
				t.Errorf("isValidBase(%q) = %v, want %v", tc.base, got, tc.want)
			}
		})
	}
}

func TestTokenHandlerSupportsUnicodeBase(t *testing.T) {
	t.Parallel()
	const base = "草根文化"
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	payload, err := json.Marshal(pumpsPayload{
		Count: 1,
		Pumps: []pumpEntry{{
			Base:         base,
			MaxChangePct: 43.1,
			Exchanges:    []exchangeEntry{{Exchange: "gate", Symbol: base + "_USDT"}},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := rdb.Set(context.Background(), "pumps:latest", payload, 0).Err(); err != nil {
		t.Fatal(err)
	}

	h := &Handler{rdb: rdb}
	router := chi.NewRouter()
	router.Get("/api/pumps/{base}", h.Token)
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/"+url.PathEscape(base), nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var response pumpEntry
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Base != base {
		t.Errorf("base = %q, want %q", response.Base, base)
	}
	if !response.IsLive {
		t.Errorf("is_live = false, want true for a base actually present in pumps:latest")
	}
}

// TestTokenHandlerFallsBackToDBWhenNotInLiveSnapshot regression-tests the
// 2026-08-28 production report: TRUMP/TAC/AURASOL/ARIA/LONGXIA/龙虾/AIINU all
// showed "Token not found" on TokenPage purely because none of them happened
// to be in pumps:latest at request time, despite having real DB history.
func TestTokenHandlerFallsBackToDBWhenNotInLiveSnapshot(t *testing.T) {
	t.Parallel()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	// Live snapshot exists but does not contain TRUMP.
	payload, err := json.Marshal(pumpsPayload{
		Count: 1,
		Pumps: []pumpEntry{{Base: "OTHERTOKEN", MaxChangePct: 10}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := rdb.Set(context.Background(), "pumps:latest", payload, 0).Err(); err != nil {
		t.Fatal(err)
	}

	exchangesJSON, err := json.Marshal([]exchangeEntry{{Exchange: "bingx", Symbol: "TRUMPSOL-USDT"}})
	if err != nil {
		t.Fatal(err)
	}
	pool := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(9399), 88.01, exchangesJSON}}
		},
	}

	h := &Handler{rdb: rdb, pool: pool}
	router := chi.NewRouter()
	router.Get("/api/pumps/{base}", h.Token)
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/TRUMP", nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
	var response pumpEntry
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Base != "TRUMP" || response.PumpEventID != 9399 {
		t.Errorf("response = %+v, want base=TRUMP pump_event_id=9399", response)
	}
	if len(response.Exchanges) != 1 || response.Exchanges[0].Symbol != "TRUMPSOL-USDT" {
		t.Errorf("exchanges = %+v, want the DB-sourced TRUMPSOL-USDT entry", response.Exchanges)
	}
	// Colleague review (2026-08-28): the DB-sourced entry is historical, not
	// live -- TokenPage's "Active on N exchanges" and its header change_pct
	// must not read as current for a token that fell out of pumps:latest.
	if response.IsLive {
		t.Errorf("is_live = true, want false for a DB-fallback (historical) entry")
	}
}

// TestTokenHandlerLiveEntryTakesPrecedenceOverDBFallback confirms IsLive is
// exactly the discriminator TokenPage/ExchangeBreakdown need: true only when
// the base is actually present in pumps:latest right now, false whenever the
// response came from dbTokenFallback, regardless of what the DB itself holds
// for the same base.
func TestTokenHandlerLiveEntryTakesPrecedenceOverDBFallback(t *testing.T) {
	t.Parallel()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	payload, err := json.Marshal(pumpsPayload{
		Count: 1,
		Pumps: []pumpEntry{{Base: "ACTIVE", MaxChangePct: 55}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := rdb.Set(context.Background(), "pumps:latest", payload, 0).Err(); err != nil {
		t.Fatal(err)
	}
	// A pool that would panic if queried -- the live match must return
	// before ever falling through to the DB.
	pool := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			t.Fatal("dbTokenFallback must not be queried when the live snapshot has a match")
			return nil
		},
	}

	h := &Handler{rdb: rdb, pool: pool}
	router := chi.NewRouter()
	router.Get("/api/pumps/{base}", h.Token)
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/ACTIVE", nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	var response pumpEntry
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if !response.IsLive {
		t.Errorf("is_live = false, want true for a live-snapshot match")
	}
}

// TestTokenHandlerFallsBackToDBWhenRedisKeyMissing covers the previously
// hard-404 branch: pumps:latest itself absent from Redis (redis.Nil) must
// also fall through to the DB, not stop at "not found" immediately.
func TestTokenHandlerFallsBackToDBWhenRedisKeyMissing(t *testing.T) {
	t.Parallel()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	// No pumps:latest key set at all.

	exchangesJSON, err := json.Marshal([]exchangeEntry{{Exchange: "gate"}})
	if err != nil {
		t.Fatal(err)
	}
	pool := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(1), 20.0, exchangesJSON}}
		},
	}

	h := &Handler{rdb: rdb, pool: pool}
	router := chi.NewRouter()
	router.Get("/api/pumps/{base}", h.Token)
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/OLDCOIN", nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
	}
}

// TestTokenHandlerReturnsNotFoundWhenNeitherLiveNorDBHasIt confirms the
// fallback stays fail-closed: a base absent from both the live snapshot and
// app.pump_events is still a genuine 404, not silently defaulted.
func TestTokenHandlerReturnsNotFoundWhenNeitherLiveNorDBHasIt(t *testing.T) {
	t.Parallel()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })

	pool := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{err: pgx.ErrNoRows}
		},
	}

	h := &Handler{rdb: rdb, pool: pool}
	router := chi.NewRouter()
	router.Get("/api/pumps/{base}", h.Token)
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/NEVERSEEN", nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404; body: %s", w.Code, w.Body.String())
	}
}

// ---- stub DB infrastructure ----

// stubRow implements pgxRow with a fixed set of values.
type stubRow struct {
	vals []any
	err  error
}

func (r *stubRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	for i, d := range dest {
		if i >= len(r.vals) {
			break
		}
		if err := scanInto(d, r.vals[i]); err != nil {
			return err
		}
	}
	return nil
}

// scanInto sets *dest = src via reflection.
// dest must be a non-nil pointer; src must be assignable or convertible to
// the element type of dest.
func scanInto(dest, src any) error {
	dv := reflect.ValueOf(dest)
	if dv.Kind() != reflect.Ptr || dv.IsNil() {
		return fmt.Errorf("scanInto: dest must be a non-nil pointer, got %T", dest)
	}
	dv = dv.Elem()
	if src == nil {
		dv.Set(reflect.Zero(dv.Type()))
		return nil
	}
	sv := reflect.ValueOf(src)
	if sv.Type().AssignableTo(dv.Type()) {
		dv.Set(sv)
		return nil
	}
	if sv.Type().ConvertibleTo(dv.Type()) {
		dv.Set(sv.Convert(dv.Type()))
		return nil
	}
	return fmt.Errorf("scanInto: cannot assign %T to *%T", src, dv.Interface())
}

// stubRows implements pgx.Rows with a fixed column dataset.
type stubRows struct {
	cols [][]any
	idx  int
}

func (r *stubRows) Next() bool                                   { r.idx++; return r.idx <= len(r.cols) }
func (r *stubRows) Close()                                       {}
func (r *stubRows) Err() error                                   { return nil }
func (r *stubRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (r *stubRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (r *stubRows) Values() ([]any, error)                       { return r.cols[r.idx-1], nil }
func (r *stubRows) RawValues() [][]byte                          { return nil }
func (r *stubRows) Conn() *pgx.Conn                              { return nil }
func (r *stubRows) Scan(dest ...any) error {
	row := r.cols[r.idx-1]
	for i, d := range dest {
		if i >= len(row) {
			break
		}
		if err := scanInto(d, row[i]); err != nil {
			return err
		}
	}
	return nil
}

// stubQuerier implements pgxPool via caller-supplied callbacks.
type stubQuerier struct {
	onQueryRow func(ctx context.Context, sql string, args ...any) pgxRow
	onQuery    func(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

func (q *stubQuerier) QueryRow(ctx context.Context, sql string, args ...any) pgxRow {
	return q.onQueryRow(ctx, sql, args...)
}

func (q *stubQuerier) Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error) {
	if q.onQuery == nil {
		return &stubRows{}, nil
	}
	return q.onQuery(ctx, sql, args...)
}

// serveSignals routes a GET request through chi and returns the recorder.
func serveSignals(q pgxPool, base string) *httptest.ResponseRecorder {
	h := &Handler{pool: q}
	r := chi.NewRouter()
	r.Get("/api/pumps/{base}/signals", h.Signals)
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/"+base+"/signals", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

// serveFunding routes a GET request through chi and returns the recorder.
func serveFunding(q pgxPool, base string) *httptest.ResponseRecorder {
	h := &Handler{pool: q}
	r := chi.NewRouter()
	r.Get("/api/pumps/{base}/funding", h.Funding)
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/"+base+"/funding", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

// ---- TestAggregateOI ----

func TestAggregateOI(t *testing.T) {
	cases := []struct {
		name         string
		snapshots    []oiSnapshotEntry
		firstSeenAt  *int64
		wantCurrent  float64
		wantBaseline float64
		wantDeltaNil bool
		wantDeltaPct float64
	}{
		{
			name:         "no snapshots",
			snapshots:    nil,
			firstSeenAt:  ptr(100),
			wantCurrent:  0,
			wantBaseline: 0,
			wantDeltaNil: true,
		},
		{
			name: "single snapshot per exchange — delta is zero, not nil",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 1000, TS: 100},
				{Exchange: "bybit", OiUSD: 500, TS: 100},
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  1500,
			wantBaseline: 1500,
			wantDeltaPct: 0,
		},
		{
			name: "OI grows — positive delta across exchanges",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 1000, TS: 100},
				{Exchange: "binance", OiUSD: 1200, TS: 200},
				{Exchange: "bybit", OiUSD: 500, TS: 100},
				{Exchange: "bybit", OiUSD: 600, TS: 200},
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  1800,
			wantBaseline: 1500,
			wantDeltaPct: 20,
		},
		{
			name: "OI declines — negative delta (divergence signal)",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 1000, TS: 100},
				{Exchange: "binance", OiUSD: 800, TS: 200},
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  800,
			wantBaseline: 1000,
			wantDeltaPct: -20,
		},
		{
			name: "no open/closed episode (firstSeenAt nil) — baseline is earliest snapshot",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 1000, TS: 100},
				{Exchange: "binance", OiUSD: 1100, TS: 200},
			},
			firstSeenAt:  nil,
			wantCurrent:  1100,
			wantBaseline: 1000,
			wantDeltaPct: 10,
		},
		{
			name: "snapshot exists only before episode start — exchange excluded from baseline",
			// A late-joining exchange's first row is after firstSeenAt, so it
			// correctly becomes both baseline and current for that exchange.
			snapshots: []oiSnapshotEntry{
				{Exchange: "okx", OiUSD: 5000, TS: 50}, // before firstSeenAt — would be wrong as baseline
				{Exchange: "okx", OiUSD: 6000, TS: 150},
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  6000,
			wantBaseline: 6000,
			wantDeltaPct: 0,
		},
		{
			name: "new exchange joins mid-episode — only contributes to current, not baseline",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 1000, TS: 100},
				{Exchange: "binance", OiUSD: 1000, TS: 200},
				{Exchange: "bybit", OiUSD: 300, TS: 200}, // joined after t=100, still counted
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  1300,
			wantBaseline: 1300,
			wantDeltaPct: 0,
		},
		{
			name: "zero baseline — delta stays nil instead of dividing by zero",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 0, TS: 100},
				{Exchange: "binance", OiUSD: 500, TS: 200},
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  500,
			wantBaseline: 0,
			wantDeltaNil: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			current, baseline, deltaPct := aggregateOI(tc.snapshots, tc.firstSeenAt)

			if current != tc.wantCurrent {
				t.Errorf("current = %v, want %v", current, tc.wantCurrent)
			}
			if baseline != tc.wantBaseline {
				t.Errorf("baseline = %v, want %v", baseline, tc.wantBaseline)
			}
			if tc.wantDeltaNil {
				if deltaPct != nil {
					t.Errorf("deltaPct = %v, want nil", *deltaPct)
				}
				return
			}
			if deltaPct == nil {
				t.Fatalf("deltaPct = nil, want %v", tc.wantDeltaPct)
			}
			if *deltaPct != tc.wantDeltaPct {
				t.Errorf("deltaPct = %v, want %v", *deltaPct, tc.wantDeltaPct)
			}
		})
	}
}

// ---- TestFundingEntryFields ----

func TestFundingEntryFields(t *testing.T) {
	cases := []struct {
		name       string
		rate       float64
		wantPct    float64
		wantAPR    float64
		wantElevat bool
	}{
		{
			name:       "zero rate — not elevated",
			rate:       0,
			wantPct:    0,
			wantAPR:    0,
			wantElevat: false,
		},
		{
			name:       "0.01% per 8h — not elevated",
			rate:       0.0001,
			wantPct:    0.01,
			wantAPR:    0.0001 * fundingPeriodsPerYear * 100,
			wantElevat: false,
		},
		{
			name:       "exactly at threshold — not elevated (strict greater-than)",
			rate:       fundingElevatedThreshold,
			wantPct:    fundingElevatedThreshold * 100,
			wantAPR:    fundingElevatedThreshold * fundingPeriodsPerYear * 100,
			wantElevat: false,
		},
		{
			name:       "0.2% per 8h — elevated, APR = 219%",
			rate:       0.002,
			wantPct:    0.2,
			wantAPR:    0.002 * fundingPeriodsPerYear * 100,
			wantElevat: true,
		},
		{
			name:       "negative rate — shorts paying (bearish), not elevated",
			rate:       -0.0005,
			wantPct:    -0.05,
			wantAPR:    -0.0005 * fundingPeriodsPerYear * 100,
			wantElevat: false,
		},
	}

	const eps = 1e-9
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			e := fundingEntry{
				Rate:       tc.rate,
				RatePct:    tc.rate * 100,
				AprPct:     tc.rate * fundingPeriodsPerYear * 100,
				IsElevated: tc.rate > fundingElevatedThreshold,
			}
			if math.Abs(e.RatePct-tc.wantPct) > eps {
				t.Errorf("RatePct = %v, want %v", e.RatePct, tc.wantPct)
			}
			if math.Abs(e.AprPct-tc.wantAPR) > eps {
				t.Errorf("AprPct = %v, want %v", e.AprPct, tc.wantAPR)
			}
			if e.IsElevated != tc.wantElevat {
				t.Errorf("IsElevated = %v, want %v", e.IsElevated, tc.wantElevat)
			}
		})
	}
}

// ---- TestFundingHandler ----

func TestFundingHandler(t *testing.T) {
	cases := []struct {
		name          string
		base          string
		episodeVals   []any   // nil → no episode (ErrNoRows); otherwise []*int64 for Scan(&eventID, &firstSeenAt)
		snapshotRows  [][]any // rows returned by the snapshot Query
		wantStatus    int
		wantBase      string
		wantFirstSeen *int64
		wantExchanges int
		checkEntries  func(t *testing.T, entries []fundingEntry) // optional; called with full slice
	}{
		{
			name:       "invalid base returns 400",
			base:       "BTC-INVALID",
			wantStatus: http.StatusBadRequest,
		},
		{
			name:          "no episode — 200 with empty exchanges list",
			base:          "BTC",
			episodeVals:   nil,
			wantStatus:    http.StatusOK,
			wantBase:      "BTC",
			wantExchanges: 0,
		},
		{
			name:          "episode found but no snapshots yet — empty exchanges",
			base:          "BTC",
			episodeVals:   []any{ptr(1), ptr(1000)},
			wantStatus:    http.StatusOK,
			wantBase:      "BTC",
			wantFirstSeen: ptr(1000),
			wantExchanges: 0,
		},
		{
			name:        "elevated funding rate — is_elevated=true, rate_pct and apr_pct computed",
			base:        "ETH",
			episodeVals: []any{ptr(2), ptr(2000)},
			snapshotRows: [][]any{
				{"binance", float64(0.002), int64(2001)},
			},
			wantStatus:    http.StatusOK,
			wantBase:      "ETH",
			wantFirstSeen: ptr(2000),
			wantExchanges: 1,
			checkEntries: func(t *testing.T, entries []fundingEntry) {
				t.Helper()
				e := entries[0]
				if e.Exchange != "binance" {
					t.Errorf("exchange = %q, want binance", e.Exchange)
				}
				if !e.IsElevated {
					t.Error("IsElevated = false, want true (rate 0.002 > threshold 0.001)")
				}
				if math.Abs(e.RatePct-0.2) > 1e-9 {
					t.Errorf("rate_pct = %v, want 0.2", e.RatePct)
				}
				wantAPR := 0.002 * fundingPeriodsPerYear * 100
				if math.Abs(e.AprPct-wantAPR) > 1e-9 {
					t.Errorf("apr_pct = %v, want %v", e.AprPct, wantAPR)
				}
				if e.RecordedAt != 2001 {
					t.Errorf("recorded_at = %v, want 2001", e.RecordedAt)
				}
			},
		},
		{
			name:        "two exchanges — each checked by name, independent of response order",
			base:        "SOL",
			episodeVals: []any{ptr(3), ptr(3000)},
			snapshotRows: [][]any{
				{"binance", float64(0.0015), int64(3001)},
				{"bybit", float64(-0.0003), int64(3001)},
			},
			wantStatus:    http.StatusOK,
			wantBase:      "SOL",
			wantFirstSeen: ptr(3000),
			wantExchanges: 2,
			checkEntries: func(t *testing.T, entries []fundingEntry) {
				t.Helper()
				byEx := make(map[string]fundingEntry, len(entries))
				for _, e := range entries {
					byEx[e.Exchange] = e
				}
				binance, ok := byEx["binance"]
				if !ok {
					t.Fatal("binance entry missing from response")
				}
				if !binance.IsElevated {
					t.Error("binance: IsElevated = false, want true (rate 0.0015 > 0.001)")
				}
				bybit, ok := byEx["bybit"]
				if !ok {
					t.Fatal("bybit entry missing from response")
				}
				if bybit.IsElevated {
					t.Error("bybit: IsElevated = true, want false (rate -0.0003 < 0.001)")
				}
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			q := &stubQuerier{
				onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
					if tc.episodeVals == nil {
						return &stubRow{err: pgx.ErrNoRows}
					}
					return &stubRow{vals: tc.episodeVals}
				},
				onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
					return &stubRows{cols: tc.snapshotRows}, nil
				},
			}
			w := serveFunding(q, tc.base)

			if w.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d; body: %s", w.Code, tc.wantStatus, w.Body.String())
			}
			if tc.wantStatus != http.StatusOK {
				return
			}

			var resp fundingResponse
			if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
				t.Fatalf("unmarshal response: %v; body: %s", err, w.Body.String())
			}
			if resp.Base != tc.wantBase {
				t.Errorf("base = %q, want %q", resp.Base, tc.wantBase)
			}
			if tc.wantFirstSeen == nil {
				if resp.PumpFirstSeenAt != nil {
					t.Errorf("pump_first_seen_at = %v, want nil", *resp.PumpFirstSeenAt)
				}
			} else if resp.PumpFirstSeenAt == nil || *resp.PumpFirstSeenAt != *tc.wantFirstSeen {
				t.Errorf("pump_first_seen_at = %v, want %v", resp.PumpFirstSeenAt, *tc.wantFirstSeen)
			}
			if len(resp.Exchanges) != tc.wantExchanges {
				t.Fatalf("len(exchanges) = %d, want %d; body: %s", len(resp.Exchanges), tc.wantExchanges, w.Body.String())
			}
			if tc.checkEntries != nil {
				tc.checkEntries(t, resp.Exchanges)
			}
		})
	}
}

// ---- TestScoreSignals ----

func TestScoreSignals(t *testing.T) {
	// baseEp is a "neutral" episode — score contributions come only from the
	// parameters under test in each case.
	baseEp := func(ageH, peakPct, lastPct float64) signalEpisode {
		return signalEpisode{AgeHours: ageH, PeakPct: peakPct, LastPct: lastPct}
	}

	// neutralRetrace produces an episode where retrace > 15 pts → 0 pts from
	// RetraceFromPeak, so component-isolation tests stay unaffected by the inversion fix.
	neutralRetrace := func(ageH, peakPct float64) signalEpisode {
		return signalEpisode{AgeHours: ageH, PeakPct: peakPct, LastPct: 0}
	}

	cases := []struct {
		name        string
		ep          signalEpisode
		currentOI   float64
		baselineOI  float64
		maxFunding  float64
		wantScore   int
		wantVerdict string
		checkComp   func(t *testing.T, c signalComponents)
	}{
		{
			// retrace=0 (still at peak) → 2 pts; all other inputs zero → total 2.
			name:        "all zeros — verdict pumping",
			ep:          baseEp(0, 0, 0),
			wantScore:   2,
			wantVerdict: "pumping",
		},
		{
			// retrace=1 (near peak) → 2 pts; age<1h, price<30%, no OI/funding → total 2.
			name:        "early pump, moderate price, no OI/funding — verdict pumping",
			ep:          baseEp(0.5, 20, 19),
			wantScore:   2,
			wantVerdict: "pumping",
		},
		{
			// neutralRetrace sets lastPct=0, peakPct=20 → retrace=20 > 15 → 0 pts.
			// Only PumpAge contributes.
			name:        "mature pump >1h adds 1 pt",
			ep:          neutralRetrace(2, 20),
			wantScore:   1,
			wantVerdict: "pumping",
			checkComp: func(t *testing.T, c signalComponents) {
				t.Helper()
				if c.PumpAge.Points != 1 {
					t.Errorf("PumpAge.Points = %d, want 1", c.PumpAge.Points)
				}
			},
		},
		{
			name:        "late pump >4h adds 2 pts",
			ep:          neutralRetrace(6, 20),
			wantScore:   2,
			wantVerdict: "pumping",
			checkComp: func(t *testing.T, c signalComponents) {
				t.Helper()
				if c.PumpAge.Points != 2 {
					t.Errorf("PumpAge.Points = %d, want 2", c.PumpAge.Points)
				}
			},
		},
		{
			// retrace=16 > 15 → 0 pts; only PriceExtent contributes.
			name:        "price >100% adds 2 pts",
			ep:          baseEp(0, 150, 134),
			wantScore:   2,
			wantVerdict: "pumping",
			checkComp: func(t *testing.T, c signalComponents) {
				t.Helper()
				if c.PriceExtent.Points != 2 {
					t.Errorf("PriceExtent.Points = %d, want 2", c.PriceExtent.Points)
				}
			},
		},
		{
			name:        "OI declining >5% adds 2 pts",
			ep:          neutralRetrace(0, 20),
			currentOI:   900_000,
			baselineOI:  1_000_000,
			wantScore:   2,
			wantVerdict: "pumping",
			checkComp: func(t *testing.T, c signalComponents) {
				t.Helper()
				if c.OiTrend.Points != 2 {
					t.Errorf("OiTrend.Points = %d, want 2", c.OiTrend.Points)
				}
			},
		},
		{
			name:        "OI growing >5% adds 0 pts",
			ep:          neutralRetrace(0, 20),
			currentOI:   1_100_000,
			baselineOI:  1_000_000,
			wantScore:   0,
			wantVerdict: "pumping",
			checkComp: func(t *testing.T, c signalComponents) {
				t.Helper()
				if c.OiTrend.Points != 0 {
					t.Errorf("OiTrend.Points = %d, want 0", c.OiTrend.Points)
				}
			},
		},
		{
			name:        "OI neutral (within ±5%) adds 1 pt",
			ep:          neutralRetrace(0, 20),
			currentOI:   1_020_000,
			baselineOI:  1_000_000,
			wantScore:   1,
			wantVerdict: "pumping",
			checkComp: func(t *testing.T, c signalComponents) {
				t.Helper()
				if c.OiTrend.Points != 1 {
					t.Errorf("OiTrend.Points = %d, want 1", c.OiTrend.Points)
				}
			},
		},
		{
			name:        "elevated funding >0.1% adds 2 pts",
			ep:          neutralRetrace(0, 20),
			maxFunding:  0.0015,
			wantScore:   2,
			wantVerdict: "pumping",
			checkComp: func(t *testing.T, c signalComponents) {
				t.Helper()
				if c.FundingRate.Points != 2 {
					t.Errorf("FundingRate.Points = %d, want 2", c.FundingRate.Points)
				}
			},
		},
		{
			name:        "moderate funding 0.05-0.1% adds 1 pt",
			ep:          neutralRetrace(0, 20),
			maxFunding:  0.0007,
			wantScore:   1,
			wantVerdict: "pumping",
		},
		{
			// peakPct=20 keeps priceExtent at 0 pts; retrace=20 > 15 → 0 pts (entry passed).
			name:        "retrace >15 pts from peak adds 0 pts — entry likely passed",
			ep:          baseEp(0, 20, 0),
			wantScore:   0,
			wantVerdict: "pumping",
			checkComp: func(t *testing.T, c signalComponents) {
				t.Helper()
				if c.RetraceFromPeak.Points != 0 {
					t.Errorf("RetraceFromPeak.Points = %d, want 0 (retrace=20)", c.RetraceFromPeak.Points)
				}
			},
		},
		{
			// retrace=8, 5 < 8 ≤ 15 → 1 pt (cooling but still viable).
			name:        "retrace 5-15 pts from peak adds 1 pt",
			ep:          baseEp(0, 20, 12),
			wantScore:   1,
			wantVerdict: "pumping",
		},
		{
			// retrace=2 ≤ 5 → 2 pts (still near peak, ideal entry).
			name:        "retrace <5 pts from peak adds 2 pts — ideal entry window",
			ep:          baseEp(0, 20, 18),
			wantScore:   2,
			wantVerdict: "pumping",
			checkComp: func(t *testing.T, c signalComponents) {
				t.Helper()
				if c.RetraceFromPeak.Points != 2 {
					t.Errorf("RetraceFromPeak.Points = %d, want 2 (retrace=2)", c.RetraceFromPeak.Points)
				}
			},
		},
		{
			// retrace=16 > 15 → 0 pts; age+price+funding = 6.
			name:        "short_setup — age+price+funding",
			ep:          baseEp(5, 120, 104),
			maxFunding:  0.0015,
			wantScore:   6, // age=2 + price=2 + funding=2
			wantVerdict: "short_setup",
		},
		{
			// retrace=2 ≤ 5 → 2 pts; all five components maxed.
			name:        "prime_short — all components maxed",
			ep:          baseEp(6, 150, 148),
			currentOI:   800_000,
			baselineOI:  1_000_000,
			maxFunding:  0.002,
			wantScore:   10, // age=2 + price=2 + oi=2 + funding=2 + retrace=2
			wantVerdict: "prime_short",
		},
		{
			// retrace=6, 5 < 6 ≤ 15 → 1 pt; age=1 + price=1 + retrace=1 + funding=1 = 4.
			name:        "cooling_off boundary at score 4",
			ep:          baseEp(2, 50, 44),
			wantScore:   4,
			maxFunding:  0.0007,
			wantVerdict: "cooling_off",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			comp, score := scoreSignals(tc.ep, tc.currentOI, tc.baselineOI, tc.maxFunding)
			if score != tc.wantScore {
				t.Errorf("score = %d, want %d", score, tc.wantScore)
			}
			if v := signalVerdict(score); v != tc.wantVerdict {
				t.Errorf("verdict = %q, want %q", v, tc.wantVerdict)
			}
			if tc.checkComp != nil {
				tc.checkComp(t, comp)
			}
		})
	}
}

// ---- TestSignalsHandler ----

func TestSignalsHandler(t *testing.T) {
	// episodeRow returns vals for the episode QueryRow (no is_open — always true for open episodes).
	episodeRow := func(id, firstSeenAt int64, peakPct, lastPct float64) []any {
		return []any{id, firstSeenAt, ptr(firstSeenAt), peakPct, lastPct}
	}

	errRow := &stubRow{err: fmt.Errorf("db unavailable")}

	cases := []struct {
		name string
		base string
		// queryRowSeq: values returned by successive QueryRow calls.
		// call 1 = episode, call 2 = current OI, call 3 = baseline OI, call 4 = max funding.
		// nil → ErrNoRows for episode (404).
		queryRowSeq [][]any
		// errAtCall: if > 0, that call index (1-based) and all subsequent calls return errRow.
		errAtCall  int
		wantStatus int
		checkResp  func(t *testing.T, resp signalsResponse)
	}{
		{
			name:       "invalid base returns 400",
			base:       "BTC-INVALID",
			wantStatus: http.StatusBadRequest,
		},
		{
			name:        "no open episode returns 404",
			base:        "BTC",
			queryRowSeq: nil,
			wantStatus:  http.StatusNotFound,
		},
		{
			name: "OI query fails — data_quality.oi=false, both OI+funding fail → insufficient_data",
			base: "ETH",
			queryRowSeq: [][]any{
				episodeRow(1, 1000, 50.0, 49.0),
			},
			errAtCall:  2, // current OI fails; funding query also returns errRow
			wantStatus: http.StatusOK,
			checkResp: func(t *testing.T, resp signalsResponse) {
				t.Helper()
				if resp.DataQuality.OI {
					t.Error("data_quality.oi = true, want false (OI query failed)")
				}
				if resp.DataQuality.Funding {
					t.Error("data_quality.funding = true, want false")
				}
				if resp.Verdict != "insufficient_data" {
					t.Errorf("verdict = %q, want insufficient_data", resp.Verdict)
				}
				if resp.Episode.IsOpen != true {
					t.Error("episode.is_open = false, want true")
				}
			},
		},
		{
			name: "elevated funding and declining OI — prime_short, data_quality all true",
			base: "SOL",
			queryRowSeq: [][]any{
				episodeRow(2, 1000, 150.0, 148.0), // age≈years (mocked), peak>100, retrace=2pts (near peak → 2pts)
				{float64(800_000), int64(3)},      // current OI, 3 exchange rows
				{float64(1_000_000), int64(3)},    // baseline OI (declining -20%), 3 rows
				{float64(0.002), int64(3)},        // max funding (elevated), 3 rows
			},
			wantStatus: http.StatusOK,
			checkResp: func(t *testing.T, resp signalsResponse) {
				t.Helper()
				if resp.Score != signalMaxScore {
					t.Errorf("score = %d, want %d (all components maxed)", resp.Score, signalMaxScore)
				}
				if resp.Verdict != "prime_short" {
					t.Errorf("verdict = %q, want prime_short", resp.Verdict)
				}
				if !resp.DataQuality.OI || !resp.DataQuality.Funding {
					t.Errorf("data_quality = %+v, want both true", resp.DataQuality)
				}
			},
		},
		{
			name: "growing OI with low funding — verdict pumping",
			base: "DOGE",
			queryRowSeq: [][]any{
				episodeRow(3, 1000, 25.0, 9.0), // retrace=16 > 15 → 0 pts (entry passed)
				{float64(1_200_000), int64(2)}, // current OI growing +20%, 2 exchange rows
				{float64(1_000_000), int64(2)}, // baseline OI, 2 rows
				{float64(0.0001), int64(2)},    // low funding, 2 rows
			},
			wantStatus: http.StatusOK,
			checkResp: func(t *testing.T, resp signalsResponse) {
				t.Helper()
				if resp.Components.OiTrend.Points != 0 {
					t.Errorf("oi_trend.points = %d, want 0 (OI growing)", resp.Components.OiTrend.Points)
				}
				if resp.Verdict != "pumping" {
					t.Errorf("verdict = %q, want pumping", resp.Verdict)
				}
				if !resp.DataQuality.OI || !resp.DataQuality.Funding {
					t.Errorf("data_quality = %+v, want both true", resp.DataQuality)
				}
			},
		},
		{
			// Scan succeeds (COALESCE returns 0) but count=0 means no snapshot rows exist.
			// data_quality must be false even though the query didn't error.
			name: "OI query returns no rows (count=0) — data_quality.oi=false",
			base: "LTC",
			queryRowSeq: [][]any{
				episodeRow(5, 1000, 40.0, 39.0),
				{float64(0), int64(0)}, // current OI: scan ok, but no snapshot rows
				{float64(0), int64(0)}, // baseline OI: same
				{float64(0), int64(0)}, // funding: same
			},
			wantStatus: http.StatusOK,
			checkResp: func(t *testing.T, resp signalsResponse) {
				t.Helper()
				if resp.DataQuality.OI {
					t.Error("data_quality.oi = true, want false (no OI snapshot rows)")
				}
				if resp.DataQuality.Funding {
					t.Error("data_quality.funding = true, want false (no funding snapshot rows)")
				}
				if resp.Verdict != "insufficient_data" {
					t.Errorf("verdict = %q, want insufficient_data (both OI and funding missing)", resp.Verdict)
				}
			},
		},
		{
			name: "only funding fails — real verdict returned, data_quality.funding=false",
			base: "ADA",
			queryRowSeq: [][]any{
				episodeRow(4, 1000, 60.0, 55.0),
				{float64(900_000), int64(2)},   // current OI (declining -10%), 2 rows
				{float64(1_000_000), int64(2)}, // baseline OI, 2 rows
				// call 4 (funding) will error
			},
			errAtCall:  4,
			wantStatus: http.StatusOK,
			checkResp: func(t *testing.T, resp signalsResponse) {
				t.Helper()
				if !resp.DataQuality.OI {
					t.Error("data_quality.oi = false, want true")
				}
				if resp.DataQuality.Funding {
					t.Error("data_quality.funding = true, want false")
				}
				// OI is fine so verdict is NOT insufficient_data.
				if resp.Verdict == "insufficient_data" {
					t.Error("verdict = insufficient_data, want a real verdict (funding alone failing is not enough)")
				}
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var callIdx int
			q := &stubQuerier{
				onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
					if tc.queryRowSeq == nil {
						return &stubRow{err: pgx.ErrNoRows}
					}
					callIdx++
					if tc.errAtCall > 0 && callIdx >= tc.errAtCall {
						return errRow
					}
					if callIdx-1 >= len(tc.queryRowSeq) {
						return &stubRow{err: fmt.Errorf("unexpected QueryRow call %d", callIdx)}
					}
					return &stubRow{vals: tc.queryRowSeq[callIdx-1]}
				},
			}
			w := serveSignals(q, tc.base)

			if w.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d; body: %s", w.Code, tc.wantStatus, w.Body.String())
			}
			if tc.wantStatus != http.StatusOK {
				return
			}

			var resp signalsResponse
			if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
				t.Fatalf("unmarshal: %v; body: %s", err, w.Body.String())
			}
			if tc.checkResp != nil {
				tc.checkResp(t, resp)
			}
		})
	}
}

// ---- TestStatsHandler ----

func serveStats(q pgxPool, base string) *httptest.ResponseRecorder {
	h := &Handler{pool: q}
	r := chi.NewRouter()
	r.Get("/api/pumps/{base}/stats", h.Stats)
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/"+base+"/stats", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

func fptr(v float64) *float64 { return &v }

func TestStatsHandler(t *testing.T) {
	// statsRow returns the 10 values that Stats's QueryRow scans.
	// Pass nil for nullable retrace fields when no retrace data exists.
	statsRow := func(
		episodeCount, retraceCount int,
		avgPeak, medianPeak float64,
		avgRetrace, medianRetrace, minRetrace, maxRetrace interface{},
		avgDur, medDur float64,
	) []any {
		return []any{
			int64(episodeCount), int64(retraceCount),
			avgPeak, medianPeak,
			avgRetrace, medianRetrace, minRetrace, maxRetrace,
			avgDur, medDur,
		}
	}

	cases := []struct {
		name       string
		base       string
		row        []any // nil → no row / simulate scan error for 404 path
		wantStatus int
		checkResp  func(t *testing.T, resp tokenStatsResponse)
	}{
		{
			name:       "invalid base returns 400",
			base:       "BTC-INVALID",
			wantStatus: http.StatusBadRequest,
		},
		{
			// episode_count = 0 → no closed history → 404.
			name:       "no closed episodes returns 404",
			base:       "BTC",
			row:        statsRow(0, 0, 0, 0, nil, nil, nil, nil, 0, 0),
			wantStatus: http.StatusNotFound,
		},
		{
			// 3 episodes, all have retrace data → confidence "medium".
			// DB returns raw floats; handler must round to 1 decimal.
			name: "full stats — 3 episodes with retrace",
			base: "SOL",
			row: statsRow(
				3, 3,
				65.123, 60.456,
				fptr(-42.167), fptr(-38.333), fptr(-75.0), fptr(-20.0),
				4.567, 3.5,
			),
			wantStatus: http.StatusOK,
			checkResp: func(t *testing.T, resp tokenStatsResponse) {
				t.Helper()
				if resp.Confidence != "medium" {
					t.Errorf("confidence = %q, want medium (3 episodes)", resp.Confidence)
				}
				if resp.AvgPeakPct != 65.1 {
					t.Errorf("avg_peak_pct = %v, want 65.1 (rounded)", resp.AvgPeakPct)
				}
				if resp.MedianPeakPct != 60.5 {
					t.Errorf("median_peak_pct = %v, want 60.5 (rounded)", resp.MedianPeakPct)
				}
				if resp.AvgRetracePct == nil || *resp.AvgRetracePct != -42.2 {
					t.Errorf("avg_retrace_pct = %v, want -42.2 (rounded)", resp.AvgRetracePct)
				}
				if resp.MinRetracePct == nil || *resp.MinRetracePct != -75.0 {
					t.Errorf("min_retrace_pct = %v, want -75.0", resp.MinRetracePct)
				}
				if resp.AvgDurationHours != 4.6 {
					t.Errorf("avg_duration_hours = %v, want 4.6 (rounded)", resp.AvgDurationHours)
				}
			},
		},
		{
			// 1 episode → confidence "low".
			name:       "single episode — confidence low",
			base:       "DOGE",
			row:        statsRow(1, 1, 40.0, 40.0, fptr(-30.0), fptr(-30.0), fptr(-30.0), fptr(-30.0), 2.0, 2.0),
			wantStatus: http.StatusOK,
			checkResp: func(t *testing.T, resp tokenStatsResponse) {
				t.Helper()
				if resp.Confidence != "low" {
					t.Errorf("confidence = %q, want low (1 episode)", resp.Confidence)
				}
			},
		},
		{
			// 6 episodes → confidence "high".
			name:       "six episodes — confidence high",
			base:       "BNB",
			row:        statsRow(6, 5, 55.0, 52.0, fptr(-38.0), fptr(-36.0), fptr(-60.0), fptr(-15.0), 3.0, 2.5),
			wantStatus: http.StatusOK,
			checkResp: func(t *testing.T, resp tokenStatsResponse) {
				t.Helper()
				if resp.Confidence != "high" {
					t.Errorf("confidence = %q, want high (6 episodes)", resp.Confidence)
				}
			},
		},
		{
			// 3 episodes, none have retrace_pct yet — retrace fields must be null.
			name: "episodes without retrace — retrace fields null",
			base: "ETH",
			row: statsRow(
				3, 0,
				50.0, 48.0,
				nil, nil, nil, nil,
				2.0, 1.5,
			),
			wantStatus: http.StatusOK,
			checkResp: func(t *testing.T, resp tokenStatsResponse) {
				t.Helper()
				if resp.RetraceCount != 0 {
					t.Errorf("retrace_count = %d, want 0", resp.RetraceCount)
				}
				if resp.AvgRetracePct != nil {
					t.Errorf("avg_retrace_pct = %v, want nil", resp.AvgRetracePct)
				}
				if resp.MedianRetracePct != nil {
					t.Errorf("median_retrace_pct = %v, want nil", resp.MedianRetracePct)
				}
				if resp.MinRetracePct != nil {
					t.Errorf("min_retrace_pct = %v, want nil", resp.MinRetracePct)
				}
				if resp.MaxRetracePct != nil {
					t.Errorf("max_retrace_pct = %v, want nil", resp.MaxRetracePct)
				}
				if resp.EpisodeCount != 3 {
					t.Errorf("episode_count = %d, want 3", resp.EpisodeCount)
				}
				if resp.AvgPeakPct != 50.0 {
					t.Errorf("avg_peak_pct = %v, want 50.0", resp.AvgPeakPct)
				}
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			q := &stubQuerier{
				onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
					if tc.row == nil {
						return &stubRow{err: fmt.Errorf("unexpected call")}
					}
					return &stubRow{vals: tc.row}
				},
			}
			w := serveStats(q, tc.base)

			if w.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d; body: %s", w.Code, tc.wantStatus, w.Body.String())
			}
			if tc.wantStatus != http.StatusOK {
				return
			}

			var resp tokenStatsResponse
			if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
				t.Fatalf("unmarshal: %v; body: %s", err, w.Body.String())
			}
			if tc.checkResp != nil {
				tc.checkResp(t, resp)
			}
		})
	}
}

// ---- TestParseBingX ----

func TestParseBingX(t *testing.T) {
	t.Run("parses valid response", func(t *testing.T) {
		raw := `{"code":0,"msg":"","data":[
			{"time":1700000000000,"open":"100.0","high":"110.0","low":"90.0","close":"105.0","volume":"500.0"},
			{"time":1700000060000,"open":"105.0","high":"115.0","low":"95.0","close":"108.0","volume":"600.0"}
		]}`
		candles, err := parseBingX([]byte(raw))
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(candles) != 2 {
			t.Fatalf("want 2 candles, got %d", len(candles))
		}
		if candles[0].Time != 1700000000 {
			t.Errorf("want time=1700000000 (ms→s), got %d", candles[0].Time)
		}
		if candles[0].Open != 100.0 {
			t.Errorf("want open=100.0, got %f", candles[0].Open)
		}
		if candles[0].Volume != 500.0 {
			t.Errorf("want volume=500.0, got %f", candles[0].Volume)
		}
	})

	t.Run("exchange error returns error", func(t *testing.T) {
		raw := `{"code":80001,"msg":"Invalid symbol"}`
		_, err := parseBingX([]byte(raw))
		if err == nil {
			t.Fatal("expected error for non-zero code")
		}
	})

	t.Run("invalid json returns error", func(t *testing.T) {
		_, err := parseBingX([]byte(`not json`))
		if err == nil {
			t.Fatal("expected error for invalid json")
		}
	})
}

// ---- TestParseMEXC ----

func TestParseMEXC(t *testing.T) {
	t.Run("parses valid futures columnar response", func(t *testing.T) {
		raw := `{
			"success": true,
			"code": 0,
			"data": {
				"time":  [1700000000, 1700000900],
				"open":  ["100.0", "105.0"],
				"high":  ["110.0", "115.0"],
				"low":   ["90.0",  "95.0"],
				"close": ["105.0", "108.0"],
				"vol":   ["500.0", "600.0"]
			}
		}`
		candles, err := parseMEXC([]byte(raw))
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(candles) != 2 {
			t.Fatalf("want 2 candles, got %d", len(candles))
		}
		if candles[0].Time != 1700000000 {
			t.Errorf("want time=1700000000 (already seconds), got %d", candles[0].Time)
		}
		if candles[1].Close != 108.0 {
			t.Errorf("want close=108.0, got %f", candles[1].Close)
		}
		if candles[0].Volume != 500.0 {
			t.Errorf("want volume=500.0, got %f", candles[0].Volume)
		}
	})

	t.Run("parses columnar fields sent as JSON numbers, not strings", func(t *testing.T) {
		// MEXC's futures kline API sends open/high/low/close/vol as bare JSON
		// numbers for some symbols instead of quoted strings — observed live
		// for BRIAN and WISHBONE, which broke OHLCV chart loading for both.
		raw := `{
			"success": true,
			"code": 0,
			"data": {
				"time":  [1700000000, 1700000900],
				"open":  [0.0151, 0.01491],
				"high":  [0.01523, 0.01804],
				"low":   [0.01491, 0.01434],
				"close": [0.01491, 0.01679],
				"vol":   [59.0, 6850.0]
			}
		}`
		candles, err := parseMEXC([]byte(raw))
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(candles) != 2 {
			t.Fatalf("want 2 candles, got %d", len(candles))
		}
		if candles[0].Open != 0.0151 {
			t.Errorf("want open=0.0151, got %f", candles[0].Open)
		}
		if candles[1].Close != 0.01679 {
			t.Errorf("want close=0.01679, got %f", candles[1].Close)
		}
	})

	t.Run("exchange error returns error", func(t *testing.T) {
		raw := `{"success":false,"code":2001,"message":"symbol not found"}`
		_, err := parseMEXC([]byte(raw))
		if err == nil {
			t.Fatal("expected error for non-zero code")
		}
	})

	t.Run("empty data returns nil candles", func(t *testing.T) {
		raw := `{"success":true,"code":0,"data":{"time":[],"open":[],"high":[],"low":[],"close":[],"vol":[]}}`
		candles, err := parseMEXC([]byte(raw))
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if len(candles) != 0 {
			t.Errorf("want 0 candles, got %d", len(candles))
		}
	})

	t.Run("invalid json returns error", func(t *testing.T) {
		_, err := parseMEXC([]byte(`not json`))
		if err == nil {
			t.Fatal("expected error for invalid json")
		}
	})
}

// ---- TestRankExchangeEntries ----

func TestRankExchangeEntries(t *testing.T) {
	t.Run("sorts by volume descending and filters unsupported", func(t *testing.T) {
		entries := []exchangeEntry{
			{Exchange: "mexc", Volume24hUSD: fptr(1_000_000)},
			{Exchange: "gate", Volume24hUSD: fptr(900_000)},
			{Exchange: "huobi", Volume24hUSD: fptr(2_000_000)}, // not in supportedOHLCV
			{Exchange: "bingx", Volume24hUSD: fptr(500_000)},
		}
		got := rankExchangeEntries(entries)
		want := []string{"mexc", "gate", "bingx"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("want %v, got %v", want, got)
		}
	})

	t.Run("empty input returns nil", func(t *testing.T) {
		got := rankExchangeEntries(nil)
		if len(got) != 0 {
			t.Errorf("want empty, got %v", got)
		}
	})

	t.Run("all unsupported returns empty", func(t *testing.T) {
		entries := []exchangeEntry{
			{Exchange: "huobi", Volume24hUSD: fptr(1_000_000)},
			{Exchange: "kucoin", Volume24hUSD: fptr(500_000)},
		}
		got := rankExchangeEntries(entries)
		if len(got) != 0 {
			t.Errorf("want empty, got %v", got)
		}
	})

	t.Run("single supported exchange", func(t *testing.T) {
		entries := []exchangeEntry{
			{Exchange: "bingx", Volume24hUSD: fptr(800_000)},
			{Exchange: "huobi", Volume24hUSD: fptr(5_000_000)},
		}
		got := rankExchangeEntries(entries)
		want := []string{"bingx"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("want %v, got %v", want, got)
		}
	})

	t.Run("equal volumes use deterministic priority tie-breaker", func(t *testing.T) {
		// Nil volume simulates DB fallback where no volume info is available.
		entries := []exchangeEntry{
			{Exchange: "mexc"},
			{Exchange: "binance"},
			{Exchange: "bingx"},
			{Exchange: "gate"},
		}
		got := rankExchangeEntries(entries)
		want := []string{"binance", "gate", "bingx", "mexc"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("want %v, got %v", want, got)
		}
	})

	t.Run("XT is available for charts", func(t *testing.T) {
		entries := []exchangeEntry{{Exchange: "xt", Volume24hUSD: fptr(1_000_000)}}
		got := rankExchangeEntries(entries)
		want := []string{"xt"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("want %v, got %v", want, got)
		}
	})

	t.Run("LBank is available for charts", func(t *testing.T) {
		entries := []exchangeEntry{{Exchange: "lbank", Volume24hUSD: fptr(1_000_000)}}
		got := rankExchangeEntries(entries)
		want := []string{"lbank"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("want %v, got %v", want, got)
		}
	})
}

// TestRankedExchangesCarriesMarketIDFromLiveSnapshot confirms the live-
// snapshot path threads each exchange's captured market_id through to
// exchangeCandidate -- this is what lets fetchOHLCV request the exact
// instrument instead of guessing a symbol from base (2026-08-28 production
// report: guessing broke bingx OHLCV for TRUMP).
func TestRankedExchangesCarriesMarketIDFromLiveSnapshot(t *testing.T) {
	t.Parallel()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	payload, err := json.Marshal(pumpsPayload{
		Count: 1,
		Pumps: []pumpEntry{{
			Base: "TRUMP",
			Exchanges: []exchangeEntry{
				{Exchange: "bingx", MarketID: "TRUMPSOL-USDT", Volume24hUSD: fptr(1_000_000)},
			},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := rdb.Set(context.Background(), "pumps:latest", payload, 0).Err(); err != nil {
		t.Fatal(err)
	}

	h := &Handler{rdb: rdb}
	got := h.rankedExchanges(context.Background(), "TRUMP")
	want := []exchangeCandidate{{Exchange: "bingx", MarketID: "TRUMPSOL-USDT"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("rankedExchanges = %+v, want %+v", got, want)
	}
}

// TestRankedExchangesCarriesMarketIDFromDBFallback covers the same wiring
// through the DB fallback path (base not in the live snapshot).
func TestRankedExchangesCarriesMarketIDFromDBFallback(t *testing.T) {
	t.Parallel()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	// No pumps:latest key -- forces the DB fallback path.

	exchangesJSON := []byte(`[{"exchange":"gate","market_id":"HFT_USDT"}]`)
	pool := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{exchangesJSON}}
		},
	}

	h := &Handler{rdb: rdb, pool: pool}
	got := h.rankedExchanges(context.Background(), "HFT")
	want := []exchangeCandidate{{Exchange: "gate", MarketID: "HFT_USDT"}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("rankedExchanges = %+v, want %+v", got, want)
	}
}

func TestExchangeEntryPreservesUnavailableVolumeMetadata(t *testing.T) {
	raw := []byte(`{
		"exchange": "lbank",
		"volume_24h_usd": null,
		"volume_24h_source": "unavailable",
		"ticker_timestamp_ms": 1800000000000,
		"observed_at_ms": 1800000001000
	}`)
	var entry exchangeEntry

	if err := json.Unmarshal(raw, &entry); err != nil {
		t.Fatal(err)
	}

	if entry.Volume24hUSD != nil {
		t.Errorf("volume = %v, want nil", *entry.Volume24hUSD)
	}
	if entry.Volume24hSource != "unavailable" {
		t.Errorf("source = %q, want unavailable", entry.Volume24hSource)
	}
	if entry.TickerTimestampMS == nil || *entry.TickerTimestampMS != 1_800_000_000_000 {
		t.Errorf("timestamp = %v, want 1800000000000", entry.TickerTimestampMS)
	}
	if entry.ObservedAtMS == nil || *entry.ObservedAtMS != 1_800_000_001_000 {
		t.Errorf("observed_at = %v, want 1800000001000", entry.ObservedAtMS)
	}
}

func TestHistoryEntryExposesExplicitPeakSemantics(t *testing.T) {
	raw, err := json.Marshal(historyEntry{
		PeakPct:            90,
		Exchange24hHighPct: 90,
		ObservedPeakPct:    68,
	})
	if err != nil {
		t.Fatal(err)
	}

	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatal(err)
	}
	if payload["peak_pct"] != 90.0 {
		t.Errorf("legacy peak_pct = %v, want 90", payload["peak_pct"])
	}
	if payload["exchange_24h_high_pct"] != 90.0 {
		t.Errorf("exchange_24h_high_pct = %v, want 90", payload["exchange_24h_high_pct"])
	}
	if payload["observed_peak_pct"] != 68.0 {
		t.Errorf("observed_peak_pct = %v, want 68", payload["observed_peak_pct"])
	}
}

// ---- TestCacheSignals ----

func TestCacheSignals(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })

	firstSeenAt := int64(1_700_000_000)

	makeQuerier := func(episodeErr error) pgxPool {
		var call int
		return &stubQuerier{
			onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
				call++
				switch call {
				case 1: // episode
					if episodeErr != nil {
						return &stubRow{err: episodeErr}
					}
					return &stubRow{
						vals: []any{int64(42), firstSeenAt, ptr(firstSeenAt), 60.0, 58.0},
					}
				case 2: // current OI
					return &stubRow{vals: []any{float64(5_000_000), int64(3)}}
				case 3: // baseline OI
					return &stubRow{vals: []any{float64(4_000_000), int64(3)}}
				case 4: // max funding
					return &stubRow{vals: []any{float64(0.003), int64(2)}}
				default:
					return &stubRow{err: fmt.Errorf("unexpected call %d", call)}
				}
			},
		}
	}

	t.Run("writes signals key to Redis", func(t *testing.T) {
		h := &Handler{rdb: rdb, pool: makeQuerier(nil)}
		if err := h.CacheSignals(context.Background(), "BEAT"); err != nil {
			t.Fatalf("CacheSignals: %v", err)
		}
		raw, err := rdb.Get(context.Background(), "signals:BEAT").Bytes()
		if err != nil {
			t.Fatalf("signals:BEAT not found in Redis: %v", err)
		}
		var resp signalsResponse
		if err := json.Unmarshal(raw, &resp); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		if resp.Base != "BEAT" {
			t.Errorf("base = %q, want BEAT", resp.Base)
		}
		if resp.Score < 0 || resp.Score > signalMaxScore {
			t.Errorf("score %d out of range [0, %d]", resp.Score, signalMaxScore)
		}
	})

	t.Run("writes signals key for Unicode base", func(t *testing.T) {
		mr.FlushAll()
		const base = "草根文化"
		h := &Handler{rdb: rdb, pool: makeQuerier(nil)}
		if err := h.CacheSignals(context.Background(), base); err != nil {
			t.Fatalf("CacheSignals: %v", err)
		}
		raw, err := rdb.Get(context.Background(), "signals:"+base).Bytes()
		if err != nil {
			t.Fatalf("Unicode signals key not found: %v", err)
		}
		var resp signalsResponse
		if err := json.Unmarshal(raw, &resp); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		if resp.Base != base {
			t.Errorf("base = %q, want %q", resp.Base, base)
		}
	})

	t.Run("deletes stale key when no open episode", func(t *testing.T) {
		mr.FlushAll()
		// Seed a stale entry to confirm it gets removed.
		_ = rdb.Set(context.Background(), "signals:BEAT", []byte(`{"score":8}`), signalsCacheTTL)
		h := &Handler{rdb: rdb, pool: makeQuerier(pgx.ErrNoRows)}
		if err := h.CacheSignals(context.Background(), "BEAT"); err != nil {
			t.Fatalf("CacheSignals: %v", err)
		}
		if mr.Exists("signals:BEAT") {
			t.Error("stale signals:BEAT should have been deleted when no open episode")
		}
	})
}

func TestComputeSignalsUsesQualifiedAnchorForOIBaseline(t *testing.T) {
	const firstSeenAt = int64(1_799_999_000)
	const qualifiedAt = int64(1_800_000_000)
	var call int
	pool := &stubQuerier{
		onQueryRow: func(_ context.Context, query string, args ...any) pgxRow {
			call++
			switch call {
			case 1:
				if strings.Contains(query, "COALESCE(entry_qualified_at, first_seen_at)") {
					t.Fatalf("episode query aliases the strategy anchor as first_seen_at: %s", query)
				}
				return &stubRow{
					vals: []any{int64(42), firstSeenAt, ptr(qualifiedAt), 60.0, 58.0},
				}
			case 2:
				return &stubRow{vals: []any{float64(5_000_000), int64(1)}}
			case 3:
				if !strings.Contains(query, "recorded_at >= to_timestamp($2)") {
					t.Fatalf("baseline query is not bounded by the strategy anchor: %s", query)
				}
				if len(args) != 2 || args[0] != int64(42) || args[1] != qualifiedAt {
					t.Fatalf("unexpected baseline args: %v", args)
				}
				return &stubRow{vals: []any{float64(4_000_000), int64(1)}}
			case 4:
				return &stubRow{vals: []any{float64(0.003), int64(1)}}
			default:
				return &stubRow{err: fmt.Errorf("unexpected call %d", call)}
			}
		},
	}

	response, notFound, err := (&Handler{pool: pool}).computeSignals(
		context.Background(),
		"BEAT",
	)
	if err != nil || notFound {
		t.Fatalf("computeSignals() notFound=%v err=%v", notFound, err)
	}
	if response.Episode.FirstSeenAt != firstSeenAt {
		t.Fatalf("first_seen_at = %d, want %d", response.Episode.FirstSeenAt, firstSeenAt)
	}
	if response.Episode.StrategyAnchorAt != qualifiedAt {
		t.Fatalf(
			"strategy_anchor_at = %d, want %d",
			response.Episode.StrategyAnchorAt,
			qualifiedAt,
		)
	}
}

func TestSignalStrategyAnchorAt(t *testing.T) {
	const firstSeenAt = int64(1_799_999_000)
	const qualifiedAt = int64(1_800_000_000)

	if got := signalStrategyAnchorAt(firstSeenAt, nil); got != firstSeenAt {
		t.Fatalf("measurement anchor = %d, want %d", got, firstSeenAt)
	}
	if got := signalStrategyAnchorAt(firstSeenAt, ptr(qualifiedAt)); got != qualifiedAt {
		t.Fatalf("qualified anchor = %d, want %d", got, qualifiedAt)
	}
}

// ---- MomentumWatch ----

// serveMomentumWatch routes a GET request through chi and returns the recorder.
func serveMomentumWatch(q pgxPool) *httptest.ResponseRecorder {
	h := &Handler{pool: q}
	r := chi.NewRouter()
	r.Get("/api/pumps/momentum-watch", h.MomentumWatch)
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/momentum-watch", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

// momentumWatchRowVals mirrors the Scan order in momentumWatchQuery.
func momentumWatchRowVals(symbol, exchange string, clearStreak int) []any {
	return []any{
		exchange, "linear", symbol, "d72ca3bc-3f28-573b-a7f5-2d1978055870",
		int64(1_800_000_000), int64(1_800_000_500), clearStreak, int64(1_800_000_500),
		(*float64)(nil), (*float64)(nil), (*float64)(nil),
		(*float64)(nil), (*float64)(nil), (*float64)(nil),
	}
}

func TestMomentumWatchReturnsEmptyWhenNoActiveEpisodes(t *testing.T) {
	q := &stubQuerier{
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return &stubRows{}, nil
		},
	}
	w := serveMomentumWatch(q)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	var resp momentumWatchResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if resp.Count != 0 || len(resp.Watch) != 0 {
		t.Errorf("want empty watch list, got %+v", resp)
	}
}

func TestMomentumWatchReturnsActiveEpisodes(t *testing.T) {
	q := &stubQuerier{
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return &stubRows{cols: [][]any{
				momentumWatchRowVals("GPSUSDT", "bybit", 2),
			}}, nil
		},
	}
	w := serveMomentumWatch(q)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	var resp momentumWatchResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if resp.Count != 1 || len(resp.Watch) != 1 {
		t.Fatalf("want 1 entry, got %+v", resp)
	}
	e := resp.Watch[0]
	if e.Symbol != "GPSUSDT" || e.Exchange != "bybit" || e.ClearStreak != 2 {
		t.Errorf("entry = %+v, want symbol=GPSUSDT exchange=bybit clear_streak=2", e)
	}
	if e.EpisodeID != "d72ca3bc-3f28-573b-a7f5-2d1978055870" {
		t.Errorf("episode_id = %q, unexpected", e.EpisodeID)
	}
}

// TestMomentumWatchQueryOnlySelectsActiveEpisodes is a regression: this
// endpoint must never surface rejected/suppressed/cleared WATCH evaluations
// as if they were live prospective longs, only app.momentum_flow_watch_states
// rows still flagged active_episode.
func TestMomentumWatchQueryOnlySelectsActiveEpisodes(t *testing.T) {
	if !strings.Contains(momentumWatchQuery, "WHERE s.active_episode") {
		t.Error("momentumWatchQuery must filter to active_episode = true")
	}
}

// TestMomentumWatchQueryNeverTouchesPumpEventsTables is a regression for the
// same never-merge-the-underlying-tables rule already enforced on the trades
// page's combinedTradesCTE: this endpoint reads only momentum_flow's own
// tables and must never join or union against app.pump_events / app.trades.
func TestMomentumWatchQueryNeverTouchesPumpEventsTables(t *testing.T) {
	if strings.Contains(momentumWatchQuery, "pump_events") || strings.Contains(momentumWatchQuery, "app.trades") {
		t.Error("momentumWatchQuery must stay isolated to momentum_flow's own tables")
	}
}

// TestMomentumWatchQueryFallsBackFirstWatchToLastWatch is a regression for a
// production incident (2026-08-16): an episode reactivated via the
// evaluator's own suppressed_cooldown path has no decision_status='watch'
// row at all for its current episode_id, so the first-watch lateral
// subquery's own min() was NULL -- scanning NULL into Go's non-pointer
// int64 FirstWatchAt crashed the whole endpoint the moment such an episode
// (TAOUSDT, confirmed directly against production data) was active. This is
// a lightweight text check (this package has no real-Postgres integration
// harness, see the stub-only convention above); the fix itself was verified
// by running the full query directly against production before shipping.
func TestMomentumWatchQueryFallsBackFirstWatchToLastWatch(t *testing.T) {
	if !strings.Contains(momentumWatchQuery, "coalesce(min(e2.bucket_start), s.last_watch_at)") {
		t.Error("first_watch_at must fall back to s.last_watch_at instead of staying NULL")
	}
}

package pumps

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

func ptr(v int64) *int64 { return &v }

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

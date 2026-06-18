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
		return []any{id, firstSeenAt, peakPct, lastPct}
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
			{Exchange: "mexc", Volume24hUSD: 1_000_000},
			{Exchange: "gate", Volume24hUSD: 900_000},
			{Exchange: "huobi", Volume24hUSD: 2_000_000}, // not in supportedOHLCV
			{Exchange: "bingx", Volume24hUSD: 500_000},
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
			{Exchange: "huobi", Volume24hUSD: 1_000_000},
			{Exchange: "kucoin", Volume24hUSD: 500_000},
		}
		got := rankExchangeEntries(entries)
		if len(got) != 0 {
			t.Errorf("want empty, got %v", got)
		}
	})

	t.Run("single supported exchange", func(t *testing.T) {
		entries := []exchangeEntry{
			{Exchange: "bingx", Volume24hUSD: 800_000},
			{Exchange: "huobi", Volume24hUSD: 5_000_000},
		}
		got := rankExchangeEntries(entries)
		want := []string{"bingx"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("want %v, got %v", want, got)
		}
	})

	t.Run("equal volumes use deterministic priority tie-breaker", func(t *testing.T) {
		// All volume=0 simulates DB fallback where no volume info is available.
		entries := []exchangeEntry{
			{Exchange: "mexc", Volume24hUSD: 0},
			{Exchange: "binance", Volume24hUSD: 0},
			{Exchange: "bingx", Volume24hUSD: 0},
			{Exchange: "gate", Volume24hUSD: 0},
		}
		got := rankExchangeEntries(entries)
		want := []string{"binance", "gate", "bingx", "mexc"}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("want %v, got %v", want, got)
		}
	})
}

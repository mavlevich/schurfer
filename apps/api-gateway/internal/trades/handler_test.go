package trades

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ---- stub infrastructure (mirrors pumps/handler_test.go pattern) ----

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

func serveTrades(q pgxPool, target string) *httptest.ResponseRecorder {
	h := &Handler{pool: q}
	req := httptest.NewRequest(http.MethodGet, target, nil)
	w := httptest.NewRecorder()
	h.List(w, req)
	return w
}

func serveStats(q pgxPool, target string) *httptest.ResponseRecorder {
	h := &Handler{pool: q}
	req := httptest.NewRequest(http.MethodGet, target, nil)
	w := httptest.NewRecorder()
	h.Stats(w, req)
	return w
}

func TestComputeStatsBasic(t *testing.T) {
	s := computeStats(statsAgg{
		Gross: tradeAgg{
			N: 4, Wins: 2, Losses: 2,
			SumPct: 10, SumWinPct: 30, SumLossPct: -20,
			TotalUSD: 50, WinningUSD: 150, LosingUSD: -100,
		},
		Net: tradeAgg{
			N: 2, Wins: 1, Losses: 1,
			SumPct: 2, SumWinPct: 5, SumLossPct: -3,
			TotalUSD: 10, WinningUSD: 25, LosingUSD: -15,
		},
		NetSubsetGrossUSD:    18,
		NetSubsetGrossSumPct: 6,
		LegacyCount:          2,
	})
	if s.WinRate != 50 {
		t.Errorf("win_rate: want 50, got %v", s.WinRate)
	}
	if s.Expectancy != 2.5 {
		t.Errorf("expectancy: want 2.5, got %v", s.Expectancy)
	}
	if s.AvgWin != 15 || s.AvgLoss != -10 {
		t.Errorf("avg win/loss: want 15/-10, got %v/%v", s.AvgWin, s.AvgLoss)
	}
	if s.ProfitFactor == nil || *s.ProfitFactor != 1.5 {
		t.Errorf("profit_factor: want 1.5, got %v", s.ProfitFactor)
	}
	if s.GrossUSD != 50 {
		t.Errorf("gross_usd: want 50, got %v", s.GrossUSD)
	}
	if s.NetCount != 2 || s.NetUSD == nil || *s.NetUSD != 10 {
		t.Errorf("net stats: want count=2 usd=10, got %d/%v", s.NetCount, s.NetUSD)
	}
	// NetSubsetGrossUSD/Pct must be the gross figures for the same 2 trades NetUSD
	// covers (18 / 2 = 3 pct avg), not GrossUSD's own 4-trade total (50) -- GrossUSD
	// and NetUSD are never directly comparable, only NetSubsetGrossUSD and NetUSD are.
	if s.NetSubsetGrossUSD == nil || *s.NetSubsetGrossUSD != 18 {
		t.Errorf("net_subset_gross_usd: want 18, got %v", s.NetSubsetGrossUSD)
	}
	if s.NetSubsetGrossPct == nil || *s.NetSubsetGrossPct != 3 {
		t.Errorf("net_subset_gross_pct: want 3 (6/2), got %v", s.NetSubsetGrossPct)
	}
}

func TestComputeStatsProfitFactorUsesDollarsNotPercent(t *testing.T) {
	// PF must be gross-$ won / gross-$ lost, independent of the percent sums (which
	// are wrong once position sizes differ). Here the percents would give a different
	// ratio than the dollars; PF must follow the dollars.
	s := computeStats(statsAgg{Gross: tradeAgg{
		N: 3, Wins: 1, Losses: 2,
		SumWinPct: 5, SumLossPct: -40,
		WinningUSD: 100, LosingUSD: -40,
	}})
	if s.ProfitFactor == nil || *s.ProfitFactor != 2.5 {
		t.Errorf("profit_factor: want 2.5 (100/40), got %v", s.ProfitFactor)
	}
}

func TestComputeStatsNoLossesHasNilProfitFactor(t *testing.T) {
	s := computeStats(statsAgg{Gross: tradeAgg{N: 2, Wins: 2, WinningUSD: 20}})
	if s.ProfitFactor != nil {
		t.Errorf("profit_factor: want nil with no losses, got %v", *s.ProfitFactor)
	}
}

func TestComputeStatsEmpty(t *testing.T) {
	s := computeStats(statsAgg{})
	if s.Count != 0 || s.WinRate != 0 || s.Expectancy != 0 ||
		s.ProfitFactor != nil || s.NetUSD != nil {
		t.Errorf("empty stats not zeroed: %+v", s)
	}
}

func TestComputeStatsWithholdsNetWhenOnlyLegacyRowsExist(t *testing.T) {
	s := computeStats(statsAgg{
		Gross:       tradeAgg{N: 30, TotalUSD: 8.71},
		LegacyCount: 30,
	})
	if s.NetCount != 0 || s.NetUSD != nil || s.NetExpectancy != nil {
		t.Errorf("legacy rows must not fabricate net stats: %+v", s)
	}
	if s.NetSubsetGrossUSD != nil || s.NetSubsetGrossPct != nil {
		t.Errorf("net_subset_gross must stay nil alongside net stats when net_count=0: %+v", s)
	}
}

// TestComputeStatsNetSubsetGrossIsolatesRealCostErosion is a regression for a real
// production reading (2026-08-23, app.trades, all closed): GrossUSD (+$102.93 over
// 385 trades) and NetUSD (-$30.22 over only 170 trades with complete accounting)
// looked contradictory side by side purely because they cover different, differently
// sized trade populations -- 215 of the 385 gross trades have no cost accounting at
// all and cannot appear in Net. NetSubsetGrossUSD isolates the SAME 170 trades' own
// gross figure (+$0.70) so the UI can show that the accounted-for subset was already
// roughly flat gross, and real per-trade costs (~$0.18-0.23) are what pushed it to a
// net loss -- not a mysterious mismatch between two unrelated numbers.
func TestComputeStatsNetSubsetGrossIsolatesRealCostErosion(t *testing.T) {
	s := computeStats(statsAgg{
		Gross:                tradeAgg{N: 385, TotalUSD: 102.93},
		Net:                  tradeAgg{N: 170, TotalUSD: -30.22},
		NetSubsetGrossUSD:    0.70,
		NetSubsetGrossSumPct: 119, // arbitrary pct sum for this test's N=170
		LegacyCount:          32,
		IncompleteCount:      183,
	})
	if s.GrossUSD != 102.93 {
		t.Errorf("gross_usd: want the full-population 102.93, got %v", s.GrossUSD)
	}
	if s.NetUSD == nil || *s.NetUSD != -30.22 {
		t.Errorf("net_usd: want -30.22, got %v", s.NetUSD)
	}
	if s.NetSubsetGrossUSD == nil || *s.NetSubsetGrossUSD != 0.70 {
		t.Errorf("net_subset_gross_usd: want the same-170-trades figure 0.70, got %v", s.NetSubsetGrossUSD)
	}
}

func TestStatsAppliesExchangeFilter(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, args ...any) pgxRow {
			capturedArgs = args
			return &stubRow{vals: []any{
				int64(0), int64(0), int64(0),
				float64(0), float64(0), float64(0),
				float64(0), float64(0), float64(0),
				int64(0), int64(0), int64(0),
				float64(0), float64(0), float64(0),
				float64(0), float64(0), float64(0),
				int64(0), int64(0),
				float64(0), float64(0),
			}}
		},
	}
	serveStats(q, "/api/trades/stats?exchange=bybit")
	if len(capturedArgs) != 1 || capturedArgs[0] != "bybit" {
		t.Errorf("want exchange arg [bybit] passed to SQL, got %v", capturedArgs)
	}
}

func TestStatsHandlerReturnsAggregate(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{
				int64(4), int64(2), int64(2),
				float64(10), float64(30), float64(-20),
				float64(50), float64(150), float64(-100),
				int64(2), int64(1), int64(1),
				float64(2), float64(5), float64(-3),
				float64(10), float64(25), float64(-15),
				int64(2), int64(0),
				float64(18), float64(6),
			}}
		},
	}
	w := serveStats(q, "/api/trades/stats")
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	var resp statsResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if resp.Count != 4 || resp.WinRate != 50 {
		t.Errorf("want count=4 win_rate=50, got %d/%v", resp.Count, resp.WinRate)
	}
	if resp.ProfitFactor == nil || *resp.ProfitFactor != 1.5 {
		t.Errorf("want profit_factor 1.5, got %v", resp.ProfitFactor)
	}
	if resp.GrossUSD != 50 {
		t.Errorf("want gross_usd 50, got %v", resp.GrossUSD)
	}
	if resp.NetUSD == nil || *resp.NetUSD != 10 {
		t.Errorf("want net_usd 10, got %v", resp.NetUSD)
	}
	if resp.NetSubsetGrossUSD == nil || *resp.NetSubsetGrossUSD != 18 {
		t.Errorf("want net_subset_gross_usd 18, got %v", resp.NetSubsetGrossUSD)
	}
}

var epoch = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

func strPtr(s string) *string { return &s }

// tradeRowVals returns a slice matching the Scan order in handler.go.
func tradeRowVals(id int64, status, exchange string) []any {
	return []any{
		"pump_short:" + strconv.FormatInt(id, 10), "app.trades", "pump_short_v1", "pump_short", "1", "live", strPtr("initial_sl"), "BEAT/USDT:USDT", exchange, "perp", "short",
		float64(50), float64(3),
		float64(0.0030), epoch,
		(*float64)(nil), (*time.Time)(nil),
		(*float64)(nil), (*float64)(nil),
		float64(0), float64(0), (*float64)(nil),
		(*float64)(nil), (*float64)(nil),
		(*float64)(nil), (*float64)(nil),
		(*float64)(nil), (*float64)(nil),
		"legacy_price_only_v1", "legacy", (*string)(nil),
		status, (*string)(nil),
		json.RawMessage(`{"score":8}`), (*string)(nil), epoch,
	}
}

// ---- tests ----

func TestListReturnsEmptyWhenNoTrades(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	w := serveTrades(q, "/api/trades")
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	var resp listResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if resp.Total != 0 {
		t.Errorf("want total=0, got %d", resp.Total)
	}
	if len(resp.Trades) != 0 {
		t.Errorf("want 0 trades, got %d", len(resp.Trades))
	}
}

func TestListReturnsTrades(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(2)}}
		},
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return &stubRows{cols: [][]any{
				tradeRowVals(1, "open", "bybit"),
				tradeRowVals(2, "closed", "bingx"),
			}}, nil
		},
	}
	w := serveTrades(q, "/api/trades")
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	var resp listResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if resp.Total != 2 {
		t.Errorf("want total=2, got %d", resp.Total)
	}
	if len(resp.Trades) != 2 {
		t.Fatalf("want 2 trades, got %d", len(resp.Trades))
	}
	if resp.Trades[0].Exchange != "bybit" {
		t.Errorf("want exchange=bybit, got %s", resp.Trades[0].Exchange)
	}
}

func TestListDefaultPagination(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	w := serveTrades(q, "/api/trades")
	var resp listResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if resp.Limit != defaultLimit {
		t.Errorf("want limit=%d, got %d", defaultLimit, resp.Limit)
	}
	if resp.Offset != 0 {
		t.Errorf("want offset=0, got %d", resp.Offset)
	}
}

func TestListClampLimit(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	w := serveTrades(q, "/api/trades?limit=9999")
	var resp listResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if resp.Limit != maxLimit {
		t.Errorf("want limit clamped to %d, got %d", maxLimit, resp.Limit)
	}
}

func TestListStatusFilterPassedToSQL(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, args ...any) pgxRow {
			capturedArgs = args
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	serveTrades(q, "/api/trades?status=closed")
	if len(capturedArgs) == 0 {
		t.Fatal("expected args to be passed to count query")
	}
	if capturedArgs[0] != "closed" {
		t.Errorf("want first arg=closed, got %v", capturedArgs[0])
	}
}

func TestListExchangeFilterPassedToSQL(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, args ...any) pgxRow {
			capturedArgs = args
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	serveTrades(q, "/api/trades?exchange=bybit")
	if len(capturedArgs) == 0 {
		t.Fatal("expected args to be passed to count query")
	}
	if capturedArgs[0] != "bybit" {
		t.Errorf("want first arg=bybit, got %v", capturedArgs[0])
	}
}

func TestListBothFiltersPassedToSQL(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, args ...any) pgxRow {
			capturedArgs = args
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	serveTrades(q, "/api/trades?status=closed&exchange=bybit")
	if len(capturedArgs) != 2 {
		t.Fatalf("want 2 args (status+exchange), got %d: %v", len(capturedArgs), capturedArgs)
	}
	if capturedArgs[0] != "closed" {
		t.Errorf("want args[0]=closed, got %v", capturedArgs[0])
	}
	if capturedArgs[1] != "bybit" {
		t.Errorf("want args[1]=bybit, got %v", capturedArgs[1])
	}
}

// TestListStrategyModeSideFiltersPassedToSQLExactlyOnce is a regression for
// the strategy/mode/side filter blocks having been accidentally duplicated
// in List's own WHERE-building code: each filter matched (and its SQL
// placeholder was allocated) twice, so a request combining all three ended
// up with 6 args instead of 3 and a WHERE clause repeating each condition.
// It happened to still filter correctly (a repeated `AND x = $n` is a
// no-op), but every filter must be applied exactly once.
func TestListStrategyModeSideFiltersPassedToSQLExactlyOnce(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, args ...any) pgxRow {
			capturedArgs = args
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	serveTrades(q, "/api/trades?strategy=early_momentum&mode=paper&side=long")
	if len(capturedArgs) != 3 {
		t.Fatalf("want 3 args (strategy+mode+side, each once), got %d: %v", len(capturedArgs), capturedArgs)
	}
	if capturedArgs[0] != "early_momentum" || capturedArgs[1] != "paper" || capturedArgs[2] != "long" {
		t.Errorf("want args=[early_momentum paper long], got %v", capturedArgs)
	}
}

// TestStatsStrategyModeSideFiltersPassedToSQLExactlyOnce mirrors the List
// regression above for Stats, which had the identical duplication.
func TestStatsStrategyModeSideFiltersPassedToSQLExactlyOnce(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, args ...any) pgxRow {
			capturedArgs = args
			return &stubRow{vals: []any{
				int64(0), int64(0), int64(0),
				float64(0), float64(0), float64(0),
				float64(0), float64(0), float64(0),
				int64(0), int64(0), int64(0),
				float64(0), float64(0), float64(0),
				float64(0), float64(0), float64(0),
				int64(0), int64(0),
				float64(0), float64(0),
			}}
		},
	}
	serveStats(q, "/api/trades/stats?strategy=pump_short&mode=live&side=short")
	if len(capturedArgs) != 3 {
		t.Fatalf("want 3 args (strategy+mode+side, each once), got %d: %v", len(capturedArgs), capturedArgs)
	}
	if capturedArgs[0] != "pump_short" || capturedArgs[1] != "live" || capturedArgs[2] != "short" {
		t.Errorf("want args=[pump_short live short], got %v", capturedArgs)
	}
}

// TestListNullExitReasonDoesNotCrash is a regression for the colleague-
// review finding that exit_reason was scanned into a non-pointer string:
// an open trade (or a still-open momentum_flow_paper probe) has NULL
// notes, and split_part(NULL, ' ', 1) is itself NULL, not ” -- scanning
// that NULL crashed the whole /api/trades endpoint. Reproduced for real
// against Postgres in TestCombinedTradesCTEAgainstRealPostgres; this is
// the equivalent fast stub-level check.
func TestListNullExitReasonDoesNotCrash(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(1)}}
		},
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			row := tradeRowVals(1, "open", "bybit")
			row[6] = (*string)(nil) // exit_reason: NULL, matching a still-open trade
			return &stubRows{cols: [][]any{row}}, nil
		},
	}
	w := serveTrades(q, "/api/trades")
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp listResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Trades) != 1 {
		t.Fatalf("want 1 trade, got %d", len(resp.Trades))
	}
	if resp.Trades[0].ExitReason != nil {
		t.Errorf("want ExitReason=nil for a NULL exit_reason, got %v", *resp.Trades[0].ExitReason)
	}
}

// TestCombinedTradesCTECoalescesNullableMomentumFlowCosts is a regression
// for a production incident (2026-08-16): app.momentum_flow_paper_probes'
// own fees_usd/funding_usd are nullable (NULL until that probe's own cost
// accounting completes, independent of entry_status='opened' -- a still-
// open probe legitimately has neither yet), but tradeRow.FeesUSD/
// FundingUSD are plain non-pointer float64 (matching app.trades' own
// NOT NULL columns). Without a fail-closed COALESCE on the momentum_flow
// side of the UNION, the very first still-open probe in a result page
// crashed the entire List query with "cannot scan NULL into *float64" --
// this is a lightweight text check (this package has no real-Postgres
// integration test harness, see handler_test.go's own stub-only
// convention) rather than a real scan reproduction; the fix itself was
// verified directly against production data before shipping.
func TestCombinedTradesCTECoalescesNullableMomentumFlowCosts(t *testing.T) {
	if !strings.Contains(combinedTradesCTE, "coalesce(p.fees_usd, 0)") {
		t.Error("momentum_flow_paper fees_usd must be coalesced to 0, not left nullable")
	}
	if !strings.Contains(combinedTradesCTE, "coalesce(p.funding_usd, 0)") {
		t.Error("momentum_flow_paper funding_usd must be coalesced to 0, not left nullable")
	}
}

func TestListOriginFilterPassedToSQL(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, args ...any) pgxRow {
			capturedArgs = args
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	serveTrades(q, "/api/trades?origin=momentum_flow_paper")
	if len(capturedArgs) != 1 || capturedArgs[0] != "momentum_flow_paper" {
		t.Errorf("want origin arg=[momentum_flow_paper], got %v", capturedArgs)
	}
}

// momentumFlowPaperRowVals mirrors tradeRowVals but for the momentum_flow_paper
// side of the UNION: a UUID-shaped string id, origin set, and no legacy
// pump-short-only fields (entry_slippage_bps/exit_slippage_bps/slippage_usd/
// pnl_usd/pnl_pct all NULL, per combinedTradesCTE's own doc comment on why
// those have no real equivalent on the paper side).
func momentumFlowPaperRowVals(paperID, status, exchange string) []any {
	return []any{
		"momentum_flow_paper:" + paperID, "momentum_flow_paper", "momentum_flow_v1", "momentum_flow", "1", "paper", (*string)(nil), "ERAUSDT", exchange, "linear", "long",
		float64(50), float64(1),
		float64(10.5), epoch,
		(*float64)(nil), (*time.Time)(nil),
		(*float64)(nil), (*float64)(nil),
		float64(0), float64(0), (*float64)(nil),
		(*float64)(nil), (*float64)(nil),
		(*float64)(nil), (*float64)(nil),
		(*float64)(nil), (*float64)(nil),
		"momentum_flow_paper_v1", "pending", (*string)(nil),
		status, (*string)(nil),
		json.RawMessage(`{}`), (*string)(nil), epoch,
	}
}

func TestListReturnsBothOriginsTaggedAndSortedTogether(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(2)}}
		},
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return &stubRows{cols: [][]any{
				tradeRowVals(1, "closed", "bybit"),
				momentumFlowPaperRowVals("11111111-1111-1111-1111-111111111111", "open", "binance"),
			}}, nil
		},
	}
	w := serveTrades(q, "/api/trades")
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	var resp listResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Trades) != 2 {
		t.Fatalf("want 2 trades, got %d", len(resp.Trades))
	}
	if resp.Trades[0].Origin != "app.trades" || resp.Trades[0].ID != "pump_short:1" {
		t.Errorf("trade 0 = %+v, want origin=app.trades id=pump_short:1", resp.Trades[0])
	}
	if resp.Trades[1].Origin != "momentum_flow_paper" ||
		resp.Trades[1].ID != "momentum_flow_paper:11111111-1111-1111-1111-111111111111" {
		t.Errorf("trade 1 = %+v, want origin=momentum_flow_paper with its own uuid-shaped id", resp.Trades[1])
	}
}

func TestListSetupContextIsRawJSON(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(1)}}
		},
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return &stubRows{cols: [][]any{tradeRowVals(1, "open", "bybit")}}, nil
		},
	}
	w := serveTrades(q, "/api/trades")
	var resp listResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	var ctx map[string]any
	if err := json.Unmarshal(resp.Trades[0].SetupContext, &ctx); err != nil {
		t.Errorf("setup_context is not valid JSON: %v", err)
	}
	if ctx["score"] != float64(8) {
		t.Errorf("want score=8, got %v", ctx["score"])
	}
}

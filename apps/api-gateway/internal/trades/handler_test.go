package trades

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
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

var epoch = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

// tradeRowVals returns a slice matching the Scan order in handler.go.
func tradeRowVals(id int64, status, exchange string) []any {
	return []any{
		id, "BEAT/USDT:USDT", exchange, "perp", "short",
		float64(50), float64(3),
		float64(0.0030), epoch,
		(*float64)(nil), (*time.Time)(nil),
		(*float64)(nil), (*float64)(nil),
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

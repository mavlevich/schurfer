package decisions

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

// ---- stub infrastructure (mirrors trades/handler_test.go pattern) ----

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

func serveDecisions(q pgxPool, target string) *httptest.ResponseRecorder {
	h := &Handler{pool: q}
	req := httptest.NewRequest(http.MethodGet, target, nil)
	w := httptest.NewRecorder()
	h.List(w, req)
	return w
}

var epoch = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

func decisionRowVals(id int64, base, action string) []any {
	score := 7
	pump := 45.5
	return []any{
		id, epoch, base, "bybit", action, "test reason", &score, &pump, epoch,
	}
}

// ---- tests ----

func TestListReturnsEmptyWhenNoDecisions(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	w := serveDecisions(q, "/api/decisions")
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
	if len(resp.Decisions) != 0 {
		t.Errorf("want 0 decisions, got %d", len(resp.Decisions))
	}
}

func TestListReturnsDecisions(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(2)}}
		},
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return &stubRows{cols: [][]any{
				decisionRowVals(1, "BEAT", "skipped"),
				decisionRowVals(2, "ACT", "opened"),
			}}, nil
		},
	}
	w := serveDecisions(q, "/api/decisions")
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
	if len(resp.Decisions) != 2 {
		t.Fatalf("want 2 decisions, got %d", len(resp.Decisions))
	}
	if resp.Decisions[0].Base != "BEAT" {
		t.Errorf("want base=BEAT, got %s", resp.Decisions[0].Base)
	}
	if resp.Decisions[0].Action != "skipped" {
		t.Errorf("want action=skipped, got %s", resp.Decisions[0].Action)
	}
}

func TestListDefaultPagination(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	w := serveDecisions(q, "/api/decisions")
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
	w := serveDecisions(q, "/api/decisions?limit=9999")
	var resp listResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if resp.Limit != maxLimit {
		t.Errorf("want limit clamped to %d, got %d", maxLimit, resp.Limit)
	}
}

func TestListBaseFilterPassedToSQL(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, args ...any) pgxRow {
			capturedArgs = args
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	serveDecisions(q, "/api/decisions?base=BEAT")
	if len(capturedArgs) == 0 {
		t.Fatal("expected args to be passed to count query")
	}
	if capturedArgs[0] != "BEAT" {
		t.Errorf("want first arg=BEAT, got %v", capturedArgs[0])
	}
}

func TestListActionFilterPassedToSQL(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, args ...any) pgxRow {
			capturedArgs = args
			return &stubRow{vals: []any{int64(0)}}
		},
	}
	serveDecisions(q, "/api/decisions?action=skipped")
	if len(capturedArgs) == 0 {
		t.Fatal("expected args to be passed to count query")
	}
	if capturedArgs[0] != "skipped" {
		t.Errorf("want first arg=skipped, got %v", capturedArgs[0])
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
	serveDecisions(q, "/api/decisions?base=BEAT&action=opened")
	if len(capturedArgs) != 2 {
		t.Fatalf("want 2 args (base+action), got %d: %v", len(capturedArgs), capturedArgs)
	}
	if capturedArgs[0] != "BEAT" {
		t.Errorf("want args[0]=BEAT, got %v", capturedArgs[0])
	}
	if capturedArgs[1] != "opened" {
		t.Errorf("want args[1]=opened, got %v", capturedArgs[1])
	}
}

func TestListScoreAndPumpPctReturned(t *testing.T) {
	q := &stubQuerier{
		onQueryRow: func(_ context.Context, _ string, _ ...any) pgxRow {
			return &stubRow{vals: []any{int64(1)}}
		},
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return &stubRows{cols: [][]any{decisionRowVals(1, "BEAT", "skipped")}}, nil
		},
	}
	w := serveDecisions(q, "/api/decisions")
	var resp listResponse
	if err := json.NewDecoder(w.Body).Decode(&resp); err != nil {
		t.Fatal(err)
	}
	if resp.Decisions[0].Score == nil || *resp.Decisions[0].Score != 7 {
		t.Errorf("want score=7, got %v", resp.Decisions[0].Score)
	}
	if resp.Decisions[0].PumpPct == nil || *resp.Decisions[0].PumpPct != 45.5 {
		t.Errorf("want pump_pct=45.5, got %v", resp.Decisions[0].PumpPct)
	}
}

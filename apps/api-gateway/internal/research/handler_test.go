package research

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/jackc/pgx/v5"
	"github.com/redis/go-redis/v9"
)

type stubRow struct {
	values []any
	err    error
}

func (r stubRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	for index, target := range dest {
		if index >= len(r.values) {
			break
		}
		if err := assign(target, r.values[index]); err != nil {
			return err
		}
	}
	return nil
}

func assign(dest, source any) error {
	target := reflect.ValueOf(dest)
	if target.Kind() != reflect.Ptr || target.IsNil() {
		return fmt.Errorf("destination must be a non-nil pointer")
	}
	target = target.Elem()
	if source == nil {
		target.Set(reflect.Zero(target.Type()))
		return nil
	}
	value := reflect.ValueOf(source)
	if value.Type().AssignableTo(target.Type()) {
		target.Set(value)
		return nil
	}
	if value.Type().ConvertibleTo(target.Type()) {
		target.Set(value.Convert(target.Type()))
		return nil
	}
	if target.Kind() == reflect.Ptr && value.Type().AssignableTo(target.Type().Elem()) {
		copy := reflect.New(target.Type().Elem())
		copy.Elem().Set(value)
		target.Set(copy)
		return nil
	}
	return fmt.Errorf("cannot assign %T to %T", source, dest)
}

type stubDB struct {
	rows  []stubRow
	calls []string
}

func (db *stubDB) QueryRow(_ context.Context, sql string, _ ...any) pgxRow {
	db.calls = append(db.calls, sql)
	row := db.rows[0]
	db.rows = db.rows[1:]
	return row
}

func testRedis(t *testing.T, fields map[string]string) (*redis.Client, func()) {
	t.Helper()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	if len(fields) > 0 {
		values := make(map[string]any, len(fields))
		for key, value := range fields {
			values[key] = value
		}
		if err := client.HSet(context.Background(), "market:orderflow:health", values).Err(); err != nil {
			t.Fatal(err)
		}
	}
	return client, func() { _ = client.Close() }
}

func serve(t *testing.T, handler *Handler) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(http.MethodGet, "/api/research/readiness", nil)
	response := httptest.NewRecorder()
	handler.Readiness(response, request)
	return response
}

func TestReadinessSeparatesExactAndOperationalProgress(t *testing.T) {
	now := time.Date(2026, time.July, 31, 12, 0, 0, 0, time.UTC)
	meanDelta := 0.17
	db := &stubDB{rows: []stubRow{
		{values: []any{16, 12, 8, 2, 41, 0, 1, 3}},
		{values: []any{
			liquidTakerContract,
			"liquid_taker_forward_report_v1",
			now.Add(-time.Hour),
			liquidTakerStart,
			now.Add(-2 * time.Hour),
			"abc123",
			false,
			strings.Repeat("a", 64),
			"collecting",
			"withheld",
			12,
			8,
			2,
		}},
		{values: []any{6, 6, 6, 6, meanDelta}},
	}}
	rdb, closeRedis := testRedis(t, map[string]string{
		"updated_at_ms":           strconv64(now.Add(-time.Second).UnixMilli()),
		"started_at_ms":           strconv64(orderflowStart.UnixMilli()),
		"status":                  "ok",
		"event_rate_per_sec":      "445.8",
		"activation_total":        "37",
		"active_captures":         "6",
		"records_persisted_total": "116137",
		"storage_bytes":           "12687769",
		"window_max_lag_ms":       "101",
		"queue_dropped_total":     "0",
		"pending_dropped_total":   "0",
		"persist_errors_total":    "0",
		"storage_limited_total":   "0",
	})
	defer closeRedis()

	handler := &Handler{db: db, redis: rdb, now: func() time.Time { return now }}
	response := serve(t, handler)
	if response.Code != http.StatusOK {
		t.Fatalf("status: got %d, body %s", response.Code, response.Body.String())
	}

	var payload Response
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if payload.Interpretation != "collection_progress_only_no_strategy_change" {
		t.Fatalf("unexpected interpretation: %s", payload.Interpretation)
	}
	if got := payload.ProspectiveCohorts[0].MatureInputEpisodes.Current; got != 12 {
		t.Fatalf("HYP-008 episodes: got %d", got)
	}
	if payload.ProspectiveCohorts[0].MatureInputEpisodes.Exact {
		t.Fatal("mature input count must not claim formal-report exactness")
	}
	diagnostics := payload.ProspectiveCohorts[0].InputDiagnostics
	if diagnostics.ClosedCandidateEpisodes != 16 {
		t.Fatalf("closed candidate episodes: got %d", diagnostics.ClosedCandidateEpisodes)
	}
	if diagnostics.IgnoredMeasurementDecisions != 41 {
		t.Fatalf("ignored measurement decisions: got %d", diagnostics.IgnoredMeasurementDecisions)
	}
	if diagnostics.UnexpectedStrategyEpisodes != 0 {
		t.Fatalf("unexpected strategy episodes: got %d", diagnostics.UnexpectedStrategyEpisodes)
	}
	latest := payload.ProspectiveCohorts[0].LatestReport
	if latest == nil || latest.EligibleEpisodes != 12 || latest.Verdict != "withheld" {
		t.Fatalf("latest registered report: got %#v", latest)
	}
	if got := payload.ProspectiveCohorts[1].Status; got != "scheduled" {
		t.Fatalf("HYP-010 status: got %s", got)
	}
	if got := payload.ExitLiquidity.ComparableObservations.Current; got != 6 {
		t.Fatalf("exit comparable count: got %d", got)
	}
	if !payload.ExitLiquidity.ComparableObservations.Exact {
		t.Fatal("exit calibration count must be exact")
	}
	if payload.Orderflow == nil {
		t.Fatal("orderflow progress missing")
	}
	if got := payload.Orderflow.CompletedWindowsEstimate.Current; got != 31 {
		t.Fatalf("completed windows estimate: got %d", got)
	}
	if payload.Orderflow.CompletedWindowsEstimate.Exact {
		t.Fatal("completed windows estimate must not claim report exactness")
	}
	if len(db.calls) != 3 {
		t.Fatalf("scheduled cohort should not query database, got %d calls", len(db.calls))
	}
}

func TestReadinessQueriesStartedWiderCohort(t *testing.T) {
	now := time.Date(2026, time.August, 30, 12, 0, 0, 0, time.UTC)
	db := &stubDB{rows: []stubRow{
		{values: []any{105, 101, 31, 5, 120, 0, 1, 3}},
		{err: pgx.ErrNoRows},
		{values: []any{102, 100, 30, 4, 110, 0, 0, 2}},
		{err: pgx.ErrNoRows},
		{values: []any{30, 29, 28, 20, nil}},
	}}
	rdb, closeRedis := testRedis(t, nil)
	defer closeRedis()

	handler := &Handler{db: db, redis: rdb, now: func() time.Time { return now }}
	response := serve(t, handler)
	if response.Code != http.StatusOK {
		t.Fatalf("status: got %d, body %s", response.Code, response.Body.String())
	}

	var payload Response
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	for _, item := range payload.ProspectiveCohorts {
		if item.Status != "report_required" {
			t.Fatalf("%s status: got %s", item.Key, item.Status)
		}
	}
	if payload.Orderflow != nil {
		t.Fatal("missing Redis health should produce null orderflow progress")
	}
	if len(db.calls) != 5 {
		t.Fatalf("started wider cohort should query database, got %d calls", len(db.calls))
	}
}

func TestReadinessFailsClosedOnDatabaseError(t *testing.T) {
	db := &stubDB{rows: []stubRow{{err: fmt.Errorf("database unavailable")}}}
	rdb, closeRedis := testRedis(t, nil)
	defer closeRedis()
	handler := &Handler{
		db:    db,
		redis: rdb,
		now: func() time.Time {
			return time.Date(2026, time.July, 31, 12, 0, 0, 0, time.UTC)
		},
	}
	response := serve(t, handler)
	if response.Code != http.StatusInternalServerError {
		t.Fatalf("status: got %d", response.Code)
	}
}

func TestQueriesPreserveResearchBoundaries(t *testing.T) {
	requiredCohortFragments := []string{
		"app.trade_decisions",
		"app.pump_events",
		"app.trade_decision_outcomes",
		"horizon_minutes = 480",
		"o.status = 'complete'",
		"d.strategy_version = 'pump_short_measurement_v1'",
		"coalesce(d.features @> '{\"measurement_only\": true}'::jsonb, false)",
		"WHERE NOT (",
	}
	for _, fragment := range requiredCohortFragments {
		if !strings.Contains(cohortProgressSQL, fragment) {
			t.Fatalf("cohort query missing %q", fragment)
		}
	}
	if !strings.Contains(exitLiquidityProgressSQL, "LEFT JOIN app.trade_exit_liquidity_observations") {
		t.Fatal("exit progress must retain missing observations in its denominator")
	}
	if !strings.Contains(exitLiquidityProgressSQL, "abs(extract(epoch") {
		t.Fatal("exit progress must enforce the registered quote-time skew")
	}
	if !strings.Contains(latestReportSQL, "app.research_report_runs") ||
		!strings.Contains(latestReportSQL, "ORDER BY generated_at DESC, id DESC") {
		t.Fatal("latest report query must use the append-only registry deterministically")
	}
}

func strconv64(value int64) string {
	return fmt.Sprintf("%d", value)
}

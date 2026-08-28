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
		"updated_at_ms":            strconv64(now.Add(-time.Second).UnixMilli()),
		"started_at_ms":            strconv64(orderflowStart.UnixMilli()),
		"status":                   "ok",
		"event_rate_per_sec":       "445.8",
		"activation_total":         "37",
		"active_captures":          "6",
		"records_persisted_total":  "116137",
		"storage_bytes":            "12687769",
		"window_max_lag_ms":        "101",
		"queue_dropped_total":      "0",
		"pending_dropped_total":    "0",
		"persist_errors_total":     "0",
		"storage_limited_total":    "0",
		"trade_reconnect_total":    "3",
		"trade_read_timeout_total": "2",
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
	if payload.Orderflow == nil || payload.Orderflow.TradeReconnectTotal != 3 ||
		payload.Orderflow.TradeReadTimeoutTotal != 2 {
		t.Fatalf("unexpected order-flow recovery telemetry: %+v", payload.Orderflow)
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
		{values: []any{
			120, 115, 110, 5, 0, 0, 0, 0, 0, 0, 105, 100, 31, 4, 80,
			20, 15, 0, 80, 20, 12, 8, "source_lead_identity_registry_v1",
			strings.Repeat("a", 64), false, now.Add(-time.Hour),
		}},
		{values: []any{105, 100, 3, 2, 900.0, 1_500.0, 4.0, 8.0, 2.0, 5.0}},
		{values: []any{105, 99, 4, 2, 1_000.0, 1_700.0, 5.0, 9.0, 2.5, 5.5}},
		{values: []any{`[]`}},
		{err: pgx.ErrNoRows},
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
	if payload.SourceLead.Status != "report_required" {
		t.Fatalf("source-lead status: got %s", payload.SourceLead.Status)
	}
	if !payload.SourceLead.MatureFourHourWindows.Exact {
		t.Fatal("source-lead database progress must be exact")
	}
	if len(db.calls) != 10 {
		t.Fatalf("started wider cohort should query database, got %d calls", len(db.calls))
	}
}

func TestSourceLeadProgressExposesOperationalFailures(t *testing.T) {
	now := time.Date(2026, time.August, 3, 12, 0, 0, 0, time.UTC)
	db := &stubDB{rows: []stubRow{
		{values: []any{
			12, 11, 9, 1, 2, 1, 1, 0, 1, 1, 8, 4, 6, 1, 3,
			2, 1, 1, 5, 1, 1, 1, "source_lead_identity_registry_v1",
			strings.Repeat("a", 64), false, now.Add(-time.Hour),
		}},
		{values: []any{8, 7, 0, 1, 800.0, 1_400.0, 3.0, 7.0, 1.5, 4.0}},
		{values: []any{8, 6, 1, 1, 900.0, 1_600.0, 4.0, 8.0, 2.0, 5.0}},
		{values: []any{`[{"base":"ABC","source_identity_key":"gate:swap:ABC_USDT:1","captures":2,"first_observed_at":"2026-08-02T01:00:00Z","last_observed_at":"2026-08-03T01:00:00Z","executable_targets":"binance,bybit","exact_target_identities":2,"source_conflict":false}]`}},
		{err: pgx.ErrNoRows},
	}}
	handler := &Handler{db: db, now: func() time.Time { return now }}

	progress, err := handler.sourceLeadProgress(context.Background(), now)
	if err != nil {
		t.Fatal(err)
	}
	if progress.Status != "unhealthy" {
		t.Fatalf("status: got %s", progress.Status)
	}
	if progress.TargetEligible.Current != 8 || !progress.TargetEligible.Exact {
		t.Fatalf("target eligibility: got %#v", progress.TargetEligible)
	}
	if progress.MatureFourHourWindows.Current != 4 {
		t.Fatalf("mature windows: got %d", progress.MatureFourHourWindows.Current)
	}
	if progress.Qualified != 2 || progress.QualifiedProspective != 1 || progress.QualificationMissing != 1 {
		t.Fatalf("qualification counts: got %#v", progress)
	}
	if !reflect.DeepEqual(
		progress.HealthFlags,
		[]string{"collecting_older_than_10m", "critical_capture_failure_last_24h"},
	) {
		t.Fatalf("health flags: got %#v", progress.HealthFlags)
	}
	if len(progress.Targets) != 2 || progress.Targets[0].SourceToQuoteP90MS == nil {
		t.Fatalf("target metrics: got %#v", progress.Targets)
	}
	if len(progress.IdentityReviewCandidates) != 1 ||
		progress.IdentityReviewCandidates[0].ExactTargetIdentities != 2 {
		t.Fatalf("identity review candidates: got %#v", progress.IdentityReviewCandidates)
	}
}

func TestSourceLeadStatusFailsClosedOnMixedRegistryContract(t *testing.T) {
	now := time.Date(2026, time.August, 3, 12, 0, 0, 0, time.UTC)
	progress := SourceLeadProgress{
		CohortStart:           sourceLeadStart,
		IdentityRegistryMixed: true,
	}

	if got := sourceLeadStatus(now, progress); got != "unhealthy" {
		t.Fatalf("status: got %s, want unhealthy", got)
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
	requiredSourceLeadFragments := []string{
		"app.source_lead_captures",
		"app.source_lead_qualifications",
		"source_first_observed_at >= $1",
		"capture_version = 'source_lead_prospective_capture_v1'",
		"interval '240 minutes'",
		"confirmation.first_seen_at <= captures.source_first_observed_at + interval '60 minutes'",
		"jsonb_typeof(t.liquidity->'ask_impact_bps') = 'number'",
		"identity_registry_fingerprint",
		"count(DISTINCT identity_registry_version)",
		"IS DISTINCT FROM (identity_registry_fingerprint IS NULL)",
		"exact_target_identities",
		"qualification.qualification_version = 'source_lead_qualified_capture_v2'",
		"AND source_first_observed_at >= $3",
		"t.status = 'sampled' AND t.identity_verified",
		"AND c.source_first_observed_at >= $3",
		"WHERE status = 'sampled' AND identity_verified",
		"AND source_first_observed_at >= $4 AND observed_at >= source_first_observed_at",
		"AND source_first_observed_at >= $4 AND spread_bps >= 0",
		"AND source_first_observed_at >= $4 AND entry_impact_bps >= 0",
	}
	sourceLeadQueries := sourceLeadProgressSQL + sourceLeadTargetProgressSQL +
		sourceLeadIdentityReviewSQL
	for _, fragment := range requiredSourceLeadFragments {
		if !strings.Contains(sourceLeadQueries, fragment) {
			t.Fatalf("source-lead query missing %q", fragment)
		}
	}
}

func strconv64(value int64) string {
	return fmt.Sprintf("%d", value)
}

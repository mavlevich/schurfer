package research

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
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

// stubDB routes each QueryRow call to a canned response keyed by the
// query's own identity: the SQL text itself, except for the two SQL
// constants shared by more than one call site (latestReportSQL, reused
// across three different contracts; sourceLeadTargetProgressSQL, reused
// across two exchanges), which are additionally keyed by the
// distinguishing bind argument -- see stubKey. Readiness's own sections
// now run concurrently (errgroup, fix/research-readiness-handler-
// concurrency-v1), so a plain FIFO queue keyed only by call order is no
// longer valid: goroutine scheduling order is not guaranteed, and a
// positional queue could hand a hyp_010 row to the hyp_008 caller, or
// panic on an out-of-range pop under -race. Mutex-protected for the same
// reason -- concurrent QueryRow calls now genuinely happen.
type stubDB struct {
	mu        sync.Mutex
	responses map[string][]stubRow
	calls     []string
}

// stubKey mirrors the args a caller passes closely enough to disambiguate
// every call site this package's handler actually has, without needing to
// know here which named Go function is calling -- the stub only ever sees
// (sql, args). Falls back to sql alone (rather than panicking or a static
// index) when a caller's own bug passes fewer args than the query expects
// -- QueryRow's own resulting "no queued response" error is a much more
// legible test failure than an index-out-of-range panic here.
func stubKey(sql string, args []any) string {
	switch {
	case sql == latestReportSQL && len(args) > 0:
		if contract, ok := args[0].(string); ok {
			return sql + ":" + contract // contract
		}
	case sql == sourceLeadTargetProgressSQL && len(args) > 2:
		if exchange, ok := args[2].(string); ok { //nolint:gosec // len(args) > 2 above guards this index
			return sql + ":" + exchange // exchange
		}
	}
	return sql
}

// latestReportKey and targetKey let tests build stubDB.responses without
// duplicating stubKey's own argument-position knowledge.
func latestReportKey(contract string) string { return stubKey(latestReportSQL, []any{contract}) }
func targetKey(exchange string) string {
	return stubKey(sourceLeadTargetProgressSQL, []any{nil, nil, exchange})
}

func (db *stubDB) QueryRow(_ context.Context, sql string, args ...any) pgxRow {
	db.mu.Lock()
	defer db.mu.Unlock()
	db.calls = append(db.calls, sql)
	key := stubKey(sql, args)
	queue := db.responses[key]
	if len(queue) == 0 {
		return stubRow{err: fmt.Errorf("stubDB: no queued response for %s", key)}
	}
	db.responses[key] = queue[1:]
	return queue[0]
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
	db := &stubDB{responses: map[string][]stubRow{
		latestReportKey(liquidTakerContract): {{values: []any{
			liquidTakerContract,
			"liquid_taker_forward_report_v1",
			now.Add(-time.Hour),
			liquidTakerStart,
			now.Add(-2 * time.Hour),
			"abc123",
			false,
			strings.Repeat("a", 64),
			"ready",
			"do_not_promote",
			802,
			296,
			5,
		}}},
		latestReportKey(liquidTakerWiderContract): {{err: pgx.ErrNoRows}},
		exitLiquidityProgressSQL:                  {{values: []any{6, 6, 6, 6, meanDelta}}},
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
	// hyp_008 and hyp_010 are permanently closed (do_not_promote, 2026-08-29):
	// their milestone counts come from the frozen formal-checkpoint constants,
	// not a live query, regardless of the handler's clock.
	if got := payload.ProspectiveCohorts[0].MatureInputEpisodes.Current; got != 494 {
		t.Fatalf("HYP-008 episodes: got %d", got)
	}
	if got := payload.ProspectiveCohorts[0].Status; got != "closed" {
		t.Fatalf("HYP-008 status: got %s", got)
	}
	if payload.Orderflow == nil || payload.Orderflow.TradeReconnectTotal != 3 ||
		payload.Orderflow.TradeReadTimeoutTotal != 2 {
		t.Fatalf("unexpected order-flow recovery telemetry: %+v", payload.Orderflow)
	}
	diagnostics := payload.ProspectiveCohorts[0].InputDiagnostics
	if diagnostics != (CohortInputDiagnostics{}) {
		t.Fatalf("closed cohort must not fabricate a diagnostics breakdown: got %+v", diagnostics)
	}
	latest := payload.ProspectiveCohorts[0].LatestReport
	if latest == nil || latest.EligibleEpisodes != 802 || latest.Verdict != "do_not_promote" {
		t.Fatalf("latest registered report: got %#v", latest)
	}
	if got := payload.ProspectiveCohorts[1].Status; got != "closed" {
		t.Fatalf("HYP-010 status: got %s", got)
	}
	if payload.ProspectiveCohorts[1].LatestReport != nil {
		t.Fatalf("HYP-010 latest report: expected none registered in this fixture, got %#v",
			payload.ProspectiveCohorts[1].LatestReport)
	}
	if payload.ExitLiquidity == nil {
		t.Fatal("exit liquidity progress missing")
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

func TestReadinessClosedCohortsSkipLiveQuery(t *testing.T) {
	now := time.Date(2026, time.August, 30, 12, 0, 0, 0, time.UTC)
	db := &stubDB{responses: map[string][]stubRow{
		latestReportKey(liquidTakerContract):      {{err: pgx.ErrNoRows}},
		latestReportKey(liquidTakerWiderContract): {{err: pgx.ErrNoRows}},
		exitLiquidityProgressSQL:                  {{values: []any{30, 29, 28, 20, nil}}},
		sourceLeadProgressSQL: {{values: []any{
			120, 115, 110, 5, 0, 0, 0, 0, 0, 0, 105, 100, 31, 4, 80,
			20, 15, 0, 80, 20, 5, 12, 8, "source_lead_identity_registry_v1",
			strings.Repeat("a", 64), false, now.Add(-time.Hour),
		}}},
		targetKey("binance"):                {{values: []any{105, 100, 3, 2, 900.0, 1_500.0, 4.0, 8.0, 2.0, 5.0}}},
		targetKey("bybit"):                  {{values: []any{105, 99, 4, 2, 1_000.0, 1_700.0, 5.0, 9.0, 2.5, 5.5}}},
		sourceLeadIdentityReviewSQL:         {{values: []any{`[]`}}},
		latestReportKey(sourceLeadContract): {{err: pgx.ErrNoRows}},
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
		if item.Status != "closed" {
			t.Fatalf("%s status: got %s", item.Key, item.Status)
		}
	}
	if payload.Orderflow != nil {
		t.Fatal("missing Redis health should produce null orderflow progress")
	}
	if payload.SourceLead == nil {
		t.Fatal("source lead progress missing")
	}
	if payload.SourceLead.Status != "report_required" {
		t.Fatalf("source-lead status: got %s", payload.SourceLead.Status)
	}
	if !payload.SourceLead.MatureFourHourWindows.Exact {
		t.Fatal("source-lead database progress must be exact")
	}
	// Only latestReport(hyp_008), latestReport(hyp_010), exitLiquidityProgress,
	// and source-lead's own queries run -- cohortProgressSQL has no call sites
	// left at all (both cohorts are permanently closed).
	if len(db.calls) != 8 {
		t.Fatalf("closed cohorts must not query cohortProgressSQL, got %d calls", len(db.calls))
	}
}

func TestSourceLeadProgressExposesOperationalFailures(t *testing.T) {
	now := time.Date(2026, time.August, 3, 12, 0, 0, 0, time.UTC)
	db := &stubDB{responses: map[string][]stubRow{
		sourceLeadProgressSQL: {{values: []any{
			12, 11, 9, 1, 2, 1, 1, 0, 1, 1, 8, 4, 6, 1, 3,
			2, 1, 1, 5, 1, 4, 1, 1, "source_lead_identity_registry_v1",
			strings.Repeat("a", 64), false, now.Add(-time.Hour),
		}}},
		targetKey("binance"): {{values: []any{8, 7, 0, 1, 800.0, 1_400.0, 3.0, 7.0, 1.5, 4.0}}},
		targetKey("bybit"):   {{values: []any{8, 6, 1, 1, 900.0, 1_600.0, 4.0, 8.0, 2.0, 5.0}}},
		sourceLeadIdentityReviewSQL: {{values: []any{
			`[{"base":"ABC","source_identity_key":"gate:swap:ABC_USDT:1","captures":2,"first_observed_at":"2026-08-02T01:00:00Z","last_observed_at":"2026-08-03T01:00:00Z","executable_targets":"binance,bybit","exact_target_identities":2,"source_conflict":false}]`,
		}}},
		latestReportKey(sourceLeadContract): {{err: pgx.ErrNoRows}},
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
	if progress.RouteEvidencePending != 4 {
		t.Fatalf("route evidence pending: got %d", progress.RouteEvidencePending)
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

// TestReadinessDegradesFailingSectionsInsteadOfFailingWhole is the core
// regression for fix/research-readiness-handler-concurrency-v1: Readiness
// used to fail the entire response with 500 the instant any one section's
// DB query errored (the previous version of this test, TestReadiness
// FailsClosedOnDatabaseError, asserted exactly that as the intended
// behavior). Now each section runs independently -- one section's error
// must degrade only that section to nil, and every other, successfully-
// fetched section must still be served at 200.
func TestReadinessDegradesFailingSectionsInsteadOfFailingWhole(t *testing.T) {
	now := time.Date(2026, time.August, 30, 12, 0, 0, 0, time.UTC)
	db := &stubDB{responses: map[string][]stubRow{
		latestReportKey(liquidTakerContract):      {{err: pgx.ErrNoRows}},
		latestReportKey(liquidTakerWiderContract): {{err: pgx.ErrNoRows}},
		// exit_liquidity is the one section that fails here -- must degrade
		// to nil, not take the whole response down with it.
		exitLiquidityProgressSQL: {{err: fmt.Errorf("database unavailable")}},
		sourceLeadProgressSQL: {{values: []any{
			120, 115, 110, 5, 0, 0, 0, 0, 0, 0, 105, 100, 31, 4, 80,
			20, 15, 0, 80, 20, 5, 12, 8, "source_lead_identity_registry_v1",
			strings.Repeat("a", 64), false, now.Add(-time.Hour),
		}}},
		targetKey("binance"):                {{values: []any{105, 100, 3, 2, 900.0, 1_500.0, 4.0, 8.0, 2.0, 5.0}}},
		targetKey("bybit"):                  {{values: []any{105, 99, 4, 2, 1_000.0, 1_700.0, 5.0, 9.0, 2.5, 5.5}}},
		sourceLeadIdentityReviewSQL:         {{values: []any{`[]`}}},
		latestReportKey(sourceLeadContract): {{err: pgx.ErrNoRows}},
	}}
	rdb, closeRedis := testRedis(t, nil)
	defer closeRedis()
	handler := &Handler{db: db, redis: rdb, now: func() time.Time { return now }}

	response := serve(t, handler)
	if response.Code != http.StatusOK {
		t.Fatalf(
			"status: got %d, want 200 (a single failing section must degrade, not 500 the endpoint); body %s",
			response.Code, response.Body.String(),
		)
	}

	var payload Response
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if payload.ExitLiquidity != nil {
		t.Fatalf("exit_liquidity: want nil (degraded) after its query errored, got %+v", payload.ExitLiquidity)
	}
	if payload.SourceLead == nil || payload.SourceLead.Status != "report_required" {
		t.Fatalf(
			"source_lead: want a successfully-populated section despite exit_liquidity's failure, got %+v",
			payload.SourceLead,
		)
	}
	for _, item := range payload.ProspectiveCohorts {
		if item.Status != "closed" {
			t.Fatalf("%s status: want closed regardless of exit_liquidity's failure, got %s", item.Key, item.Status)
		}
	}
}

// blockingRow implements pgxRow by blocking until ctx is done, then
// returning ctx.Err() -- a genuinely hanging query, not just a slow one,
// so TestReadinessSubcallTimeoutBoundsAHangingQuery proves subcallContext's
// timeout actually cuts a section off rather than merely asserting the
// code compiles to use one.
type blockingRow struct{ ctx context.Context }

func (r blockingRow) Scan(_ ...any) error {
	<-r.ctx.Done()
	return r.ctx.Err()
}

// blockingDB implements queryRower by handing back a blockingRow for
// every call -- every DB-backed section hangs.
type blockingDB struct{}

func (blockingDB) QueryRow(ctx context.Context, _ string, _ ...any) pgxRow {
	return blockingRow{ctx: ctx}
}

// TestReadinessSubcallTimeoutBoundsAHangingQuery is the other half of
// fix/research-readiness-handler-concurrency-v1's regression coverage: the
// 2026-08-29 production incident this fix exists to prevent was a single
// 18.6s query (cohortProgressSQL, since removed) starving every section
// behind it on the shared, unbounded request context, timing out the
// whole endpoint against the server's 30s WriteTimeout. A hanging query
// must now be cut off by its own subcallContext budget and degrade to nil
// -- Readiness must return promptly, not hang for the life of the request.
// Uses a tiny injected subcallTimeout so the test itself stays fast rather
// than paying the full production budget.
func TestReadinessSubcallTimeoutBoundsAHangingQuery(t *testing.T) {
	const testTimeout = 50 * time.Millisecond
	rdb, closeRedis := testRedis(t, nil)
	defer closeRedis()
	handler := &Handler{
		db:             blockingDB{},
		redis:          rdb,
		subcallTimeout: testTimeout,
		now: func() time.Time {
			return time.Date(2026, time.August, 30, 12, 0, 0, 0, time.UTC)
		},
	}

	start := time.Now()
	response := serve(t, handler)
	elapsed := time.Since(start)

	if response.Code != http.StatusOK {
		t.Fatalf(
			"status: got %d, want 200 (a hanging section must degrade, not hang the endpoint); body %s",
			response.Code, response.Body.String(),
		)
	}
	// This bound must actually distinguish concurrent from sequential
	// execution, not just "eventually returns" (colleague review: the
	// original 10x margin here passed even under a regression back to
	// sequential calls, since 4 blocking sections x testTimeout each still
	// fell well inside it). blockingDB hangs every h.db call: liquidReport,
	// widerReport, exitProgress, and sourceLeadProgress's own first internal
	// query each block for exactly testTimeout (orderflowProgress touches
	// only Redis, never blocks). Sequential execution of those 4 sections
	// has a hard floor of 4*testTimeout; running them concurrently keeps
	// wall time close to 1*testTimeout regardless of how many there are.
	// 2*testTimeout sits strictly between those two floors -- generous
	// enough for scheduling jitter on one timeout window, provably
	// insufficient for four sequential ones.
	if elapsed > 2*testTimeout {
		t.Fatalf(
			"Readiness took %s for 4 concurrently-hanging sections with a %s each -- "+
				"want close to one timeout window, not a multiple of it (sequential execution regression?)",
			elapsed, testTimeout,
		)
	}

	var payload Response
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if payload.ExitLiquidity != nil {
		t.Fatalf("exit_liquidity: want nil after its query hung past the timeout, got %+v", payload.ExitLiquidity)
	}
	if payload.SourceLead != nil {
		t.Fatalf("source_lead: want nil after its query hung past the timeout, got %+v", payload.SourceLead)
	}
	if payload.ProspectiveCohorts[0].LatestReport != nil {
		t.Fatalf(
			"hyp_008 latest_report: want nil after its query hung past the timeout, got %+v",
			payload.ProspectiveCohorts[0].LatestReport,
		)
	}
}

func TestQueriesPreserveResearchBoundaries(t *testing.T) {
	// hyp_008/hyp_010 are permanently closed (do_not_promote, 2026-08-29) and
	// no longer run a live cohortProgressSQL query -- see
	// liquidTakerClosedCounts/liquidTakerWiderClosedCounts in handler.go.
	if liquidTakerClosedCounts != (cohortCounts{episodes: 494, clusters: 207, weeks: 4}) {
		t.Fatalf("liquid taker closed counts drifted from the frozen ROADMAP verdict: %+v", liquidTakerClosedCounts)
	}
	if liquidTakerWiderClosedCounts != (cohortCounts{episodes: 429, clusters: 183, weeks: 4}) {
		t.Fatalf("liquid taker wider closed counts drifted from the frozen ROADMAP verdict: %+v", liquidTakerWiderClosedCounts)
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
		"AND source_first_observed_at >= $3",
		"t.status = 'sampled' AND t.identity_verified",
		"AND c.source_first_observed_at >= $3",
		"WHERE status = 'sampled' AND identity_verified",
		"AND source_first_observed_at >= $4 AND observed_at >= source_first_observed_at",
		"AND source_first_observed_at >= $4 AND spread_bps >= 0",
		"AND source_first_observed_at >= $4 AND entry_impact_bps >= 0",
		"qualification_reason = 'route_evidence_not_yet_independent'",
	}
	sourceLeadQueries := sourceLeadProgressSQL + sourceLeadTargetProgressSQL +
		sourceLeadIdentityReviewSQL
	for _, fragment := range requiredSourceLeadFragments {
		if !strings.Contains(sourceLeadQueries, fragment) {
			t.Fatalf("source-lead query missing %q", fragment)
		}
	}
	// Colleague review, 2026-08-29/30, PR 3 review round: the qualification
	// join must never be scoped to one qualification_version -- captures
	// spans the full history, and a version-scoped join makes every
	// pre-cutover capture's already-computed qualification disappear from
	// every count the moment QUALIFICATION_VERSION bumps, even though
	// nothing about that capture's own row changed.
	if strings.Contains(sourceLeadProgressSQL, "qualification.qualification_version =") {
		t.Fatal("qualification join must not be scoped to a single qualification_version")
	}
}

func strconv64(value int64) string {
	return fmt.Sprintf("%d", value)
}

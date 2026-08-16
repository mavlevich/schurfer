package pumps

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// testDatabaseURL matches infra/docker/docker-compose.dev.yml's local dev Postgres
// (same convention as apps/collector/internal/momentumcapture/writer_integration_test.go's
// own testDatabaseURL). This test is real-database, not a stub: handler_test.go's own
// stubRows/scanInto proves the handler's own filtering/mapping logic, but only a real
// pgx connection can catch a genuine NULL-scan mismatch between a nullable SQL column
// and a non-pointer Go destination field -- exactly the class of bug that crashed this
// endpoint in production twice (2026-08-16, see momentumWatchQuery's own doc comment
// and combinedTradesCTE's own in the trades package). Skips instead of failing when no
// Postgres is reachable, so `go test ./...` still passes without a live database; CI's
// own test-go job runs a real Postgres service specifically so this is NOT skipped
// there (see .github/workflows/ci.yml).
const testDatabaseURL = "postgres://schurfer:schurfer_dev@localhost:5432/schurfer"

func connectTestPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, testDatabaseURL)
	if err != nil {
		t.Skipf("no local postgres reachable: %v", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		t.Skipf("no local postgres reachable: %v", err)
	}
	return pool
}

// insertWatchRun creates the parent app.momentum_flow_watch_runs row a
// momentum_flow_watch_states row's own watch_version foreign key requires.
func insertWatchRun(t *testing.T, pool *pgxpool.Pool, watchVersion string) {
	t.Helper()
	_, err := pool.Exec(context.Background(), `
		INSERT INTO app.momentum_flow_watch_runs
			(watch_version, contract_sha256, contract_json, cohort_started_at)
		VALUES ($1, repeat('a', 64), '{}'::jsonb, now())
		ON CONFLICT (watch_version) DO NOTHING`,
		watchVersion,
	)
	if err != nil {
		t.Fatalf("insertWatchRun: %v", err)
	}
}

// insertWatchEvaluation inserts one timeseries.momentum_flow_watch_evaluations_1m
// row. watchID is nil for every decision_status except 'watch' (the table's own
// momentum_flow_watch_evaluations_watch_shape CHECK enforces this pairing).
func insertWatchEvaluation(
	t *testing.T, pool *pgxpool.Pool,
	watchVersion, exchange, symbol string,
	bucketStart time.Time,
	decisionStatus string,
	episodeID string,
	watchID *string,
) {
	t.Helper()
	qualityReady := decisionStatus != "rejected_quality"
	rawQualified := decisionStatus == "watch" ||
		decisionStatus == "suppressed_active_episode" ||
		decisionStatus == "suppressed_cooldown"
	inputHash := sha256.Sum256([]byte(exchange + symbol + bucketStart.String()))
	evaluatorStarted := bucketStart
	evaluatorCompleted := bucketStart.Add(2 * time.Second)
	decisionAt := bucketStart.Add(1 * time.Second)
	_, err := pool.Exec(context.Background(), `
		INSERT INTO timeseries.momentum_flow_watch_evaluations_1m
			(exchange, market_type, symbol, capture_version, watch_version, bucket_start,
			 universe_version, quality_ready, raw_qualified, decision_status,
			 price_return_60m_pct, price_return_15m_pct, oi_growth_60m_pct,
			 buy_imbalance_15m, flow_notional_15m_usd, flow_acceleration_15m_vs_prior_45m,
			 cross_section_size, evaluator_started_at, evaluator_completed_at, decision_at,
			 episode_id, watch_id, state_active_after, state_clear_streak_after, input_hash)
		VALUES
			($1, 'linear', $2, 'test-capture-v1', $3, $4::timestamptz,
			 'test-universe-v1', $5, $6, $7,
			 1.5, 0.5, 2.0, 0.2, 1000.0, 0.5,
			 10, $10::timestamptz, $11::timestamptz, $12::timestamptz,
			 $8::uuid, $9::uuid, true, 0, $13::bytea)
		ON CONFLICT (exchange, market_type, symbol, watch_version, bucket_start) DO NOTHING`,
		exchange, symbol, watchVersion, bucketStart,
		qualityReady, rawQualified, decisionStatus,
		episodeID, watchID,
		evaluatorStarted, evaluatorCompleted, decisionAt,
		inputHash[:],
	)
	if err != nil {
		t.Fatalf("insertWatchEvaluation: %v", err)
	}
}

// insertWatchState inserts one app.momentum_flow_watch_states row.
func insertWatchState(
	t *testing.T, pool *pgxpool.Pool,
	watchVersion, exchange, symbol string,
	clearStreak int,
	lastWatchAt time.Time,
	episodeID string,
	lastBucketStart time.Time,
) {
	t.Helper()
	_, err := pool.Exec(context.Background(), `
		INSERT INTO app.momentum_flow_watch_states
			(watch_version, exchange, market_type, symbol, active_episode, clear_streak,
			 last_watch_at, episode_id, last_bucket_start)
		VALUES ($1, $2, 'linear', $3, true, $4, $5, $6::uuid, $7)
		ON CONFLICT (watch_version, exchange, market_type, symbol) DO UPDATE SET
			clear_streak = excluded.clear_streak,
			last_watch_at = excluded.last_watch_at,
			episode_id = excluded.episode_id,
			last_bucket_start = excluded.last_bucket_start`,
		watchVersion, exchange, symbol, clearStreak, lastWatchAt, episodeID, lastBucketStart,
	)
	if err != nil {
		t.Fatalf("insertWatchState: %v", err)
	}
}

func cleanupWatchTestRows(pool *pgxpool.Pool, watchVersion string) {
	// ON DELETE CASCADE from momentum_flow_watch_runs takes momentum_flow_watch_states
	// with it; the evaluations hypertable has no FK, so it needs its own delete.
	ctx := context.Background()
	_, _ = pool.Exec(ctx, `DELETE FROM timeseries.momentum_flow_watch_evaluations_1m WHERE watch_version = $1`, watchVersion) //nolint:errcheck
	_, _ = pool.Exec(ctx, `DELETE FROM app.momentum_flow_watch_runs WHERE watch_version = $1`, watchVersion)                  //nolint:errcheck
}

// TestMomentumWatchAgainstRealPostgres is a regression for a production incident
// (2026-08-16): an episode reactivated via the evaluator's own suppressed_cooldown
// path has no decision_status='watch' row at all for its current episode_id, so
// the first-watch lateral subquery's own min() was NULL. Scanning that NULL into
// Go's non-pointer int64 FirstWatchAt crashed the whole endpoint with a 500 --
// impossible to catch with the stub-based tests in handler_test.go, since
// stubRows.Scan (unlike real pgx) treats a nil column value as a harmless zero,
// not an error. Only a real Postgres connection reproduces the actual crash this
// query previously had.
func TestMomentumWatchAgainstRealPostgres(t *testing.T) {
	pool := connectTestPool(t)
	defer pool.Close()

	const watchVersion = "test_momentum_watch_v1_integration"
	defer cleanupWatchTestRows(pool, watchVersion)
	insertWatchRun(t, pool, watchVersion)

	now := time.Now().UTC().Truncate(time.Minute)

	// Case 1 (the bug): active_episode reactivated via suppressed_cooldown --
	// its own current episode_id has NO decision_status='watch' evaluation row.
	// Only a suppressed_cooldown row exists, matching last_bucket_start (the
	// outer join key) so the row surfaces at all.
	staleEpisode := "11111111-1111-1111-1111-111111111111"
	insertWatchEvaluation(
		t, pool, watchVersion, "bybit", "NOWATCHUSDT", now, "suppressed_cooldown", staleEpisode, nil,
	)
	insertWatchState(t, pool, watchVersion, "bybit", "NOWATCHUSDT", 0, now.Add(-90*time.Minute), staleEpisode, now)

	// Case 2 (the normal path): active_episode WITH a real 'watch' row for its
	// own current episode_id -- first_watch_at must come from that row, not the
	// fallback, and must be <= last_watch_at.
	watchID := "22222222-2222-2222-2222-222222222222"
	normalEpisode := "33333333-3333-3333-3333-333333333333"
	watchBucket := now.Add(-5 * time.Minute)
	insertWatchEvaluation(
		t, pool, watchVersion, "bybit", "HASWATCHUSDT", watchBucket, "watch", normalEpisode, &watchID,
	)
	insertWatchState(t, pool, watchVersion, "bybit", "HASWATCHUSDT", 0, watchBucket, normalEpisode, watchBucket)

	h := &Handler{pool: &poolAdapter{inner: pool}}
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/momentum-watch", nil)
	w := httptest.NewRecorder()
	h.MomentumWatch(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}

	byOrigin := map[string]momentumWatchEntry{}
	for _, e := range decodeMomentumWatchResponse(t, w).Watch {
		byOrigin[e.Symbol] = e
	}

	noWatch, ok := byOrigin["NOWATCHUSDT"]
	if !ok {
		t.Fatal("NOWATCHUSDT (suppressed_cooldown, no watch row) missing from response")
	}
	if noWatch.FirstWatchAt != noWatch.LastWatchAt {
		t.Errorf(
			"NOWATCHUSDT: first_watch_at (%d) must fall back to last_watch_at (%d) when no 'watch' row exists",
			noWatch.FirstWatchAt, noWatch.LastWatchAt,
		)
	}

	hasWatch, ok := byOrigin["HASWATCHUSDT"]
	if !ok {
		t.Fatal("HASWATCHUSDT (real watch row) missing from response")
	}
	if hasWatch.FirstWatchAt > hasWatch.LastWatchAt {
		t.Errorf(
			"HASWATCHUSDT: first_watch_at (%d) must not be after last_watch_at (%d)",
			hasWatch.FirstWatchAt, hasWatch.LastWatchAt,
		)
	}
}

func decodeMomentumWatchResponse(t *testing.T, w *httptest.ResponseRecorder) momentumWatchResponse {
	t.Helper()
	var resp momentumWatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v (%s)", err, w.Body.String())
	}
	return resp
}

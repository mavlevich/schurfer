package trades

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// newTestUUID generates a random UUIDv4 string without pulling in a UUID
// library dependency just for test fixtures.
func newTestUUID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b) //nolint:errcheck // crypto/rand.Read never returns a short read without an error
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// testDatabaseURL matches infra/docker/docker-compose.dev.yml's local dev Postgres
// (same convention as apps/collector/internal/momentumcapture/writer_integration_test.go's
// own testDatabaseURL, and apps/api-gateway/internal/pumps's own momentum_watch_
// integration_test.go). Real-database, not a stub: handler_test.go's own stubRows/
// scanInto proves the handler's own filtering/sorting/origin-tagging logic, but only
// a real pgx connection can catch a genuine NULL-scan mismatch between a nullable SQL
// column and a non-pointer Go destination field -- exactly the class of bug that
// crashed this endpoint in production (2026-08-16, see combinedTradesCTE's own doc
// comment). Skips instead of failing when no Postgres is reachable; CI's own test-go
// job runs a real Postgres service specifically so this is NOT skipped there.
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

// TestCombinedTradesCTEAgainstRealPostgres is a regression for a production
// incident (2026-08-16): app.momentum_flow_paper_probes' own fees_usd/funding_usd
// are nullable (NULL until that probe's own cost accounting completes -- a still-
// open probe legitimately has neither yet), but tradeRow.FeesUSD/FundingUSD are
// plain non-pointer float64, matching app.trades' own NOT NULL columns. Without the
// COALESCE in combinedTradesCTE, scanning such a probe crashed the whole /api/trades
// endpoint -- impossible to catch with the stub-based tests in handler_test.go, since
// stubRows.Scan (unlike real pgx) treats a nil column value as a harmless zero, not
// an error. Only a real Postgres connection reproduces the actual crash this query
// previously had.
func TestCombinedTradesCTEAgainstRealPostgres(t *testing.T) {
	pool := connectTestPool(t)
	defer pool.Close()

	paperVersion := "test_momentum_flow_paper_v1_integration"
	defer cleanupPaperTestRows(pool, paperVersion)
	insertPaperRun(t, pool, paperVersion)

	paperID := newTestUUID()
	insertStillOpenPaperProbe(t, pool, paperVersion, paperID)

	h := &Handler{pool: &poolAdapter{inner: pool}}
	req := httptest.NewRequest(http.MethodGet, "/api/trades?origin=momentum_flow_paper", nil)
	w := httptest.NewRecorder()
	h.List(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}

	var resp listResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v (%s)", err, w.Body.String())
	}

	var found *tradeRow
	for i := range resp.Trades {
		if resp.Trades[i].ID == "momentum_flow_paper:"+paperID {
			found = &resp.Trades[i]
		}
	}
	if found == nil {
		t.Fatalf("still-open probe missing from response: %+v", resp.Trades)
	}
	if found.FeesUSD != 0 {
		t.Errorf("FeesUSD: want 0 (coalesced from NULL), got %v", found.FeesUSD)
	}
	if found.FundingUSD != 0 {
		t.Errorf("FundingUSD: want 0 (coalesced from NULL), got %v", found.FundingUSD)
	}
}

// TestCombinedTradesCTEReadsCanonicalStrategyIdentity is a regression for
// the colleague-review finding that strategy_name/strategy_version were
// derived from setup_context->>'strategy', which pump_short's own
// trader.py never sets (it stamps setup_context["strategy_version"]
// instead -- see journal.strategy_identity's own docstring). Every
// pump_short trade showed strategy_name="unknown" in this endpoint. The
// canonical source is app.trades.strategy_id -> app.strategies, which
// every trade already carries regardless of what setup_context happens to
// contain.
func TestCombinedTradesCTEReadsCanonicalStrategyIdentity(t *testing.T) {
	pool := connectTestPool(t)
	defer pool.Close()

	version := "1t" + newTestUUID()[:8] // app.strategies.version is varchar(16)
	defer cleanupStrategyTestRows(pool, version)

	strategyID := insertStrategy(t, pool, "pump_short", version)
	tradeID := insertTradeWithoutSetupContextStrategyKey(t, pool, strategyID)

	h := &Handler{pool: &poolAdapter{inner: pool}}
	req := httptest.NewRequest(http.MethodGet, "/api/trades?exchange=test_strategy_join", nil)
	w := httptest.NewRecorder()
	h.List(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp listResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v (%s)", err, w.Body.String())
	}

	var found *tradeRow
	for i := range resp.Trades {
		if resp.Trades[i].ID == fmt.Sprintf("pump_short:%d", tradeID) {
			found = &resp.Trades[i]
		}
	}
	if found == nil {
		t.Fatalf("inserted trade missing from response: %+v", resp.Trades)
	}
	if found.StrategyName != "pump_short" {
		t.Errorf("StrategyName: want pump_short (from app.strategies join), got %q", found.StrategyName)
	}
	if found.StrategyVersion != version {
		t.Errorf("StrategyVersion: want %q (from app.strategies join), got %q", version, found.StrategyVersion)
	}
}

// TestByStrategyGroupsRealTradesByNameAndVersion proves the GROUP BY
// strategy_name, strategy_version actually separates two different
// strategies' trades against a real Postgres instance, not just the
// stub-based unit tests in handler_test.go.
func TestByStrategyGroupsRealTradesByNameAndVersion(t *testing.T) {
	pool := connectTestPool(t)
	defer pool.Close()

	nameA := "test_by_strategy_a_" + newTestUUID()[:8]
	nameB := "test_by_strategy_b_" + newTestUUID()[:8]
	defer cleanupByStrategyTestRows(pool, nameA, nameB)

	strategyA := insertStrategy(t, pool, nameA, "1")
	strategyB := insertStrategy(t, pool, nameB, "1")
	insertClosedTrade(t, pool, strategyA)
	insertClosedTrade(t, pool, strategyA)
	insertClosedTrade(t, pool, strategyB)

	h := &Handler{pool: &poolAdapter{inner: pool}}
	req := httptest.NewRequest(http.MethodGet, "/api/trades/stats/by-strategy?exchange=test_by_strategy", nil)
	w := httptest.NewRecorder()
	h.ByStrategy(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp byStrategyResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v (%s)", err, w.Body.String())
	}

	var bucketA, bucketB *strategyStatsEntry
	for i := range resp.Strategies {
		switch resp.Strategies[i].StrategyName {
		case nameA:
			bucketA = &resp.Strategies[i]
		case nameB:
			bucketB = &resp.Strategies[i]
		}
	}
	if bucketA == nil || bucketB == nil {
		t.Fatalf("want both strategies present, got %+v", resp.Strategies)
	}
	if bucketA.Count != 2 {
		t.Errorf("strategy A: want count=2, got %d", bucketA.Count)
	}
	if bucketB.Count != 1 {
		t.Errorf("strategy B: want count=1, got %d", bucketB.Count)
	}
	if bucketA.GrossUSD != 10 { // two trades at +$5 each
		t.Errorf("strategy A: want gross_usd=10, got %v", bucketA.GrossUSD)
	}
}

func insertStrategy(t *testing.T, pool *pgxpool.Pool, name, version string) int64 {
	t.Helper()
	var id int64
	err := pool.QueryRow(context.Background(), `
		INSERT INTO app.strategies (name, version, description)
		VALUES ($1, $2, 'integration test')
		ON CONFLICT (name, version) DO UPDATE SET updated_at = now()
		RETURNING id`,
		name, version,
	).Scan(&id)
	if err != nil {
		t.Fatalf("insertStrategy: %v", err)
	}
	return id
}

// insertTradeWithoutSetupContextStrategyKey mirrors the real pump_short
// trader.py convention exactly: setup_context carries "strategy_version",
// never "strategy" -- the only reliable strategy identity is the FK.
func insertTradeWithoutSetupContextStrategyKey(t *testing.T, pool *pgxpool.Pool, strategyID int64) int64 {
	t.Helper()
	var id int64
	err := pool.QueryRow(context.Background(), `
		INSERT INTO app.trades (
			strategy_id, symbol, exchange, market_type, side,
			size_usd, leverage, entry_price, entry_at,
			fees_usd, funding_usd,
			accounting_version, accounting_status, status,
			setup_context, notes
		) VALUES (
			$1, 'JOINTEST/USDT:USDT', 'test_strategy_join', 'perp', 'short',
			50.0, 3.0, 1.0, now(),
			0, 0,
			'legacy_price_only_v1', 'legacy', 'open',
			$2::jsonb, NULL
		) RETURNING id`,
		strategyID, `{"strategy_version": "irrelevant_legacy_value"}`,
	).Scan(&id)
	if err != nil {
		t.Fatalf("insertTradeWithoutSetupContextStrategyKey: %v", err)
	}
	return id
}

func cleanupStrategyTestRows(pool *pgxpool.Pool, version string) {
	ctx := context.Background()
	_, _ = pool.Exec(ctx, `DELETE FROM app.trades WHERE exchange = 'test_strategy_join'`)                   //nolint:errcheck
	_, _ = pool.Exec(ctx, `DELETE FROM app.strategies WHERE name = 'pump_short' AND version = $1`, version) //nolint:errcheck
}

// insertClosedTrade inserts a closed trade with a known gross_pnl_usd/pct
// (5% of a $100 short move) under the given strategy, scoped to the
// 'test_by_strategy' exchange tag so it's invisible to every other test.
func insertClosedTrade(t *testing.T, pool *pgxpool.Pool, strategyID int64) {
	t.Helper()
	var id int64
	err := pool.QueryRow(context.Background(), `
		INSERT INTO app.trades (
			strategy_id, symbol, exchange, market_type, side,
			size_usd, leverage, entry_price, entry_at,
			exit_price, exit_at,
			gross_pnl_usd, gross_pnl_pct, pnl_usd, pnl_pct,
			fees_usd, funding_usd,
			accounting_version, accounting_status, status,
			setup_context, notes
		) VALUES (
			$1, 'BYSTRATEGYTEST/USDT:USDT', 'test_by_strategy', 'perp', 'short',
			100.0, 1.0, 1.0, now() - interval '1 hour',
			0.95, now(),
			5.0, 5.0, 5.0, 5.0,
			0, 0,
			'legacy_price_only_v1', 'legacy', 'closed',
			'{}'::jsonb, NULL
		) RETURNING id`,
		strategyID,
	).Scan(&id)
	if err != nil {
		t.Fatalf("insertClosedTrade: %v", err)
	}
}

func cleanupByStrategyTestRows(pool *pgxpool.Pool, names ...string) {
	ctx := context.Background()
	_, _ = pool.Exec(ctx, `DELETE FROM app.trades WHERE exchange = 'test_by_strategy'`) //nolint:errcheck
	for _, name := range names {
		_, _ = pool.Exec(ctx, `DELETE FROM app.strategies WHERE name = $1`, name) //nolint:errcheck
	}
}

func insertPaperRun(t *testing.T, pool *pgxpool.Pool, paperVersion string) {
	t.Helper()
	_, err := pool.Exec(context.Background(), `
		INSERT INTO app.momentum_flow_paper_runs
			(paper_version, contract_sha256, contract_json, cohort_started_at)
		VALUES ($1, repeat('b', 64), '{}'::jsonb, now())
		ON CONFLICT (paper_version) DO NOTHING`,
		paperVersion,
	)
	if err != nil {
		t.Fatalf("insertPaperRun: %v", err)
	}
}

// insertStillOpenPaperProbe inserts one app.momentum_flow_paper_probes row shaped
// exactly like the production rows that crashed this endpoint: entry_status='opened'
// (a real, filled entry) but fees_usd/funding_usd both NULL (cost accounting for a
// still-open position has not run yet, independent of entry_status).
func insertStillOpenPaperProbe(t *testing.T, pool *pgxpool.Pool, paperVersion, paperID string) {
	t.Helper()
	now := time.Now().UTC()
	watchID := newTestUUID()
	episodeID := newTestUUID()

	_, err := pool.Exec(context.Background(), `
		INSERT INTO app.momentum_flow_paper_probes
			(paper_id, paper_version, watch_version, watch_id, episode_id,
			 exchange, market_type, symbol,
			 watch_bucket_start, watch_decision_at, claimed_at,
			 entry_status, entry_vwap, entry_filled_notional_usd, entry_at,
			 position_status)
		VALUES
			($1::uuid, $2, 'test_watch_v1', $3::uuid, $4::uuid,
			 'bybit', 'linear', 'ERAUSDT',
			 $5::timestamptz, $6::timestamptz, $7::timestamptz,
			 'opened', 10.5, 50.0, $6::timestamptz,
			 'open')`,
		paperID, paperVersion, watchID, episodeID,
		now.Add(-10*time.Minute), now.Add(-9*time.Minute), now.Add(-9*time.Minute),
	)
	if err != nil {
		t.Fatalf("insertStillOpenPaperProbe: %v", err)
	}
}

func cleanupPaperTestRows(pool *pgxpool.Pool, paperVersion string) {
	// ON DELETE RESTRICT on momentum_flow_paper_probes.paper_version means the
	// probes row must go first, then the parent run row.
	ctx := context.Background()
	_, _ = pool.Exec(ctx, `DELETE FROM app.momentum_flow_paper_probes WHERE paper_version = $1`, paperVersion) //nolint:errcheck
	_, _ = pool.Exec(ctx, `DELETE FROM app.momentum_flow_paper_runs WHERE paper_version = $1`, paperVersion)   //nolint:errcheck
}

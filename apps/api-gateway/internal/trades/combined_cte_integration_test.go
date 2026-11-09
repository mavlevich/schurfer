package trades

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mavlevich/schurfer/api-gateway/internal/testdburl"
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
// crashed this endpoint in production (2026-08-16, see CombinedTradesCTE's own doc
// comment). Skips instead of failing when no Postgres is reachable; CI's own test-go
// job runs a real Postgres service specifically so this is NOT skipped there.
var testDatabaseURL = func() string {
	url, err := testdburl.URL()
	if err != nil {
		panic(err)
	}
	return url
}()

func connectTestPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, testDatabaseURL)
	if err != nil {
		if os.Getenv("REQUIRE_INTEGRATION_DB") == "1" {
			t.Fatalf("no local postgres reachable: %v", err)
		}
		t.Skipf("no local postgres reachable: %v", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		if os.Getenv("REQUIRE_INTEGRATION_DB") == "1" {
			t.Fatalf("no local postgres reachable: %v", err)
		}
		t.Skipf("no local postgres reachable: %v", err)
	}
	return pool
}

// TestCombinedTradesCTEAgainstRealPostgres is a regression for a production
// incident (2026-08-16): app.momentum_flow_paper_probes' own fees_usd/funding_usd
// are nullable (NULL until that probe's own cost accounting completes -- a still-
// open probe legitimately has neither yet), but tradeRow.FeesUSD/FundingUSD are
// plain non-pointer float64, matching app.trades' own NOT NULL columns. Without the
// COALESCE in CombinedTradesCTE, scanning such a probe crashed the whole /api/trades
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

// TestCombinedTradesCTEReadsPaperVersionAndLeverage is a regression for the
// 2026-10-17 audit finding: every paper probe was reported as strategy_key
// "momentum_flow_v1" with leverage 1, so the 3x variant
// (momentum_flow_paper_v1_lev3) showed a third of its ROE and merged with the
// 1x versions in the by-strategy view.
func TestCombinedTradesCTEReadsPaperVersionAndLeverage(t *testing.T) {
	pool := connectTestPool(t)
	defer pool.Close()

	paperVersion := "test_paper_lev3_" + newTestUUID()[:8]
	defer cleanupPaperTestRows(pool, paperVersion)
	insertPaperRunWithLeverage(t, pool, paperVersion, 3)
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
		t.Fatalf("probe missing from response: %+v", resp.Trades)
	}
	if found.Leverage != 3 {
		t.Errorf("Leverage: want 3 (from the run's contract), got %v", found.Leverage)
	}
	if found.StrategyKey != paperVersion || found.StrategyVersion != paperVersion {
		t.Errorf("strategy key/version: want %q, got %q/%q", paperVersion, found.StrategyKey, found.StrategyVersion)
	}
	if found.Side != "long" {
		t.Errorf("Side: want long, got %q", found.Side)
	}
}

// TestCombinedTradesCTEHidesHYP015OutcomesUntilTheRead checks that a paper
// probe entered from the HYP-015 cohort start shows no exit, cost, return or
// real status until the formal read completes (an early stop must not show as
// closed, in the row or through a status filter), that an earlier probe keeps
// its outcome, and that a completed claim shows both again.
func TestCombinedTradesCTEHidesHYP015OutcomesUntilTheRead(t *testing.T) {
	pool := connectTestPool(t)
	defer pool.Close()
	ctx := context.Background()

	var completed int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM app.hold12h_formal_read_claims WHERE status = 'completed'`,
	).Scan(&completed); err != nil {
		t.Fatalf("count claims: %v", err)
	}
	if completed > 0 {
		t.Skip("a completed HYP-015 claim already exists in this database")
	}

	paperVersion := "test_paper_blind_" + newTestUUID()[:8]
	defer cleanupPaperTestRows(pool, paperVersion)
	insertPaperRun(t, pool, paperVersion)
	inCohort, earlyStop, stillOpen, before := newTestUUID(), newTestUUID(), newTestUUID(), newTestUUID()
	cohortEntry := time.Date(2026, 10, 10, 12, 0, 0, 0, time.UTC)
	insertClosedPaperProbe(t, pool, paperVersion, inCohort, cohortEntry, "max_hold", 12*time.Hour)
	insertClosedPaperProbe(t, pool, paperVersion, earlyStop, cohortEntry.Add(time.Hour), "stop_loss", 30*time.Minute)
	insertStillOpenPaperProbe(t, pool, paperVersion, stillOpen)
	insertClosedPaperProbe(t, pool, paperVersion, before, time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC), "max_hold", 12*time.Hour)
	blinded := []string{inCohort, earlyStop, stillOpen}

	rows := listPaperTrades(t, pool, paperVersion, "")
	for _, id := range blinded {
		assertBlindedTrade(t, rows, id)
	}
	if shown := rows["momentum_flow_paper:"+before]; shown.GrossPnlPct == nil || *shown.GrossPnlPct != 2.5 ||
		shown.Status != "closed" {
		t.Errorf("pre-cohort probe should keep its return and status, got %+v", shown)
	}
	// A status filter must not tell an early stop from a running position.
	for _, status := range []string{"closed", "open", "exit_unresolved"} {
		filtered := listPaperTrades(t, pool, paperVersion, "&status="+status)
		for _, id := range blinded {
			if _, ok := filtered["momentum_flow_paper:"+id]; ok {
				t.Errorf("status=%s matched blinded probe %s", status, id)
			}
		}
	}
	// The list total is a count over the same CTE and filter.
	var closedTotal int
	if err := pool.QueryRow(ctx,
		CombinedTradesCTE+`SELECT count(*) FROM combined WHERE status = 'closed' AND strategy_key = $1`,
		paperVersion,
	).Scan(&closedTotal); err != nil {
		t.Fatalf("count closed: %v", err)
	}
	if closedTotal != 1 {
		t.Errorf("closed count for this version: want 1 (the pre-cohort probe), got %d", closedTotal)
	}

	claim := "test_blind_" + newTestUUID()[:8]
	defer pool.Exec(ctx, `DELETE FROM app.hold12h_formal_read_claims WHERE contract_version = $1`, claim) //nolint:errcheck
	insertCompletedFormalReadClaim(t, pool, claim)
	read := listPaperTrades(t, pool, paperVersion, "")
	if r := read["momentum_flow_paper:"+inCohort]; r.GrossPnlPct == nil || *r.GrossPnlPct != 2.5 ||
		r.ExitPrice == nil || r.FeesUSD != 0.1 || r.Status != "closed" || r.AccountingStatus != "complete" {
		t.Errorf("cohort probe should show its outcome after the read, got %+v", r)
	}
	if r := read["momentum_flow_paper:"+earlyStop]; r.Status != "closed" || r.ExitReason == nil || *r.ExitReason != "stop_loss" {
		t.Errorf("early stop should show as a closed stop after the read, got %+v", r)
	}
}

// listPaperTrades returns this paper version's rows of /api/trades, by id.
func listPaperTrades(t *testing.T, pool *pgxpool.Pool, paperVersion, query string) map[string]tradeRow {
	t.Helper()
	h := &Handler{pool: &poolAdapter{inner: pool}}
	req := httptest.NewRequest(http.MethodGet, "/api/trades?origin=momentum_flow_paper&limit=200"+query, nil)
	w := httptest.NewRecorder()
	h.List(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp listResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v (%s)", err, w.Body.String())
	}
	rows := map[string]tradeRow{}
	for _, row := range resp.Trades {
		if row.StrategyKey == paperVersion {
			rows[row.ID] = row
		}
	}
	return rows
}

// assertBlindedTrade checks that a cohort probe shows its entry only.
func assertBlindedTrade(t *testing.T, rows map[string]tradeRow, paperID string) {
	t.Helper()
	row, ok := rows["momentum_flow_paper:"+paperID]
	if !ok {
		t.Fatalf("cohort probe %s missing from response", paperID)
	}
	if row.ExitPrice != nil || row.ExitAt != nil || row.ExitReason != nil || row.OutcomeLabel != nil ||
		row.AccountingError != nil {
		t.Errorf("cohort probe shows its exit before the read: %+v", row)
	}
	if row.GrossPnlUSD != nil || row.GrossPnlPct != nil || row.NetPnlUSD != nil || row.NetPnlPct != nil ||
		row.FeesUSD != 0 || row.FundingUSD != 0 {
		t.Errorf("cohort probe shows its costs or return before the read: %+v", row)
	}
	if row.Status != "blinded" || row.AccountingStatus != "blinded" || row.EntryPrice != 10.5 {
		t.Errorf("cohort probe must read status/accounting 'blinded' with its entry kept: %+v", row)
	}
}

// insertCompletedFormalReadClaim stores a completed HYP-015 claim row, which
// ends the blinding for every reader of CombinedTradesCTE.
func insertCompletedFormalReadClaim(t *testing.T, pool *pgxpool.Pool, contractVersion string) {
	t.Helper()
	if _, err := pool.Exec(context.Background(), `
		INSERT INTO app.hold12h_formal_read_claims
			(contract_version, contract_sha256, cohort_start, decision_prefix_end, code_revision,
			 working_tree_dirty, output_dir, watch_ids, watch_ids_sha256, coverage_closed,
			 coverage_funding_covered, coverage_accounting_complete, coverage_open_positions,
			 accepted_incomplete_coverage, status, lease_owner, lease_expires_at, completed_at,
			 artifact_sha256)
		VALUES ($1, repeat('c', 64), '2026-10-05', '2026-11-02', 'test', false, '/verdict',
			'[]'::jsonb, repeat('d', 64), 0, 0, 0, 0, false, 'completed', 'test', now(), now(),
			repeat('e', 64))`, contractVersion,
	); err != nil {
		t.Fatalf("insert completed claim: %v", err)
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
	insertPaperRunWithLeverage(t, pool, paperVersion, 1)
}

// insertPaperRunWithLeverage stores a contract_json shaped like the worker's own
// PaperContract.canonical_json(), which has carried "leverage" since the first
// paper contract (#203).
func insertPaperRunWithLeverage(t *testing.T, pool *pgxpool.Pool, paperVersion string, leverage int) {
	t.Helper()
	_, err := pool.Exec(context.Background(), `
		INSERT INTO app.momentum_flow_paper_runs
			(paper_version, contract_sha256, contract_json, cohort_started_at)
		VALUES ($1::text, repeat('b', 64),
			jsonb_build_object('paper_version', $1::text, 'side', 'long', 'leverage', $2::int), now())
		ON CONFLICT (paper_version) DO NOTHING`,
		paperVersion, leverage,
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

// insertClosedPaperProbe inserts a closed, fully accounted probe entered at
// entryAt, exited for exitReason after hold: gross +2.5%, fees 0.1 USD,
// funding 0.05 USD.
func insertClosedPaperProbe(
	t *testing.T, pool *pgxpool.Pool, paperVersion, paperID string,
	entryAt time.Time, exitReason string, hold time.Duration,
) {
	t.Helper()
	_, err := pool.Exec(context.Background(), `
		INSERT INTO app.momentum_flow_paper_probes
			(paper_id, paper_version, watch_version, watch_id, episode_id,
			 exchange, market_type, symbol, unified_symbol,
			 watch_bucket_start, watch_decision_at, claimed_at,
			 entry_status, entry_vwap, entry_filled_notional_usd, entry_at,
			 position_status, exit_reason, exit_vwap, exit_at,
			 gross_return_pct, net_return_pct, gross_pnl_usd, net_pnl_usd,
			 fees_usd, funding_usd, accounting_status)
		VALUES
			($1::uuid, $2, 'test_watch_v1', $3::uuid, $4::uuid,
			 'bybit', 'linear', 'ERAUSDT', 'ERA/USDT:USDT',
			 $5::timestamptz - interval '1 minute', $5::timestamptz, $5::timestamptz,
			 'opened', 10.5, 50.0, $5::timestamptz,
			 'closed', $6, 10.7625, $7::timestamptz,
			 2.5, 2.2, 1.25, 1.1,
			 0.1, 0.05, 'complete')`,
		paperID, paperVersion, newTestUUID(), newTestUUID(), entryAt, exitReason, entryAt.Add(hold),
	)
	if err != nil {
		t.Fatalf("insertClosedPaperProbe: %v", err)
	}
}

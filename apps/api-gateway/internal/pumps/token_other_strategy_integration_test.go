package pumps

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

// newTestUUID generates a random UUIDv4 string without pulling in a UUID
// library dependency just for test fixtures -- mirrors the trades
// package's own helper of the same name (each integration-test file in
// this repo keeps its own minimal fixture helpers rather than sharing
// unexported test code across packages).
func newTestUUID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b) //nolint:errcheck // crypto/rand.Read never returns a short read without an error
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// randomTestBase returns a fresh, never-before-used base per call (colleague
// review: a hardcoded base like "ERA" can collide with a real app.pump_events
// row this repo's own shared dev database happens to have -- Token would
// then return pumpEntry before ever reaching the momentum_flow fallback this
// test exists to exercise, and the test would fail for a reason that has
// nothing to do with this fix. Prefixed with a letter and uppercased so it
// satisfies isValidBase and matches Token's own uppercasing of the URL
// param.
func randomTestBase(t *testing.T) string {
	t.Helper()
	b := make([]byte, 5)
	_, _ = rand.Read(b) //nolint:errcheck // crypto/rand.Read never returns a short read without an error
	return "T" + strings.ToUpper(hex.EncodeToString(b))
}

func insertOtherStrategyPaperRun(t *testing.T, pool *pgxpool.Pool, paperVersion string) {
	t.Helper()
	_, err := pool.Exec(context.Background(), `
		INSERT INTO app.momentum_flow_paper_runs
			(paper_version, contract_sha256, contract_json, cohort_started_at)
		VALUES ($1, repeat('c', 64), '{}'::jsonb, now())
		ON CONFLICT (paper_version) DO NOTHING`,
		paperVersion,
	)
	if err != nil {
		t.Fatalf("insertOtherStrategyPaperRun: %v", err)
	}
}

// insertOpenedPaperProbe inserts one app.momentum_flow_paper_probes row shaped
// like a real opened position: entry_status='opened' with BOTH symbol (the
// native, watch-time form, e.g. "TESTUSDT") and unified_symbol (the
// CCXT-unified form, e.g. "TEST/USDT:USDT") set, exactly as open_entry
// (momentum_flow_paper_repository.py) writes them together in production --
// see CombinedTradesCTE's own doc comment on why matching only the native
// `symbol` column against a unified base pattern silently misses every
// momentum_flow row.
func insertOpenedPaperProbe(t *testing.T, pool *pgxpool.Pool, paperVersion, symbol, unifiedSymbol string) {
	t.Helper()
	now := time.Now().UTC()
	paperID := newTestUUID()
	watchID := newTestUUID()
	episodeID := newTestUUID()

	_, err := pool.Exec(context.Background(), `
		INSERT INTO app.momentum_flow_paper_probes
			(paper_id, paper_version, watch_version, watch_id, episode_id,
			 exchange, market_type, symbol,
			 watch_bucket_start, watch_decision_at, claimed_at,
			 entry_status, unified_symbol, entry_vwap, entry_filled_notional_usd, entry_at,
			 position_status)
		VALUES
			($1::uuid, $2, 'test_watch_v1', $3::uuid, $4::uuid,
			 'bybit', 'linear', $5,
			 $6::timestamptz, $7::timestamptz, $7::timestamptz,
			 'opened', $8, 10.5, 50.0, $7::timestamptz,
			 'open')`,
		paperID, paperVersion, watchID, episodeID, symbol,
		now.Add(-10*time.Minute), now.Add(-9*time.Minute), unifiedSymbol,
	)
	if err != nil {
		t.Fatalf("insertOpenedPaperProbe: %v", err)
	}
}

func cleanupOtherStrategyPaperTestRows(pool *pgxpool.Pool, paperVersion string) {
	// ON DELETE RESTRICT on momentum_flow_paper_probes.paper_version means the
	// probes row must go first, then the parent run row.
	ctx := context.Background()
	_, _ = pool.Exec(ctx, `DELETE FROM app.momentum_flow_paper_probes WHERE paper_version = $1`, paperVersion) //nolint:errcheck
	_, _ = pool.Exec(ctx, `DELETE FROM app.momentum_flow_paper_runs WHERE paper_version = $1`, paperVersion)   //nolint:errcheck
}

// TestTokenFindsMomentumFlowActivityAgainstRealPostgres is a regression for
// the colleague-review finding on fix/token-activity-non-pump-assets-v1:
// app.momentum_flow_paper_probes.symbol is the native, watch-time form
// ("ERAUSDT"), not the CCXT-unified form CombinedTradesCTE's app.trades arm
// uses -- the unified form only exists in that table's own separate
// unified_symbol column. Matching base against plain `symbol` (the original,
// broken version of this fix) would silently never match a momentum_flow
// row; only a real Postgres connection against the real column layout
// proves the normalized_base fix actually works, the way the stub-based
// tests in handler_test.go cannot.
func TestTokenFindsMomentumFlowActivityAgainstRealPostgres(t *testing.T) {
	pool := connectTestPool(t)
	defer pool.Close()

	// Both freshly generated per run (colleague review): base can never
	// collide with a real app.pump_events row already sitting in the shared
	// dev database, and paperVersion can never collide with a concurrent or
	// previously-aborted run of this same test or the trades package's own
	// momentum_flow_paper integration tests -- Go runs different packages'
	// tests concurrently by default, and this exact "ERAUSDT" native symbol
	// used to be hardcoded here AND in trades/combined_cte_integration_test.go.
	base := randomTestBase(t)
	paperVersion := "test_other_strategy_v1_" + newTestUUID()
	nativeSymbol := base + "USDT"
	unifiedSymbol := base + "/USDT:USDT"

	defer cleanupOtherStrategyPaperTestRows(pool, paperVersion)
	insertOtherStrategyPaperRun(t, pool, paperVersion)
	insertOpenedPaperProbe(t, pool, paperVersion, nativeSymbol, unifiedSymbol)

	// Token checks the live Redis snapshot before falling through to the DB
	// -- an empty snapshot (no pumps:latest key at all) exercises exactly
	// the same redis.Nil fallthrough as production when nothing is
	// currently pumping.
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })

	h := &Handler{rdb: rdb, pool: &poolAdapter{inner: pool}}
	router := chi.NewRouter()
	router.Get("/api/pumps/{base}", h.Token)
	req := httptest.NewRequest(http.MethodGet, "/api/pumps/"+base, nil)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp tokenNoPumpResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v (%s)", err, w.Body.String())
	}
	if resp.HasPumpEpisode {
		t.Errorf("has_pump_episode = true, want false (%s has no app.pump_events row)", base)
	}
	if resp.OtherStrategyKey != "momentum_flow_v1" {
		t.Errorf("other_strategy_key = %q, want momentum_flow_v1", resp.OtherStrategyKey)
	}
}

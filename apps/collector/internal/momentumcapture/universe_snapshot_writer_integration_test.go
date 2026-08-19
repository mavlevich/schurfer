package momentumcapture

import (
	"context"
	"crypto/sha256"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mavlevich/schurfer/collector/internal/momentumsource"
)

// testUniverseSnapshotPool connects to the same local dev Postgres as
// writer_integration_test.go's own testDatabaseURL, skipping (not failing)
// when none is reachable -- see that file's own doc comment on why a real
// database, not a stub, is needed here: a stub cannot catch a genuine
// schema mismatch, a CHECK constraint this package's own code assumes
// holds, or whether the atomic all-or-nothing write is actually atomic.
//
// Closes the pool via t.Cleanup, not a caller-owned defer: t.Cleanup
// funcs run in last-registered-first-run order, so as long as every
// caller registers cleanUniverseSnapshotRows's own t.Cleanup AFTER this
// one returns (every test below does), the row DELETE always runs while
// the pool is still open, not after. An earlier version of this file had
// callers `defer pool.Close()` locally instead -- a real bug, not just a
// style choice: that defer fires before ANY t.Cleanup func does, so every
// row-cleanup ran against an already-closed pool and silently did
// nothing (the error was discarded), leaving every test run's rows
// permanently behind for the next one to collide with.
func testUniverseSnapshotPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, testDatabaseURL)
	if err != nil {
		t.Skipf("no local dev postgres reachable: %v", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		t.Skipf("no local dev postgres reachable: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

func cleanUniverseSnapshotRows(t *testing.T, pool *pgxpool.Pool, exchange string) {
	t.Helper()
	t.Cleanup(func() {
		//nolint:errcheck,gosec // best-effort cleanup: a failed DELETE must not fail the test itself
		pool.Exec(context.Background(), `DELETE FROM app.momentum_universe_snapshots WHERE exchange = $1`, exchange)
	})
}

func readyInstrument(exchange, marketID string, onboardedAt time.Time) momentumsource.Instrument {
	return momentumsource.NewInstrument(
		exchange, marketID, "BASE", "USDT", "USDT",
		"LinearPerpetual", "linear_usdt_perpetual", &onboardedAt, onboardedAt.Add(time.Hour),
	)
}

func TestPersistUniverseSnapshotAgainstRealPostgres(t *testing.T) {
	pool := testUniverseSnapshotPool(t)
	ctx := context.Background()

	// A clean exchange name for this test run, so it never collides with
	// real canary rows or other test runs sharing this dev database.
	exchange := "writertest-basic"
	cleanUniverseSnapshotRows(t, pool, exchange)

	onboardedAt := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	instruments := []momentumsource.Instrument{
		readyInstrument(exchange, "BTCUSDT", onboardedAt),
		momentumsource.NewInstrument(exchange, "MISSINGUSDT", "MISSING", "USDT", "USDT", "LinearPerpetual", "linear_usdt_perpetual", nil, onboardedAt),
	}

	w := NewUniverseSnapshotWriter(pool)
	capturedAt := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)
	if err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1", instruments, capturedAt); err != nil {
		t.Fatalf("PersistUniverseSnapshot: %v", err)
	}

	var instrumentCount int
	var storedCaptureVersion, storedSchemaVersion string
	if err := pool.QueryRow(ctx,
		`SELECT instrument_count, capture_version, schema_version
		 FROM app.momentum_universe_snapshots
		 WHERE exchange = $1 AND universe_version = $2`,
		exchange, "universe-hash-1",
	).Scan(&instrumentCount, &storedCaptureVersion, &storedSchemaVersion); err != nil {
		t.Fatalf("read back snapshot row: %v", err)
	}
	if instrumentCount != 2 || storedCaptureVersion != "v1" || storedSchemaVersion != IdentitySchemaVersion {
		t.Fatalf("snapshot row = count=%d capture=%q schema=%q", instrumentCount, storedCaptureVersion, storedSchemaVersion)
	}

	rows, err := pool.Query(ctx,
		`SELECT native_market_id, identity_status, identity_key, onboarded_at
		 FROM app.momentum_universe_instruments
		 WHERE exchange = $1 ORDER BY native_market_id`,
		exchange,
	)
	if err != nil {
		t.Fatalf("query instrument rows: %v", err)
	}
	defer rows.Close()

	type row struct {
		marketID    string
		status      string
		identityKey *string
		onboardedAt *time.Time
	}
	var got []row
	for rows.Next() {
		var r row
		if err := rows.Scan(&r.marketID, &r.status, &r.identityKey, &r.onboardedAt); err != nil {
			t.Fatalf("scan instrument row: %v", err)
		}
		got = append(got, r)
	}
	if len(got) != 2 {
		t.Fatalf("got %d instrument rows, want 2", len(got))
	}
	if got[0].marketID != "BTCUSDT" || got[0].status != "ready" || got[0].identityKey == nil || got[0].onboardedAt == nil {
		t.Fatalf("BTCUSDT row = %+v, want ready with a non-NULL identity_key/onboarded_at", got[0])
	}
	// Regression: the fail-closed invariant round-trips through Postgres
	// exactly as the CHECK constraint requires -- a non-ready row NEVER
	// carries a non-NULL identity_key, not even one this code could have
	// silently produced from partial data.
	if got[1].marketID != "MISSINGUSDT" || got[1].status != "missing_onboarded_at" ||
		got[1].identityKey != nil || got[1].onboardedAt != nil {
		t.Fatalf("MISSINGUSDT row = %+v, want missing_onboarded_at with NULL identity_key/onboarded_at", got[1])
	}
}

func TestPersistUniverseSnapshotIsIdempotent(t *testing.T) {
	pool := testUniverseSnapshotPool(t)
	ctx := context.Background()

	exchange := "writertest-idempotent"
	cleanUniverseSnapshotRows(t, pool, exchange)

	onboardedAt := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	instruments := []momentumsource.Instrument{readyInstrument(exchange, "BTCUSDT", onboardedAt)}

	w := NewUniverseSnapshotWriter(pool)
	capturedAt := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)
	if err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1", instruments, capturedAt); err != nil {
		t.Fatalf("first PersistUniverseSnapshot: %v", err)
	}
	// A real retry (e.g. the capture binary restarted before confirming
	// its own write) must succeed without duplicating or erroring, even
	// with a different capturedAt (the wall-clock time of this SPECIFIC
	// attempt, not part of what makes two snapshots "the same").
	later := capturedAt.Add(time.Minute)
	if err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1", instruments, later); err != nil {
		t.Fatalf("second (idempotent retry) PersistUniverseSnapshot: %v", err)
	}

	var snapshotRows, instrumentRows int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM app.momentum_universe_snapshots WHERE exchange = $1`, exchange).Scan(&snapshotRows); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM app.momentum_universe_instruments WHERE exchange = $1`, exchange).Scan(&instrumentRows); err != nil {
		t.Fatal(err)
	}
	if snapshotRows != 1 || instrumentRows != 1 {
		t.Fatalf("snapshot rows = %d, instrument rows = %d, want 1/1 (idempotent retry must not duplicate)", snapshotRows, instrumentRows)
	}
}

func TestPersistUniverseSnapshotRejectsPayloadMismatchUnderTheSameKey(t *testing.T) {
	pool := testUniverseSnapshotPool(t)
	ctx := context.Background()

	exchange := "writertest-mismatch"
	cleanUniverseSnapshotRows(t, pool, exchange)

	onboardedAt := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	instruments := []momentumsource.Instrument{readyInstrument(exchange, "BTCUSDT", onboardedAt)}

	w := NewUniverseSnapshotWriter(pool)
	capturedAt := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)
	if err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1", instruments, capturedAt); err != nil {
		t.Fatalf("first PersistUniverseSnapshot: %v", err)
	}

	// A real mismatch under the same natural key is, by construction,
	// something PersistUniverseSnapshot itself cannot produce from normal
	// parameter variation (catalog_version, instrument_count, and every
	// metadata_hash are all derived from the same instrument content, so
	// identical instruments always produce an identical payload_hash).
	// This simulates the invariant PersistUniverseSnapshot actually
	// protects against instead: the already-persisted row's own
	// payload_hash gets corrupted directly (a stand-in for a hash-
	// computation change without a schema_version bump, or any other way
	// a stored row could end up not matching what a fresh, correct
	// computation produces for the SAME identity content).
	corruptedHash := sha256.Sum256([]byte("not the real payload"))
	if _, err := pool.Exec(ctx,
		`UPDATE app.momentum_universe_snapshots SET payload_hash = $1
		 WHERE exchange = $2 AND universe_version = $3`,
		corruptedHash[:], exchange, "universe-hash-1",
	); err != nil {
		t.Fatalf("corrupt stored payload_hash: %v", err)
	}

	// Same instruments, so PersistUniverseSnapshot computes the exact
	// same catalog_version and lands on the exact same row -- whose
	// payload_hash no longer matches.
	err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1", instruments, capturedAt)
	if !errors.Is(err, ErrSnapshotPayloadMismatch) {
		t.Fatalf("PersistUniverseSnapshot() error = %v, want ErrSnapshotPayloadMismatch", err)
	}
}

// TestPersistUniverseSnapshotCaptureVersionChangeIsNotAMismatch is a
// regression for a code-review finding: an earlier version of
// computePayloadHash included captureVersion, so a routine
// momentumcapture.CaptureVersion bump -- unrelated to instrument identity
// -- would have made an otherwise-unchanged catalog fail as
// ErrSnapshotPayloadMismatch on the next restart, refusing to start the
// live Bybit capture process over a bar-schema change that never touched
// the instrument catalog.
func TestPersistUniverseSnapshotCaptureVersionChangeIsNotAMismatch(t *testing.T) {
	pool := testUniverseSnapshotPool(t)
	ctx := context.Background()

	exchange := "writertest-captureversion"
	cleanUniverseSnapshotRows(t, pool, exchange)

	onboardedAt := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	instruments := []momentumsource.Instrument{readyInstrument(exchange, "BTCUSDT", onboardedAt)}

	w := NewUniverseSnapshotWriter(pool)
	capturedAt := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)
	if err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1", instruments, capturedAt); err != nil {
		t.Fatalf("first PersistUniverseSnapshot: %v", err)
	}
	// Same exchange/universe_version/instruments, only captureVersion
	// differs -- must succeed as an idempotent no-op, not
	// ErrSnapshotPayloadMismatch.
	if err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v2-different-bar-schema", instruments, capturedAt); err != nil {
		t.Fatalf("PersistUniverseSnapshot with only captureVersion changed: %v, want success", err)
	}

	var storedCaptureVersion string
	if err := pool.QueryRow(ctx,
		`SELECT capture_version FROM app.momentum_universe_snapshots WHERE exchange = $1`, exchange,
	).Scan(&storedCaptureVersion); err != nil {
		t.Fatal(err)
	}
	if storedCaptureVersion != "v1" {
		t.Fatalf("stored capture_version = %q, want the original v1 (an idempotent match never overwrites provenance either)", storedCaptureVersion)
	}
}

func TestPersistUniverseSnapshotCatalogVersionChangesWhenOnboardedAtChangesButSymbolSetDoesNot(t *testing.T) {
	pool := testUniverseSnapshotPool(t)
	ctx := context.Background()

	exchange := "writertest-relisted"
	cleanUniverseSnapshotRows(t, pool, exchange)

	firstOnboard := time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)
	secondOnboard := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

	w := NewUniverseSnapshotWriter(pool)
	capturedAt := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)

	// Regression for a code-review finding, before implementation started:
	// universe_version alone (a hash of the symbol LIST) cannot detect a
	// symbol delisted and relisted under the same ticker with a new
	// onboarded_at -- the symbol SET is unchanged. catalog_version must
	// still differ.
	if err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1",
		[]momentumsource.Instrument{readyInstrument(exchange, "BTCUSDT", firstOnboard)}, capturedAt); err != nil {
		t.Fatalf("first snapshot: %v", err)
	}
	if err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1",
		[]momentumsource.Instrument{readyInstrument(exchange, "BTCUSDT", secondOnboard)}, capturedAt); err != nil {
		t.Fatalf("second snapshot (same universe_version, changed onboarded_at): %v", err)
	}

	var snapshotCount int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM app.momentum_universe_snapshots WHERE exchange = $1 AND universe_version = $2`,
		exchange, "universe-hash-1",
	).Scan(&snapshotCount); err != nil {
		t.Fatal(err)
	}
	if snapshotCount != 2 {
		t.Fatalf("snapshot count = %d, want 2: a changed onboarded_at under the same universe_version must produce a distinct catalog_version, not overwrite or collide", snapshotCount)
	}
}

// TestPersistUniverseSnapshotIsIdempotentAgainstConcurrentWriters is a
// regression for a code-review finding: the check-then-insert inside
// persistOnce is not atomic across transactions, so concurrent writers
// racing for the SAME (exchange, universe_version, catalog_version) key --
// e.g. two capture processes briefly overlapping during a redeploy, both
// fetching an identical catalog -- must all still see success, not a raw
// unique_violation from whichever one loses the race to INSERT the
// snapshot row. All writers start from the same barrier to maximize the
// odds real concurrent transactions actually collide on Postgres, rather
// than serializing far enough apart that none of them do.
func TestPersistUniverseSnapshotIsIdempotentAgainstConcurrentWriters(t *testing.T) {
	pool := testUniverseSnapshotPool(t)
	ctx := context.Background()

	exchange := "writertest-concurrent"
	cleanUniverseSnapshotRows(t, pool, exchange)

	onboardedAt := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	instruments := []momentumsource.Instrument{readyInstrument(exchange, "BTCUSDT", onboardedAt)}
	capturedAt := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)

	const writers = 8
	var wg sync.WaitGroup
	errs := make([]error, writers)
	start := make(chan struct{})
	for i := 0; i < writers; i++ {
		w := NewUniverseSnapshotWriter(pool)
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			errs[i] = w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1", instruments, capturedAt)
		}(i)
	}
	close(start)
	wg.Wait()

	for i, err := range errs {
		if err != nil {
			t.Fatalf("writer %d: PersistUniverseSnapshot: %v, want nil (concurrent identical writes must all succeed)", i, err)
		}
	}

	var snapshotRows, instrumentRows int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM app.momentum_universe_snapshots WHERE exchange = $1`, exchange).Scan(&snapshotRows); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM app.momentum_universe_instruments WHERE exchange = $1`, exchange).Scan(&instrumentRows); err != nil {
		t.Fatal(err)
	}
	if snapshotRows != 1 || instrumentRows != 1 {
		t.Fatalf("snapshot rows = %d, instrument rows = %d, want 1/1 (concurrent identical writers must not duplicate)", snapshotRows, instrumentRows)
	}
}

// TestMomentumUniverseInstrumentsRejectsOnboardedAtOnNonReadyRow is a
// regression for a code-review finding: identity_key_only_when_ready must
// be symmetric. An earlier version of the migration only forced
// identity_key to be NULL on a non-ready row, leaving onboarded_at
// unconstrained on that branch -- this bypasses PersistUniverseSnapshot
// entirely (which never sets onboarded_at on a non-ready Instrument in the
// first place) with a raw INSERT, to prove the DB itself refuses this
// shape, not just the application code above it.
func TestMomentumUniverseInstrumentsRejectsOnboardedAtOnNonReadyRow(t *testing.T) {
	pool := testUniverseSnapshotPool(t)
	ctx := context.Background()

	exchange := "writertest-checkconstraint"
	cleanUniverseSnapshotRows(t, pool, exchange)

	onboardedAt := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	w := NewUniverseSnapshotWriter(pool)
	capturedAt := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)
	if err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1",
		[]momentumsource.Instrument{readyInstrument(exchange, "BTCUSDT", onboardedAt)}, capturedAt); err != nil {
		t.Fatalf("seed snapshot: %v", err)
	}

	var catalogVersion string
	if err := pool.QueryRow(ctx,
		`SELECT catalog_version FROM app.momentum_universe_snapshots WHERE exchange = $1 AND universe_version = $2`,
		exchange, "universe-hash-1",
	).Scan(&catalogVersion); err != nil {
		t.Fatalf("read back catalog_version: %v", err)
	}

	metadataHash := sha256.Sum256([]byte("check-constraint-regression"))
	_, err := pool.Exec(ctx,
		`INSERT INTO app.momentum_universe_instruments (
			exchange, universe_version, catalog_version, native_market_id,
			base, quote, settle, native_market_type, canonical_market_type,
			onboarded_at, identity_status, identity_key, metadata_hash
		) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)`,
		exchange, "universe-hash-1", catalogVersion, "BADUSDT",
		"", "USDT", "USDT", "LinearPerpetual", "linear_usdt_perpetual",
		onboardedAt, "invalid_assets", nil, metadataHash[:],
	)
	if err == nil {
		t.Fatal("expected a CHECK constraint violation: a non-ready row must not carry a non-NULL onboarded_at")
	}
	var pgErr *pgconn.PgError
	if !errors.As(err, &pgErr) || pgErr.ConstraintName != "identity_key_only_when_ready" {
		t.Fatalf("INSERT error = %v, want identity_key_only_when_ready CHECK violation", err)
	}
}

func TestPersistUniverseSnapshotIsAtomicOnAConstraintViolation(t *testing.T) {
	pool := testUniverseSnapshotPool(t)
	ctx := context.Background()

	exchange := "writertest-atomic"
	cleanUniverseSnapshotRows(t, pool, exchange)

	onboardedAt := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	// Two DIFFERENT Instrument values that both resolve to the same
	// native_market_id: a real (if contrived) way to force the second
	// instrument INSERT in the batch to violate the instruments table's
	// own primary key, without mocking pgx.Tx.
	duplicateMarketID := []momentumsource.Instrument{
		readyInstrument(exchange, "DUPUSDT", onboardedAt),
		readyInstrument(exchange, "DUPUSDT", onboardedAt.Add(time.Hour)),
	}

	w := NewUniverseSnapshotWriter(pool)
	capturedAt := time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC)
	err := w.PersistUniverseSnapshot(ctx, exchange, "universe-hash-1", "v1", duplicateMarketID, capturedAt)
	if err == nil {
		t.Fatal("expected a primary-key violation on the duplicate native_market_id")
	}

	// The real assertion: the SNAPSHOT row (inserted successfully, before
	// the instrument batch ever ran) must not survive either -- the whole
	// transaction rolled back, not just the failing statement.
	var snapshotCount int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM app.momentum_universe_snapshots WHERE exchange = $1`, exchange,
	).Scan(&snapshotCount); err != nil {
		t.Fatal(err)
	}
	if snapshotCount != 0 {
		t.Fatal("a failed instrument insert must roll back the already-inserted snapshot row too: partial writes are exactly what this type exists to prevent")
	}
}

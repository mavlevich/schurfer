package momentumcapture

import (
	"context"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mavlevich/schurfer/collector/internal/momentum"
)

// testDatabaseURL matches infra/docker/docker-compose.dev.yml's local dev
// Postgres. This test is real-database, not a stub: stubWriterDB in
// writer_test.go proves the Writer's own logic, but only a real connection
// can catch a genuine pgx encoding/schema mismatch (array element types,
// NULL handling, the RETURNING clause actually matching insertRowSQL).
// Skips instead of failing when no local Postgres is reachable, so `go
// test ./...` still passes in an environment without docker-compose.dev up.
const testDatabaseURL = "postgres://schurfer:schurfer_dev@localhost:5432/schurfer"

func TestWriterFlushAgainstRealPostgres(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, testDatabaseURL)
	if err != nil {
		t.Skipf("no local dev postgres reachable: %v", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		t.Skipf("no local dev postgres reachable: %v", err)
	}

	// A clean symbol name for this test run, so it never collides with real
	// canary rows or other test runs sharing this dev database.
	symbol := "WRITERTESTUSDT"
	defer pool.Exec(context.Background(), `DELETE FROM timeseries.bybit_momentum_bars_1m WHERE symbol = $1`, symbol) //nolint:errcheck

	w := NewWriter(pool, "bybit", "linear", "test-universe-hash")
	bucket := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	price := 100.0
	bid, ask := 99.9, 100.1
	oi := 12345.6

	bar := momentum.Bar{
		Symbol:       symbol,
		BucketStart:  bucket,
		OpenPrice:    &price,
		HighPrice:    &price,
		LowPrice:     &price,
		ClosePrice:   &price,
		LastBidPrice: &bid,
		LastAskPrice: &ask,
		Buy: momentum.SideStats{
			TotalNotionalUSD: 500,
			TradeCount:       3,
			Histogram:        histogramWithOneHit(500),
			TopNotionalsUSD:  []float64{500},
		},
		Sell: momentum.SideStats{
			Histogram: histogramWithOneHit(0),
			// TopNotionalsUSD left nil deliberately: the side that never
			// traded this minute, exactly the shape production must handle.
		},
		OpenInterest:             &oi,
		TickerObservedThisMinute: true,
		TradeCount:               3,
		TickerComplete:           true,
		TradesComplete:           true,
		Complete:                 true,
	}

	// First insert: must succeed as a fresh row.
	w.Enqueue([]momentum.Bar{bar})
	if err := w.Flush(ctx); err != nil {
		t.Fatalf("first flush: %v", err)
	}
	if stats := w.Stats(); stats.BarsPersistedTotal != 1 || stats.PayloadHashMismatchTotal != 0 {
		t.Fatalf("stats after first flush = %+v", stats)
	}

	var storedTopNotional []float64
	var storedComplete bool
	if err := pool.QueryRow(ctx,
		`SELECT sell_top_notional, complete FROM timeseries.bybit_momentum_bars_1m WHERE symbol = $1`,
		symbol,
	).Scan(&storedTopNotional, &storedComplete); err != nil {
		t.Fatalf("read back: %v", err)
	}
	if storedTopNotional == nil || len(storedTopNotional) != 0 {
		t.Fatalf("sell_top_notional round-tripped as %#v, want empty non-NULL array", storedTopNotional)
	}
	if !storedComplete {
		t.Fatal("complete should have round-tripped as true")
	}

	// Second insert of the identical bar: a harmless retry, same PK, same
	// content, must not be flagged as a hash mismatch.
	w.Enqueue([]momentum.Bar{bar})
	if err := w.Flush(ctx); err != nil {
		t.Fatalf("retry flush: %v", err)
	}
	if got := w.Stats().PayloadHashMismatchTotal; got != 0 {
		t.Fatalf("retrying the identical bar must not count as a hash mismatch, got %d", got)
	}

	var rowCount int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM timeseries.bybit_momentum_bars_1m WHERE symbol = $1`, symbol,
	).Scan(&rowCount); err != nil {
		t.Fatalf("count rows: %v", err)
	}
	if rowCount != 1 {
		t.Fatalf("row count = %d, want 1 (ON CONFLICT must not duplicate)", rowCount)
	}

	// Third insert: same PK, DIFFERENT content (a genuine, if artificial,
	// collision) must be flagged as a mismatch, not silently accepted.
	mutated := bar
	changedPrice := 999.0
	mutated.ClosePrice = &changedPrice
	w.Enqueue([]momentum.Bar{mutated})
	if err := w.Flush(ctx); err != nil {
		t.Fatalf("mutated flush: %v", err)
	}
	if got := w.Stats().PayloadHashMismatchTotal; got != 1 {
		t.Fatalf("PayloadHashMismatchTotal = %d, want 1 after a same-PK different-content write", got)
	}
}

func histogramWithOneHit(notional float64) []momentum.HistogramBucket {
	buckets := make([]momentum.HistogramBucket, 11)
	if notional > 0 {
		buckets[0].Count = 1
		buckets[0].NotionalUSD = notional
	}
	return buckets
}

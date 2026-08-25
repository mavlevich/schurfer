package liquidationcapture

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

const testDatabaseURL = "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"

func integrationPool(t *testing.T) *pgxpool.Pool {
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
	var exists bool
	if err := pool.QueryRow(ctx, `SELECT to_regclass('timeseries.liquidation_events') IS NOT NULL`).Scan(&exists); err != nil || !exists {
		pool.Close()
		t.Skip("migration 0038 is not applied")
	}
	t.Cleanup(pool.Close)
	return pool
}

func TestWriterPersistsDeduplicatesAndDetectsPayloadMismatchAgainstRealPostgres(t *testing.T) {
	pool := integrationPool(t)
	ctx := context.Background()
	exchange := fmt.Sprintf("m38_%d", time.Now().UnixNano())
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(),
			`DELETE FROM timeseries.liquidation_events WHERE exchange = $1`, exchange)
	})

	input := validEvent()
	input.Exchange = exchange
	input.RawPayload = json.RawMessage(`{"native":"first"}`)
	event, err := NewEvent(input, "same-native-event")
	if err != nil {
		t.Fatal(err)
	}
	writer := &Writer{db: pool}
	if !writer.Enqueue(event) {
		t.Fatal("first enqueue rejected")
	}
	if err := writer.Flush(ctx); err != nil {
		t.Fatal(err)
	}
	if !writer.Enqueue(event) {
		t.Fatal("retry enqueue rejected")
	}
	if err := writer.Flush(ctx); err != nil {
		t.Fatal(err)
	}
	stats := writer.Stats()
	if stats.EventsPersistedTotal != 1 || stats.DuplicateEventsTotal != 1 {
		t.Fatalf("idempotent retry stats = %+v", stats)
	}

	mismatch := event
	mismatch.RawPayload = json.RawMessage(`{"native":"different"}`)
	mismatch.PayloadHash = sha256Sum(mismatch.RawPayload)
	if !writer.Enqueue(mismatch) {
		t.Fatal("mismatch enqueue rejected")
	}
	if err := writer.Flush(ctx); err != nil {
		t.Fatal(err)
	}
	if writer.Stats().PayloadHashMismatchTotal != 1 {
		t.Fatalf("payload mismatch was not exposed: %+v", writer.Stats())
	}

	var rows int
	if err := pool.QueryRow(ctx,
		`SELECT count(*) FROM timeseries.liquidation_events WHERE exchange = $1`, exchange,
	).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 1 {
		t.Fatalf("stored rows = %d, want exactly one", rows)
	}
}

func sha256Sum(payload []byte) [32]byte {
	return sha256.Sum256(payload)
}

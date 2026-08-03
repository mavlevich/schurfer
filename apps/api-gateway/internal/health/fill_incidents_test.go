package health

import (
	"context"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

type stubRow struct {
	value string
	err   error
}

func (r stubRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	target, ok := dest[0].(*string)
	if !ok {
		return nil
	}
	*target = r.value
	return nil
}

type stubDB struct {
	row stubRow
}

func (db *stubDB) QueryRow(_ context.Context, _ string, _ ...any) pgxRow {
	return db.row
}

func testRedisClient(t *testing.T) *redis.Client {
	t.Helper()
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	return client
}

func TestCheckFillIncidentsReportsOpenIncidentsAndPnlReadyState(t *testing.T) {
	client := testRedisClient(t)
	if err := client.Set(context.Background(), "risk:pnl_ready", "1", 0).Err(); err != nil {
		t.Fatal(err)
	}
	db := &stubDB{row: stubRow{value: `[{
		"id": 7,
		"exchange": "bybit",
		"base": "BEAT",
		"operation": "open",
		"order_id": "ord-1",
		"status": "pending",
		"attempt_count": 2,
		"last_error": "fill price still unresolved",
		"created_at": "2026-08-03T18:00:00Z"
	}]`}}

	got := (&Checker{rdb: client, db: db}).checkFillIncidents(context.Background())
	if got == nil {
		t.Fatal("expected fill-incidents report")
	}
	if !got.PnlReady {
		t.Fatalf("expected pnl_ready true, got %+v", got)
	}
	if len(got.Open) != 1 {
		t.Fatalf("expected one open incident, got %+v", got.Open)
	}
	incident := got.Open[0]
	if incident.ID != 7 || incident.Exchange != "bybit" || incident.Base != "BEAT" {
		t.Fatalf("unexpected incident identity: %+v", incident)
	}
	if incident.Status != "pending" || incident.AttemptCount != 2 {
		t.Fatalf("unexpected incident state: %+v", incident)
	}
	if incident.LastError == nil || *incident.LastError != "fill price still unresolved" {
		t.Fatalf("unexpected last_error: %+v", incident.LastError)
	}
}

func TestCheckFillIncidentsReportsPnlNotReadyWhenLeaseMissing(t *testing.T) {
	client := testRedisClient(t)
	db := &stubDB{row: stubRow{value: `[]`}}

	got := (&Checker{rdb: client, db: db}).checkFillIncidents(context.Background())
	if got == nil {
		t.Fatal("expected fill-incidents report")
	}
	if got.PnlReady {
		t.Fatalf("expected pnl_ready false when risk:pnl_ready is absent, got %+v", got)
	}
	if len(got.Open) != 0 {
		t.Fatalf("expected no open incidents, got %+v", got.Open)
	}
}

func TestCheckFillIncidentsOmitsReportOnQueryError(t *testing.T) {
	client := testRedisClient(t)
	if err := client.Set(context.Background(), "risk:pnl_ready", "1", 0).Err(); err != nil {
		t.Fatal(err)
	}
	db := &stubDB{row: stubRow{err: context.DeadlineExceeded}}

	got := (&Checker{rdb: client, db: db}).checkFillIncidents(context.Background())
	if got != nil {
		t.Fatalf("expected nil report on query failure, got %+v", got)
	}
}

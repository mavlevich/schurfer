package notifier

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type stubAlertDB struct {
	query  string
	args   []any
	err    error
	closed bool
	row    stubAlertRow
}

type stubAlertRow struct {
	values []any
	err    error
}

func (row stubAlertRow) Scan(dest ...any) error {
	if row.err != nil {
		return row.err
	}
	for index, value := range row.values {
		switch pointer := dest[index].(type) {
		case *int:
			*pointer = value.(int)
		case *int64:
			*pointer = value.(int64)
		case *[]int64:
			*pointer = value.([]int64)
		default:
			return errors.New("unexpected scan destination")
		}
	}
	return nil
}

func (db *stubAlertDB) Exec(
	_ context.Context,
	query string,
	args ...any,
) (pgconn.CommandTag, error) {
	db.query = query
	db.args = args
	return pgconn.NewCommandTag("INSERT 0 1"), db.err
}

func (db *stubAlertDB) QueryRow(
	_ context.Context,
	query string,
	args ...any,
) pgx.Row {
	db.query = query
	db.args = args
	return db.row
}

func (db *stubAlertDB) Close() {
	db.closed = true
}

func TestPostgresAlertRecorderRecordsIdempotentDelivery(t *testing.T) {
	db := &stubAlertDB{}
	recorder := &postgresAlertRecorder{pool: db}
	now := time.Now().UTC()
	high24h := 80.0
	tickerAt := now.Add(-3 * time.Second)
	delivery := alertDelivery{
		EventID:               42,
		Base:                  "BTC",
		Exchange:              "binance",
		ThresholdPct:          60,
		ObservedChangePct:     65,
		Exchange24hHighPct:    &high24h,
		TickerAt:              &tickerAt,
		ScannerObservedAt:     now.Add(-2 * time.Second),
		ScanPublishedAt:       now.Add(-time.Second),
		NotificationStartedAt: now.Add(-100 * time.Millisecond),
		NotificationSentAt:    now,
	}

	if err := recorder.Record(context.Background(), delivery); err != nil {
		t.Fatal(err)
	}

	if !strings.Contains(db.query, "INSERT INTO app.pump_alert_deliveries") {
		t.Errorf("unexpected query: %s", db.query)
	}
	if !strings.Contains(db.query, "ON CONFLICT") {
		t.Error("delivery insert must remain idempotent")
	}
	if len(db.args) != 11 {
		t.Fatalf("args = %d, want 11", len(db.args))
	}
	if db.args[0] != int64(42) || db.args[1] != "BTC" || db.args[2] != "binance" {
		t.Errorf("unexpected identity args: %v", db.args[:3])
	}
	if db.args[3] != float64(60) || db.args[4] != float64(65) {
		t.Errorf("unexpected measurement args: %v", db.args[3:5])
	}
}

func TestPostgresAlertRecorderPropagatesDatabaseError(t *testing.T) {
	want := errors.New("database unavailable")
	recorder := &postgresAlertRecorder{pool: &stubAlertDB{err: want}}

	err := recorder.Record(context.Background(), alertDelivery{})

	if !errors.Is(err, want) {
		t.Fatalf("error = %v, want %v", err, want)
	}
}

func TestPostgresAlertRecorderReadsSourceLeadHealth(t *testing.T) {
	db := &stubAlertDB{row: stubAlertRow{values: []any{2, []int64{40, 41}}}}
	recorder := &postgresAlertRecorder{pool: db}

	health, err := recorder.ReadSourceLeadHealth(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if health.StaleCollecting != 2 ||
		!reflect.DeepEqual(health.CriticalAbandonedIDs, []int64{40, 41}) {
		t.Fatalf("health = %#v", health)
	}
	if !strings.Contains(db.query, "capture_worker_failed:%") ||
		strings.Contains(db.query, "collector_process_restarted") {
		t.Fatalf("health query does not isolate critical failures: %s", db.query)
	}
}

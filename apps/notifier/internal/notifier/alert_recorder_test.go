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
	query    string
	args     []any
	err      error
	closed   bool
	row      stubAlertRow
	rows     *stubAlertRows
	queryErr error
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

func (db *stubAlertDB) Query(
	_ context.Context,
	query string,
	args ...any,
) (pgx.Rows, error) {
	db.query = query
	db.args = args
	if db.queryErr != nil {
		return nil, db.queryErr
	}
	if db.rows == nil {
		return &stubAlertRows{}, nil
	}
	return db.rows, nil
}

func (db *stubAlertDB) Close() {
	db.closed = true
}

// stubAlertRows is a minimal pgx.Rows fake: only Next/Scan/Err/Close do
// real work (everything this package's own multi-row readers actually
// use), the rest of the interface returns zero values since nothing here
// calls them.
type stubAlertRows struct {
	values [][]any
	index  int
	err    error
}

func (r *stubAlertRows) Close()                                       {}
func (r *stubAlertRows) Err() error                                   { return r.err }
func (r *stubAlertRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (r *stubAlertRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (r *stubAlertRows) Values() ([]any, error)                       { return nil, nil }
func (r *stubAlertRows) RawValues() [][]byte                          { return nil }
func (r *stubAlertRows) Conn() *pgx.Conn                              { return nil }

func (r *stubAlertRows) Next() bool {
	if r.index >= len(r.values) {
		return false
	}
	r.index++
	return true
}

func (r *stubAlertRows) Scan(dest ...any) error {
	row := r.values[r.index-1]
	for i, value := range row {
		switch pointer := dest[i].(type) {
		case *string:
			*pointer = value.(string)
		case *float64:
			*pointer = value.(float64)
		case *int:
			*pointer = value.(int)
		case *time.Time:
			*pointer = value.(time.Time)
		case **float64:
			if value == nil {
				*pointer = nil
			} else {
				v := value.(float64)
				*pointer = &v
			}
		case **time.Time:
			if value == nil {
				*pointer = nil
			} else {
				v := value.(time.Time)
				*pointer = &v
			}
		default:
			return errors.New("unexpected scan destination")
		}
	}
	return nil
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

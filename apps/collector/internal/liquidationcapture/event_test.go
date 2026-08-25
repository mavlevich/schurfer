package liquidationcapture

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

func validEvent() Event {
	now := time.Date(2026, 8, 25, 12, 0, 1, 0, time.UTC)
	price := 100.0
	notional := 250.0
	return Event{
		Exchange: "bybit", MarketType: "linear", NativeMarketID: "BTCUSDT",
		UniverseVersion:       "universe-v1",
		SourceContractVariant: "bybit_all_liquidation_v1",
		CoverageKind:          CoverageCompleteStream, PositionSide: PositionLong,
		EventAt: now.Add(-time.Second), ExchangePublishedAt: now.Add(-500 * time.Millisecond),
		ReceivedAt: now, SourceSessionID: "session-a", Quantity: 2.5,
		QuantityUnit: "base_asset", BankruptcyPrice: &price,
		EstimatedLiquidationNotional: &notional, RawPayload: json.RawMessage(`{"s":"BTCUSDT"}`),
	}
}

type insertedRow struct{}

func (insertedRow) Scan(dest ...any) error {
	*(dest[0].(*bool)) = true
	*(dest[1].(*[]byte)) = nil
	return nil
}

type insertedBatchResults struct{ remaining int }

func (results *insertedBatchResults) Exec() (pgconn.CommandTag, error) {
	return pgconn.CommandTag{}, errors.New("not used")
}
func (results *insertedBatchResults) Query() (pgx.Rows, error) {
	return nil, errors.New("not used")
}
func (results *insertedBatchResults) QueryRow() pgx.Row {
	results.remaining--
	return insertedRow{}
}
func (results *insertedBatchResults) Close() error { return nil }

type enqueueDuringFlushDB struct {
	writer *Writer
	event  Event
	calls  int
}

func (db *enqueueDuringFlushDB) SendBatch(_ context.Context, batch *pgx.Batch) pgx.BatchResults {
	db.calls++
	if db.calls == 1 {
		db.writer.Enqueue(db.event)
	}
	return &insertedBatchResults{remaining: batch.Len()}
}
func (*enqueueDuringFlushDB) Exec(context.Context, string, ...any) (pgconn.CommandTag, error) {
	return pgconn.CommandTag{}, errors.New("not used")
}
func (*enqueueDuringFlushDB) Close() {}

func TestNewEventStableKeyDoesNotDependOnConnectionSession(t *testing.T) {
	first, err := NewEvent(validEvent(), "native-event-1")
	if err != nil {
		t.Fatal(err)
	}
	secondInput := validEvent()
	secondInput.SourceSessionID = "session-after-reconnect"
	second, err := NewEvent(secondInput, "native-event-1")
	if err != nil {
		t.Fatal(err)
	}
	if first.SourceEventKey != second.SourceEventKey {
		t.Fatal("a replay after reconnect must retain the same deduplication key")
	}
}

func TestNewEventRejectsUnknownCoverageAndFutureTimestamp(t *testing.T) {
	event := validEvent()
	event.CoverageKind = "looks_complete"
	if _, err := NewEvent(event, "id"); err == nil {
		t.Fatal("unknown coverage kind was accepted")
	}
	event = validEvent()
	event.EventAt = event.ReceivedAt.Add(6 * time.Second)
	if _, err := NewEvent(event, "id"); err == nil {
		t.Fatal("implausible future event timestamp was accepted")
	}
}

func TestWriterRejectsNewEventAtCapacityWithoutEvictingOldest(t *testing.T) {
	writer := &Writer{}
	event, err := NewEvent(validEvent(), "first")
	if err != nil {
		t.Fatal(err)
	}
	writer.pending = make([]Event, MaxPendingEvents)
	writer.pending[0] = event
	if writer.Enqueue(event) {
		t.Fatal("enqueue beyond capacity unexpectedly succeeded")
	}
	if writer.pending[0].SourceEventKey != event.SourceEventKey {
		t.Fatal("oldest event was evicted from an append-only ledger")
	}
	if writer.Stats().QueueDropsTotal != 1 {
		t.Fatal("rejected event was not exposed in writer health")
	}
}

func TestEventArgsPinsEveryPersistedField(t *testing.T) {
	event, err := NewEvent(validEvent(), "event-id")
	if err != nil {
		t.Fatal(err)
	}
	if got := len(eventArgs(event)); got != 23 {
		t.Fatalf("eventArgs length = %d, want 23", got)
	}
}

func TestWriterFlushDoesNotChaseEventsEnqueuedAfterFlushStarted(t *testing.T) {
	first, err := NewEvent(validEvent(), "first")
	if err != nil {
		t.Fatal(err)
	}
	secondInput := validEvent()
	secondInput.EventAt = secondInput.EventAt.Add(time.Millisecond)
	second, err := NewEvent(secondInput, "second")
	if err != nil {
		t.Fatal(err)
	}
	writer := &Writer{}
	db := &enqueueDuringFlushDB{writer: writer, event: second}
	writer.db = db
	writer.Enqueue(first)
	if err := writer.Flush(context.Background()); err != nil {
		t.Fatal(err)
	}
	if db.calls != 1 {
		t.Fatalf("SendBatch calls = %d, writer chased new arrivals", db.calls)
	}
	if writer.Stats().QueueDepth != 1 {
		t.Fatalf("queue depth = %d, want the new suffix left for next flush", writer.Stats().QueueDepth)
	}
}

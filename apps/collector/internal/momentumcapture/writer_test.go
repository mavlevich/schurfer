package momentumcapture

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/mavlevich/schurfer/collector/internal/momentum"
)

// stubRowResult is one canned QueryRow() answer: either a scan error, or an
// (inserted, storedHash) pair mimicking `RETURNING (xmax = 0), payload_hash`.
type stubRowResult struct {
	err        error
	inserted   bool
	storedHash []byte
}

type stubRow struct{ result stubRowResult }

func (r stubRow) Scan(dest ...any) error {
	if r.result.err != nil {
		return r.result.err
	}
	*(dest[0].(*bool)) = r.result.inserted
	*(dest[1].(*[]byte)) = r.result.storedHash
	return nil
}

type stubWriterDB struct {
	batches   []*pgx.Batch
	lastBatch *pgx.Batch
	rows      []stubRowResult // one per queued statement in the FIRST sub-batch; later sub-batches default to a fresh insert
	closed    bool
	closeErr  error
	// failCloseOnBatch, if > 0, makes the Nth (1-indexed) SendBatch call's
	// Close() fail, simulating a batch whose individual rows all scanned
	// fine but whose final commit/sync did not.
	failCloseOnBatch int
}

type stubBatchResults struct {
	db       *stubWriterDB
	idx      int
	batchNum int
}

func (r *stubBatchResults) Exec() (pgconn.CommandTag, error) {
	return pgconn.CommandTag{}, errors.New("stubBatchResults.Exec not used by Writer")
}
func (r *stubBatchResults) Query() (pgx.Rows, error) {
	return nil, errors.New("stubBatchResults.Query not used by Writer")
}
func (r *stubBatchResults) QueryRow() pgx.Row {
	defer func() { r.idx++ }()
	if r.batchNum == 1 && r.idx < len(r.db.rows) {
		return stubRow{result: r.db.rows[r.idx]}
	}
	return stubRow{result: stubRowResult{inserted: true}}
}
func (r *stubBatchResults) Close() error {
	if r.db.failCloseOnBatch == r.batchNum {
		if r.db.closeErr != nil {
			return r.db.closeErr
		}
		return errors.New("stub batch close failure")
	}
	return nil
}

func (db *stubWriterDB) SendBatch(_ context.Context, b *pgx.Batch) pgx.BatchResults {
	db.batches = append(db.batches, b)
	db.lastBatch = b
	return &stubBatchResults{db: db, batchNum: len(db.batches)}
}
func (db *stubWriterDB) Close() { db.closed = true }

func testBar(symbol string, minute int) momentum.Bar {
	bucket := time.Date(2026, 8, 10, 12, minute, 0, 0, time.UTC)
	price := 100.0
	return momentum.Bar{
		Symbol:      symbol,
		BucketStart: bucket,
		ClosePrice:  &price,
		Buy: momentum.SideStats{
			TotalNotionalUSD: 500,
			TradeCount:       3,
			Histogram:        make([]momentum.HistogramBucket, 11),
			// TopNotionalsUSD deliberately left nil: this is the exact
			// shape a quiet-on-one-side minute produces.
		},
		Sell: momentum.SideStats{
			Histogram: make([]momentum.HistogramBucket, 11),
		},
		TradeCount:     3,
		TickerComplete: true,
		TradesComplete: true,
		Complete:       true,
	}
}

func newTestWriter(db writerDB) *Writer {
	return &Writer{db: db, exchange: "bybit", marketType: "linear", universeVersion: "universe-hash"}
}

func TestWriterEnqueueDropsOldestWhenOverCapacity(t *testing.T) {
	t.Parallel()
	w := newTestWriter(&stubWriterDB{})
	// Fill to capacity with a distinguishable first bar, then push one more.
	first := testBar("FIRSTUSDT", 0)
	bars := make([]momentum.Bar, 0, MaxPendingBars+1)
	bars = append(bars, first)
	for i := 1; i < MaxPendingBars; i++ {
		bars = append(bars, testBar("BTCUSDT", i%59))
	}
	if dropped := w.Enqueue(bars); dropped != 0 {
		t.Fatalf("dropped = %d, want 0 while under capacity", dropped)
	}
	dropped := w.Enqueue([]momentum.Bar{testBar("LASTUSDT", 1)})
	if dropped != 1 {
		t.Fatalf("dropped = %d, want 1 once over capacity", dropped)
	}
	if len(w.pending) != MaxPendingBars {
		t.Fatalf("pending = %d, want %d", len(w.pending), MaxPendingBars)
	}
	if w.pending[0].Symbol == "FIRSTUSDT" {
		t.Fatal("the oldest bar should have been dropped, not kept")
	}
	if w.pending[len(w.pending)-1].Symbol != "LASTUSDT" {
		t.Fatal("the newest bar should survive the drop")
	}
	if got := w.Stats().QueueDropsTotal; got != 1 {
		t.Fatalf("QueueDropsTotal = %d, want 1 (visible in Health, not just a log line)", got)
	}
}

func TestWriterFlushQueuesOneStatementPerPendingBarAndClearsPendingOnSuccess(t *testing.T) {
	t.Parallel()
	db := &stubWriterDB{}
	w := newTestWriter(db)
	w.Enqueue([]momentum.Bar{testBar("BTCUSDT", 0), testBar("ETHUSDT", 0)})

	if err := w.Flush(context.Background()); err != nil {
		t.Fatalf("Flush: %v", err)
	}
	if db.lastBatch == nil || db.lastBatch.Len() != 2 {
		t.Fatalf("batch len = %v, want 2", db.lastBatch)
	}
	for _, qq := range db.lastBatch.QueuedQueries {
		if len(qq.Arguments) != 91 {
			t.Fatalf("args per row = %d, want 91 (matches insertRowSQL's column list)", len(qq.Arguments))
		}
	}
	if len(w.pending) != 0 {
		t.Fatalf("pending after successful flush = %d, want 0", len(w.pending))
	}
	stats := w.Stats()
	if stats.BarsPersistedTotal != 2 || stats.RowsWrittenTotal != 2 {
		t.Fatalf("stats = %+v, want 2 persisted/written", stats)
	}
}

func TestWriterFlushNormalizesNilTopNotionalToEmptySlice(t *testing.T) {
	t.Parallel()
	db := &stubWriterDB{}
	w := newTestWriter(db)
	w.Enqueue([]momentum.Bar{testBar("BTCUSDT", 0)}) // Buy.TopNotionalsUSD is nil in testBar

	if err := w.Flush(context.Background()); err != nil {
		t.Fatalf("Flush: %v", err)
	}
	args := db.lastBatch.QueuedQueries[0].Arguments
	// buy_top_notional is the 17th column (index 16).
	buyTop, ok := args[16].([]float64)
	if !ok {
		t.Fatalf("buy_top_notional arg has type %T, want []float64", args[16])
	}
	if buyTop == nil || len(buyTop) != 0 {
		t.Fatalf("buy_top_notional = %#v, want a non-nil empty slice", buyTop)
	}
}

func TestWriterFlushDetectsPayloadHashMismatchOnConflict(t *testing.T) {
	t.Parallel()
	db := &stubWriterDB{
		rows: []stubRowResult{
			{inserted: false, storedHash: make([]byte, 32)}, // all-zero: guaranteed to differ from a real SHA-256
		},
	}
	w := newTestWriter(db)
	w.Enqueue([]momentum.Bar{testBar("BTCUSDT", 0)})

	if err := w.Flush(context.Background()); err != nil {
		t.Fatalf("Flush: %v", err)
	}
	if got := w.Stats().PayloadHashMismatchTotal; got != 1 {
		t.Fatalf("PayloadHashMismatchTotal = %d, want 1", got)
	}
}

func TestWriterFlushRetryOnConflictWithMatchingHashIsNotAMismatch(t *testing.T) {
	t.Parallel()
	bar := testBar("BTCUSDT", 0)
	w := newTestWriter(&stubWriterDB{})
	_, hash := w.rowArgs(bar)

	db := &stubWriterDB{rows: []stubRowResult{{inserted: false, storedHash: hash[:]}}}
	w = newTestWriter(db)
	w.Enqueue([]momentum.Bar{bar})

	if err := w.Flush(context.Background()); err != nil {
		t.Fatalf("Flush: %v", err)
	}
	if got := w.Stats().PayloadHashMismatchTotal; got != 0 {
		t.Fatalf("PayloadHashMismatchTotal = %d, want 0 for a harmless retry with matching hash", got)
	}
}

func TestWriterFlushLeavesPendingAndAppliesBackoffOnError(t *testing.T) {
	t.Parallel()
	db := &stubWriterDB{rows: []stubRowResult{{err: errors.New("connection reset")}}}
	w := newTestWriter(db)
	w.Enqueue([]momentum.Bar{testBar("BTCUSDT", 0)})

	err := w.Flush(context.Background())
	if err == nil {
		t.Fatal("expected an error from a failing batch row")
	}
	if len(w.pending) != 1 {
		t.Fatalf("pending after failed flush = %d, want 1 (retried together, not dropped)", len(w.pending))
	}
	if stats := w.Stats(); stats.PersistErrorsTotal != 1 || stats.PersistRetriesTotal != 1 {
		t.Fatalf("stats = %+v, want 1 error and 1 retry", stats)
	}
	if w.Ready(time.Now()) {
		t.Fatal("writer should back off immediately after a failure, not be ready right away")
	}
}

func TestWriterFlushOfEmptyPendingIsANoOp(t *testing.T) {
	t.Parallel()
	db := &stubWriterDB{}
	w := newTestWriter(db)

	if err := w.Flush(context.Background()); err != nil {
		t.Fatalf("Flush of nothing pending: %v", err)
	}
	if db.lastBatch != nil {
		t.Fatal("no batch should be sent when there is nothing to flush")
	}
}

func TestWriterFlushSplitsALargeBacklogIntoBoundedSubBatches(t *testing.T) {
	t.Parallel()
	db := &stubWriterDB{}
	w := newTestWriter(db)
	total := writerSubBatchSize*2 + 10
	bars := make([]momentum.Bar, 0, total)
	for i := 0; i < total; i++ {
		bars = append(bars, testBar("BTCUSDT", i%59))
	}
	w.Enqueue(bars)

	if err := w.Flush(context.Background()); err != nil {
		t.Fatalf("Flush: %v", err)
	}
	if len(db.batches) != 3 {
		t.Fatalf("sub-batches sent = %d, want 3 (500, 500, 10)", len(db.batches))
	}
	if db.batches[0].Len() != writerSubBatchSize || db.batches[1].Len() != writerSubBatchSize || db.batches[2].Len() != 10 {
		t.Fatalf("sub-batch sizes = %d, %d, %d", db.batches[0].Len(), db.batches[1].Len(), db.batches[2].Len())
	}
	if len(w.pending) != 0 {
		t.Fatalf("pending after a fully successful flush = %d, want 0", len(w.pending))
	}
}

func TestWriterFlushOfALargeBacklogKeepsSuccessfulPrefixOnMidwayFailure(t *testing.T) {
	t.Parallel()
	db := &stubWriterDB{failCloseOnBatch: 2}
	w := newTestWriter(db)
	total := writerSubBatchSize*2 + 10
	bars := make([]momentum.Bar, 0, total)
	for i := 0; i < total; i++ {
		bars = append(bars, testBar("BTCUSDT", i%59))
	}
	w.Enqueue(bars)

	if err := w.Flush(context.Background()); err == nil {
		t.Fatal("expected an error when the second sub-batch's Close fails")
	}
	if len(db.batches) != 2 {
		t.Fatalf("sub-batches sent = %d, want 2 (stopped at the failing one, never sent the third)", len(db.batches))
	}
	// The first sub-batch (500 rows) succeeded and was trimmed; the second
	// (500 rows, failed) plus the untouched third (10 rows) remain.
	if want := writerSubBatchSize + 10; len(w.pending) != want {
		t.Fatalf("pending after a midway failure = %d, want %d (first sub-batch's success preserved)", len(w.pending), want)
	}
}

func TestWriterFlushTreatsACloseFailureAsAFlushFailure(t *testing.T) {
	t.Parallel()
	db := &stubWriterDB{failCloseOnBatch: 1, closeErr: errors.New("connection reset during commit")}
	w := newTestWriter(db)
	w.Enqueue([]momentum.Bar{testBar("BTCUSDT", 0)})

	err := w.Flush(context.Background())
	if err == nil {
		t.Fatal("expected an error when Close fails even though every row scanned cleanly")
	}
	if len(w.pending) != 1 {
		t.Fatal("a Close failure must not clear pending: the batch's durability is unconfirmed")
	}
	if stats := w.Stats(); stats.PersistErrorsTotal != 1 || stats.BarsPersistedTotal != 0 {
		t.Fatalf("stats = %+v, want 1 persist error and 0 bars persisted", stats)
	}
}

func TestHashRowIsDeterministicAndContentSensitive(t *testing.T) {
	t.Parallel()
	w := newTestWriter(&stubWriterDB{})
	barA := testBar("BTCUSDT", 0)
	barB := testBar("BTCUSDT", 0)
	_, hashA1 := w.rowArgs(barA)
	_, hashA2 := w.rowArgs(barA)
	_, hashB := w.rowArgs(barB)
	if hashA1 != hashA2 {
		t.Fatal("hashing the same bar twice must be deterministic")
	}
	if hashA1 != hashB {
		t.Fatal("hashing two bars with identical content must produce the same hash")
	}

	barC := testBar("BTCUSDT", 0)
	*barC.ClosePrice = 999
	_, hashC := w.rowArgs(barC)
	if hashC == hashA1 {
		t.Fatal("changing bar content must change the hash")
	}
	barD := testBar("BTCUSDT", 0)
	funding := 0.001
	eventAt, observedAt := time.Unix(1, 0).UTC(), time.Unix(2, 0).UTC()
	barD.FundingRate = &funding
	barD.FundingRateEventAt = &eventAt
	barD.FundingRateObservedAt = &observedAt
	_, hashD := w.rowArgs(barD)
	if hashD == hashA1 {
		t.Fatal("changing additive derivatives context must change the hash")
	}
}

func TestWriterStatsIsSafeForConcurrentReadWhileFlushing(t *testing.T) {
	db := &stubWriterDB{}
	w := newTestWriter(db)
	done := make(chan struct{})

	// One goroutine repeatedly enqueues and flushes (the real
	// cmd/momentumcapture writer-goroutine's job); another repeatedly
	// reads Stats (the real health-reporting goroutine's job). Run under
	// `go test -race` this fails loudly if pending/peak/stats are ever
	// touched without mu.
	go func() {
		defer close(done)
		for i := 0; i < 200; i++ {
			w.Enqueue([]momentum.Bar{testBar("BTCUSDT", i%59)})
			if err := w.Flush(context.Background()); err != nil {
				t.Errorf("Flush: %v", err)
				return
			}
		}
	}()
	for i := 0; i < 200; i++ {
		_ = w.Stats()
	}
	<-done
}

func TestApplyWriterStatsCopiesOnlyWriterFields(t *testing.T) {
	t.Parallel()
	health := Health{Status: "ok", ReadySymbols: 5}
	stats := WriterStats{
		QueueDepth:               3,
		QueuePeak:                7,
		BarsPersistedTotal:       100,
		PersistErrorsTotal:       2,
		PersistRetriesTotal:      2,
		RowsWrittenTotal:         100,
		PayloadHashMismatchTotal: 1,
	}
	updated := ApplyWriterStats(health, stats)
	if updated.Status != "ok" || updated.ReadySymbols != 5 {
		t.Fatal("ApplyWriterStats must not touch unrelated fields")
	}
	if updated.WriterQueueDepth != 3 || updated.WriterQueuePeak != 7 ||
		updated.BarsPersistedTotal != 100 || updated.PersistErrorsTotal != 2 ||
		updated.PersistRetriesTotal != 2 || updated.RowsWrittenTotal != 100 ||
		updated.PayloadHashMismatchTotal != 1 {
		t.Fatalf("writer fields not copied correctly: %+v", updated)
	}
}

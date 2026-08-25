package liquidationcapture

import (
	"bytes"
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	MaxPendingEvents = 10000
	writerBatchSize  = 500
	writeTimeout     = 10 * time.Second
)

type writerDB interface {
	SendBatch(context.Context, *pgx.Batch) pgx.BatchResults
	Exec(context.Context, string, ...any) (pgconn.CommandTag, error)
	Close()
}

type WriterStats struct {
	QueueDepth               int
	QueuePeak                int
	QueueDropsTotal          uint64
	EventsPersistedTotal     uint64
	DuplicateEventsTotal     uint64
	PayloadHashMismatchTotal uint64
	PersistErrorsTotal       uint64
	LastPersistAt            time.Time
}

// Writer retains append-only events until a successful database flush. When
// full it rejects the new event (and the caller marks durable coverage
// incomplete); it never evicts an older event and pretend the ledger is whole.
type Writer struct {
	db      writerDB
	mu      sync.Mutex
	pending []Event
	stats   WriterStats
}

func NewWriter(ctx context.Context, databaseURL string) (*Writer, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("connect liquidation writer: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping liquidation writer: %w", err)
	}
	return &Writer{db: pool}, nil
}

func (w *Writer) Enqueue(event Event) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	if len(w.pending) >= MaxPendingEvents {
		w.stats.QueueDropsTotal++
		return false
	}
	w.pending = append(w.pending, event)
	if len(w.pending) > w.stats.QueuePeak {
		w.stats.QueuePeak = len(w.pending)
	}
	return true
}

func (w *Writer) Flush(ctx context.Context) error {
	// Bound this call to the queue snapshot visible at entry. Producers may
	// keep enqueueing while PostgreSQL is working; chasing those new arrivals
	// forever would starve health and durable coverage under a liquidation
	// burst. The next periodic Flush owns the newer suffix.
	w.mu.Lock()
	remaining := len(w.pending)
	w.mu.Unlock()
	for remaining > 0 {
		w.mu.Lock()
		n := min(remaining, min(len(w.pending), writerBatchSize))
		if n == 0 {
			w.mu.Unlock()
			return nil
		}
		batchEvents := append([]Event(nil), w.pending[:n]...)
		w.mu.Unlock()

		flushCtx, cancel := context.WithTimeout(ctx, writeTimeout)
		batch := &pgx.Batch{}
		for _, event := range batchEvents {
			batch.Queue(insertEventSQL, eventArgs(event)...)
		}
		results := w.db.SendBatch(flushCtx, batch)
		insertedCount := 0
		duplicateCount := 0
		mismatchCount := 0
		for _, event := range batchEvents {
			var inserted bool
			var storedHash []byte
			if err := results.QueryRow().Scan(&inserted, &storedHash); err != nil {
				_ = results.Close()
				cancel()
				w.mu.Lock()
				w.stats.PersistErrorsTotal++
				w.mu.Unlock()
				return fmt.Errorf("persist liquidation event %s@%s: %w", event.NativeMarketID, event.EventAt, err)
			}
			if inserted {
				insertedCount++
			} else {
				duplicateCount++
				if len(storedHash) == sha256Size && !bytes.Equal(storedHash, event.PayloadHash[:]) {
					mismatchCount++
				}
			}
		}
		if err := results.Close(); err != nil {
			cancel()
			w.mu.Lock()
			w.stats.PersistErrorsTotal++
			w.mu.Unlock()
			return fmt.Errorf("close liquidation batch: %w", err)
		}
		cancel()

		w.mu.Lock()
		w.pending = w.pending[n:]
		w.stats.EventsPersistedTotal += uint64(insertedCount)
		w.stats.DuplicateEventsTotal += uint64(duplicateCount)
		w.stats.PayloadHashMismatchTotal += uint64(mismatchCount)
		w.stats.LastPersistAt = time.Now()
		w.mu.Unlock()
		remaining -= n
	}
	return nil
}

const sha256Size = 32

func (w *Writer) WriteHeartbeat(ctx context.Context, heartbeat Heartbeat) error {
	_, err := w.db.Exec(ctx, insertHeartbeatSQL,
		heartbeat.Exchange, CaptureVersion, heartbeat.MarketType, string(heartbeat.CoverageKind),
		heartbeat.ProcessSessionID, heartbeat.UniverseVersion, heartbeat.BucketStart, heartbeat.ExpectedConnections,
		heartbeat.ConnectedConnections, heartbeat.DataLossDetected, heartbeat.Complete,
		heartbeat.EventsReceivedTotal, heartbeat.EventsPersistedTotal,
		heartbeat.DuplicateEventsTotal, heartbeat.QueueDropsTotal,
		heartbeat.InvalidEventsTotal, heartbeat.OutOfScopeTotal,
		heartbeat.ScopeTagMissingAcceptedTotal, heartbeat.ReconnectTotal, heartbeat.ReadTimeoutTotal,
	)
	if err != nil {
		w.mu.Lock()
		w.stats.PersistErrorsTotal++
		w.mu.Unlock()
		return fmt.Errorf("persist liquidation heartbeat: %w", err)
	}
	return nil
}

func (w *Writer) Stats() WriterStats {
	w.mu.Lock()
	defer w.mu.Unlock()
	stats := w.stats
	stats.QueueDepth = len(w.pending)
	return stats
}

func (w *Writer) Close() { w.db.Close() }

func eventArgs(event Event) []any {
	return []any{
		CaptureVersion, event.Exchange, event.MarketType, event.NativeMarketID,
		event.UniverseVersion, event.SourceContractVariant,
		string(event.CoverageKind), string(event.PositionSide), event.EventAt,
		event.ExchangePublishedAt, event.ReceivedAt, event.SourceSessionID,
		event.SourceEventKey[:], event.PayloadHash[:], event.Quantity, event.QuantityUnit,
		event.BankruptcyPrice, event.OrderPrice, event.AveragePrice,
		event.LastFilledQuantity, event.AccumulatedFilledQuantity,
		event.EstimatedLiquidationNotional, event.RawPayload,
	}
}

const insertEventSQL = `
WITH inserted AS (
    INSERT INTO timeseries.liquidation_events (
        capture_version, exchange, market_type, native_market_id, universe_version,
		source_contract_variant, coverage_kind, position_side, event_at, exchange_published_at,
        received_at, source_session_id, source_event_key, payload_hash,
        quantity, quantity_unit, bankruptcy_price, order_price, average_price,
        last_filled_quantity, accumulated_filled_quantity,
        estimated_liquidation_notional, raw_payload
    ) VALUES (
		$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23
    )
    ON CONFLICT (exchange, event_at, source_event_key) DO NOTHING
    RETURNING TRUE AS inserted, payload_hash
)
SELECT inserted, payload_hash FROM inserted
UNION ALL
SELECT FALSE, payload_hash
FROM timeseries.liquidation_events
WHERE exchange = $2 AND event_at = $9 AND source_event_key = $13
LIMIT 1
`

const insertHeartbeatSQL = `
INSERT INTO timeseries.liquidation_capture_heartbeats_1m (
    exchange, capture_version, market_type, coverage_kind, process_session_id, universe_version,
    bucket_start, expected_connections, connected_connections,
    data_loss_detected, complete, events_received_total, events_persisted_total,
    duplicate_events_total, queue_drops_total, invalid_events_total,
	out_of_scope_total, scope_tag_missing_accepted_total, reconnect_total, read_timeout_total
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
ON CONFLICT (exchange, capture_version, process_session_id, bucket_start) DO NOTHING
`

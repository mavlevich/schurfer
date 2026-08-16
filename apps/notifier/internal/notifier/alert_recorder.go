package notifier

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

const recordAlertTimeout = 2 * time.Second

type alertDelivery struct {
	EventID               int64
	Base                  string
	Exchange              string
	ThresholdPct          float64
	ObservedChangePct     float64
	Exchange24hHighPct    *float64
	TickerAt              *time.Time
	ScannerObservedAt     time.Time
	ScanPublishedAt       time.Time
	NotificationStartedAt time.Time
	NotificationSentAt    time.Time
}

type alertRecorder interface {
	Record(context.Context, alertDelivery) error
	Close()
}

type alertDB interface {
	Exec(context.Context, string, ...any) (pgconn.CommandTag, error)
	QueryRow(context.Context, string, ...any) pgx.Row
	// Query is needed by momentum_flow_alerts.go's own multi-row reads
	// (a bounded window of newly-opened paper probes or newly-resolved
	// outcomes each tick, not a single scalar like ReadSourceLeadHealth's
	// own QueryRow-only reads) -- everything else in this package still
	// only ever needs QueryRow/Exec.
	Query(context.Context, string, ...any) (pgx.Rows, error)
	Close()
}

type postgresAlertRecorder struct {
	pool alertDB
}

func newPostgresAlertRecorder(ctx context.Context, databaseURL string) (*postgresAlertRecorder, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, err
	}
	return &postgresAlertRecorder{pool: pool}, nil
}

func (r *postgresAlertRecorder) Record(ctx context.Context, delivery alertDelivery) error {
	recordCtx, cancel := context.WithTimeout(ctx, recordAlertTimeout)
	defer cancel()

	_, err := r.pool.Exec(recordCtx, `
		INSERT INTO app.pump_alert_deliveries (
			event_id, base, exchange, channel, alert_kind, payload_version,
			threshold_pct, observed_change_pct, exchange_24h_high_pct,
			ticker_at, scanner_observed_at, scan_published_at,
			notification_started_at, notification_sent_at
		)
		VALUES (
			$1, $2, NULLIF($3, ''), 'telegram', 'threshold_crossed', 1,
			$4, $5, $6, $7, $8, $9, $10, $11
		)
		ON CONFLICT (
			event_id, channel, alert_kind, threshold_pct
		) DO NOTHING`,
		delivery.EventID,
		delivery.Base,
		delivery.Exchange,
		delivery.ThresholdPct,
		delivery.ObservedChangePct,
		delivery.Exchange24hHighPct,
		delivery.TickerAt,
		delivery.ScannerObservedAt,
		delivery.ScanPublishedAt,
		delivery.NotificationStartedAt,
		delivery.NotificationSentAt,
	)
	return err
}

func (r *postgresAlertRecorder) Close() {
	r.pool.Close()
}

package health

import (
	"context"
	"encoding/json"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

type pgxRow interface {
	Scan(dest ...any) error
}

type queryRower interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgxRow
}

type poolAdapter struct{ inner *pgxpool.Pool }

func (a *poolAdapter) QueryRow(ctx context.Context, sql string, args ...any) pgxRow {
	return a.inner.QueryRow(ctx, sql, args...)
}

// FillIncidents reports durable fill-resolution incidents that are still open
// (execution could not confirm a real fill price for an order) and whether
// the execution service's PnL-readiness lease is currently valid. PnlReady
// mirrors risk:pnl_ready — it is revoked by the execution service for as long
// as any incident stays open, so a human should treat displayed PnL as
// provisional whenever this is false.
type FillIncidents struct {
	PnlReady bool                  `json:"pnl_ready"`
	Open     []FillIncidentSummary `json:"open"`
}

type FillIncidentSummary struct {
	ID           int64     `json:"id"`
	Exchange     string    `json:"exchange"`
	Base         string    `json:"base"`
	Operation    string    `json:"operation"`
	OrderID      string    `json:"order_id"`
	Status       string    `json:"status"`
	AttemptCount int       `json:"attempt_count"`
	LastError    *string   `json:"last_error"`
	CreatedAt    time.Time `json:"created_at"`
}

// Bounded so a severe multi-incident outage can't make this health payload
// (pushed to every /ws/status subscriber every few seconds) grow unbounded.
const fillIncidentsLimit = 50

const fillIncidentsSQL = `
SELECT coalesce(
	jsonb_agg(
		jsonb_build_object(
			'id', id,
			'exchange', exchange,
			'base', base,
			'operation', operation,
			'order_id', order_id,
			'status', status,
			'attempt_count', attempt_count,
			'last_error', last_error,
			'created_at', created_at
		) ORDER BY created_at ASC
	), '[]'::jsonb
)::text
FROM (
	SELECT *
	FROM app.fill_resolution_incidents
	WHERE status IN ('pending', 'resolving', 'manual_required')
	ORDER BY created_at ASC
	LIMIT $1
) AS bounded`

// checkFillIncidents returns nil (omitting the field) if either the Postgres
// or Redis read fails, rather than reporting an empty/ready state that could
// be mistaken for "no open incidents" or "PnL confirmed".
func (c *Checker) checkFillIncidents(ctx context.Context) *FillIncidents {
	ctx, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()

	pnlReady, err := c.rdb.Exists(ctx, "risk:pnl_ready").Result()
	if err != nil {
		return nil
	}

	var incidentsJSON string
	if err := c.db.QueryRow(ctx, fillIncidentsSQL, fillIncidentsLimit).Scan(&incidentsJSON); err != nil {
		return nil
	}
	open := []FillIncidentSummary{}
	if err := json.Unmarshal([]byte(incidentsJSON), &open); err != nil {
		return nil
	}
	return &FillIncidents{PnlReady: pnlReady == 1, Open: open}
}

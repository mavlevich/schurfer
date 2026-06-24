package trades

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	maxLimit     = 200
	defaultLimit = 50
)

// pgxRow is satisfied by pgx.Row — extracted for test injection.
type pgxRow interface {
	Scan(dest ...any) error
}

// pgxPool is the subset of *pgxpool.Pool used by this handler.
type pgxPool interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgxRow
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

type poolAdapter struct{ inner *pgxpool.Pool }

func (a *poolAdapter) QueryRow(ctx context.Context, sql string, args ...any) pgxRow {
	return a.inner.QueryRow(ctx, sql, args...)
}

func (a *poolAdapter) Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error) {
	return a.inner.Query(ctx, sql, args...)
}

type Handler struct {
	pool pgxPool
}

func NewHandler(pool *pgxpool.Pool) *Handler {
	return &Handler{pool: &poolAdapter{inner: pool}}
}

type tradeRow struct {
	ID           int64           `json:"id"`
	Symbol       string          `json:"symbol"`
	Exchange     string          `json:"exchange"`
	MarketType   string          `json:"market_type"`
	Side         string          `json:"side"`
	SizeUSD      float64         `json:"size_usd"`
	Leverage     float64         `json:"leverage"`
	EntryPrice   float64         `json:"entry_price"`
	EntryAt      time.Time       `json:"entry_at"`
	ExitPrice    *float64        `json:"exit_price"`
	ExitAt       *time.Time      `json:"exit_at"`
	PnlUSD       *float64        `json:"pnl_usd"`
	PnlPct       *float64        `json:"pnl_pct"`
	Status       string          `json:"status"`
	OutcomeLabel *string         `json:"outcome_label"`
	SetupContext json.RawMessage `json:"setup_context"`
	Notes        *string         `json:"notes"`
	CreatedAt    time.Time       `json:"created_at"`
}

type listResponse struct {
	Total  int        `json:"total"`
	Limit  int        `json:"limit"`
	Offset int        `json:"offset"`
	Trades []tradeRow `json:"trades"`
}

// List handles GET /api/trades
// Query params: status (open|closed), exchange, limit, offset
func (h *Handler) List(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()

	status := q.Get("status")
	exchange := q.Get("exchange")

	limit := defaultLimit
	if v := q.Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	if limit > maxLimit {
		limit = maxLimit
	}

	offset := 0
	if v := q.Get("offset"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			offset = n
		}
	}

	args := []any{}
	where := "WHERE 1=1"
	if status != "" {
		args = append(args, status)
		where += " AND t.status = $" + strconv.Itoa(len(args))
	}
	if exchange != "" {
		args = append(args, exchange)
		where += " AND t.exchange = $" + strconv.Itoa(len(args))
	}

	var total int
	if err := h.pool.QueryRow(r.Context(),
		"SELECT COUNT(*) FROM app.trades t "+where, args...,
	).Scan(&total); err != nil {
		slog.Error("trades.count", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	dataArgs := append(args, limit, offset)
	n := len(dataArgs)
	rows, err := h.pool.Query(r.Context(), `
		SELECT t.id, t.symbol, t.exchange, t.market_type, t.side,
		       t.size_usd::float8, t.leverage::float8,
		       t.entry_price::float8, t.entry_at,
		       t.exit_price::float8, t.exit_at,
		       t.pnl_usd::float8, t.pnl_pct::float8,
		       t.status, t.outcome_label,
		       t.setup_context, t.notes, t.created_at
		FROM app.trades t `+where+`
		ORDER BY t.entry_at DESC
		LIMIT $`+strconv.Itoa(n-1)+` OFFSET $`+strconv.Itoa(n),
		dataArgs...,
	)
	if err != nil {
		slog.Error("trades.query", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	result := make([]tradeRow, 0)
	for rows.Next() {
		var t tradeRow
		if err := rows.Scan(
			&t.ID, &t.Symbol, &t.Exchange, &t.MarketType, &t.Side,
			&t.SizeUSD, &t.Leverage,
			&t.EntryPrice, &t.EntryAt,
			&t.ExitPrice, &t.ExitAt,
			&t.PnlUSD, &t.PnlPct,
			&t.Status, &t.OutcomeLabel,
			&t.SetupContext, &t.Notes, &t.CreatedAt,
		); err != nil {
			slog.Error("trades.scan", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		result = append(result, t)
	}
	if err := rows.Err(); err != nil {
		slog.Error("trades.rows_err", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(listResponse{
		Total:  total,
		Limit:  limit,
		Offset: offset,
		Trades: result,
	}); err != nil {
		slog.Error("trades.encode", "err", err)
	}
}

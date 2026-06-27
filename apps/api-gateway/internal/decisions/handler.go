package decisions

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

type pgxRow interface {
	Scan(dest ...any) error
}

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

type decisionRow struct {
	ID        int64     `json:"id"`
	Ts        time.Time `json:"ts"`
	Base      string    `json:"base"`
	Exchange  string    `json:"exchange"`
	Action    string    `json:"action"`
	Reason    string    `json:"reason"`
	Score     *int      `json:"score"`
	PumpPct   *float64  `json:"pump_pct"`
	CreatedAt time.Time `json:"created_at"`
}

type listResponse struct {
	Total     int           `json:"total"`
	Limit     int           `json:"limit"`
	Offset    int           `json:"offset"`
	Decisions []decisionRow `json:"decisions"`
}

// List handles GET /api/decisions
// Query params: base, action (opened|skipped), limit, offset
func (h *Handler) List(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()

	base := q.Get("base")
	action := q.Get("action")

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
	if base != "" {
		args = append(args, base)
		where += " AND d.base = $" + strconv.Itoa(len(args))
	}
	if action != "" {
		args = append(args, action)
		where += " AND d.action = $" + strconv.Itoa(len(args))
	}

	var total int
	if err := h.pool.QueryRow(r.Context(),
		"SELECT COUNT(*) FROM app.trade_decisions d "+where, args...,
	).Scan(&total); err != nil {
		slog.Error("decisions.count", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	dataArgs := append(args, limit, offset)
	n := len(dataArgs)
	rows, err := h.pool.Query(r.Context(), `
		SELECT d.id, d.ts, d.base, d.exchange, d.action, d.reason,
		       d.score, d.pump_pct::float8, d.created_at
		FROM app.trade_decisions d `+where+`
		ORDER BY d.ts DESC
		LIMIT $`+strconv.Itoa(n-1)+` OFFSET $`+strconv.Itoa(n),
		dataArgs...,
	)
	if err != nil {
		slog.Error("decisions.query", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	result := make([]decisionRow, 0)
	for rows.Next() {
		var d decisionRow
		if err := rows.Scan(
			&d.ID, &d.Ts, &d.Base, &d.Exchange, &d.Action, &d.Reason,
			&d.Score, &d.PumpPct, &d.CreatedAt,
		); err != nil {
			slog.Error("decisions.scan", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		result = append(result, d)
	}
	if err := rows.Err(); err != nil {
		slog.Error("decisions.rows_err", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(listResponse{
		Total:     total,
		Limit:     limit,
		Offset:    offset,
		Decisions: result,
	}); err != nil {
		slog.Error("decisions.encode", "err", err)
	}
}

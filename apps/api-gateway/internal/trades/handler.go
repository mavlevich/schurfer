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
	ID                int64           `json:"id"`
	Symbol            string          `json:"symbol"`
	Exchange          string          `json:"exchange"`
	MarketType        string          `json:"market_type"`
	Side              string          `json:"side"`
	SizeUSD           float64         `json:"size_usd"`
	Leverage          float64         `json:"leverage"`
	EntryPrice        float64         `json:"entry_price"`
	EntryAt           time.Time       `json:"entry_at"`
	ExitPrice         *float64        `json:"exit_price"`
	ExitAt            *time.Time      `json:"exit_at"`
	EntrySlippageBPS  *float64        `json:"entry_slippage_bps"`
	ExitSlippageBPS   *float64        `json:"exit_slippage_bps"`
	FeesUSD           float64         `json:"fees_usd"`
	FundingUSD        float64         `json:"funding_usd"`
	SlippageUSD       *float64        `json:"slippage_usd"`
	GrossPnlUSD       *float64        `json:"gross_pnl_usd"`
	GrossPnlPct       *float64        `json:"gross_pnl_pct"`
	NetPnlUSD         *float64        `json:"net_pnl_usd"`
	NetPnlPct         *float64        `json:"net_pnl_pct"`
	PnlUSD            *float64        `json:"pnl_usd"`
	PnlPct            *float64        `json:"pnl_pct"`
	AccountingVersion string          `json:"accounting_version"`
	AccountingStatus  string          `json:"accounting_status"`
	AccountingError   *string         `json:"accounting_error"`
	Status            string          `json:"status"`
	OutcomeLabel      *string         `json:"outcome_label"`
	SetupContext      json.RawMessage `json:"setup_context"`
	Notes             *string         `json:"notes"`
	CreatedAt         time.Time       `json:"created_at"`
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
		       t.entry_slippage_bps::float8, t.exit_slippage_bps::float8,
		       t.fees_usd::float8, t.funding_usd::float8, t.slippage_usd::float8,
		       t.gross_pnl_usd::float8, t.gross_pnl_pct::float8,
		       t.net_pnl_usd::float8, t.net_pnl_pct::float8,
		       t.pnl_usd::float8, t.pnl_pct::float8,
		       t.accounting_version, t.accounting_status, t.accounting_error,
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
			&t.EntrySlippageBPS, &t.ExitSlippageBPS,
			&t.FeesUSD, &t.FundingUSD, &t.SlippageUSD,
			&t.GrossPnlUSD, &t.GrossPnlPct,
			&t.NetPnlUSD, &t.NetPnlPct,
			&t.PnlUSD, &t.PnlPct,
			&t.AccountingVersion, &t.AccountingStatus, &t.AccountingError,
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

// tradeAgg holds the raw SQL aggregates over closed trades; the derived ratios are
// computed in Go (computeStats) so they can be unit-tested without a database.
type tradeAgg struct {
	N          int
	Wins       int
	Losses     int
	SumPct     float64
	SumWinPct  float64
	SumLossPct float64
	TotalUSD   float64
	WinningUSD float64
	LosingUSD  float64 // negative
}

type statsResponse struct {
	Count           int      `json:"count"`
	WinRate         float64  `json:"win_rate"`
	Expectancy      float64  `json:"expectancy"`
	AvgWin          float64  `json:"avg_win"`
	AvgLoss         float64  `json:"avg_loss"`
	ProfitFactor    *float64 `json:"profit_factor"`
	GrossUSD        float64  `json:"gross_usd"`
	NetCount        int      `json:"net_count"`
	NetWinRate      *float64 `json:"net_win_rate"`
	NetExpectancy   *float64 `json:"net_expectancy"`
	NetAvgWin       *float64 `json:"net_avg_win"`
	NetAvgLoss      *float64 `json:"net_avg_loss"`
	NetProfitFactor *float64 `json:"net_profit_factor"`
	NetUSD          *float64 `json:"net_usd"`
	LegacyCount     int      `json:"legacy_count"`
	IncompleteCount int      `json:"incomplete_count"`
}

type statsAgg struct {
	Gross           tradeAgg
	Net             tradeAgg
	LegacyCount     int
	IncompleteCount int
}

func profitFactor(a tradeAgg) *float64 {
	if a.LosingUSD >= 0 {
		return nil
	}
	value := a.WinningUSD / -a.LosingUSD
	return &value
}

func computeStats(a statsAgg) statsResponse {
	s := statsResponse{
		Count:           a.Gross.N,
		GrossUSD:        a.Gross.TotalUSD,
		NetCount:        a.Net.N,
		LegacyCount:     a.LegacyCount,
		IncompleteCount: a.IncompleteCount,
		ProfitFactor:    profitFactor(a.Gross),
	}
	if a.Gross.N > 0 {
		s.WinRate = float64(a.Gross.Wins) / float64(a.Gross.N) * 100
		s.Expectancy = a.Gross.SumPct / float64(a.Gross.N)
	}
	if a.Gross.Wins > 0 {
		s.AvgWin = a.Gross.SumWinPct / float64(a.Gross.Wins)
	}
	if a.Gross.Losses > 0 {
		s.AvgLoss = a.Gross.SumLossPct / float64(a.Gross.Losses)
	}
	if a.Net.N > 0 {
		winRate := float64(a.Net.Wins) / float64(a.Net.N) * 100
		expectancy := a.Net.SumPct / float64(a.Net.N)
		netUSD := a.Net.TotalUSD
		s.NetWinRate = &winRate
		s.NetExpectancy = &expectancy
		s.NetUSD = &netUSD
		s.NetProfitFactor = profitFactor(a.Net)
		if a.Net.Wins > 0 {
			avgWin := a.Net.SumWinPct / float64(a.Net.Wins)
			s.NetAvgWin = &avgWin
		}
		if a.Net.Losses > 0 {
			avgLoss := a.Net.SumLossPct / float64(a.Net.Losses)
			s.NetAvgLoss = &avgLoss
		}
	}
	return s
}

// Stats handles GET /api/trades/stats: aggregate performance over the whole set of
// closed trades (optionally filtered by exchange), not just one page of the list.
func (h *Handler) Stats(w http.ResponseWriter, r *http.Request) {
	exchange := r.URL.Query().Get("exchange")

	args := []any{}
	where := "WHERE t.status = 'closed' AND t.gross_pnl_pct IS NOT NULL"
	if exchange != "" {
		args = append(args, exchange)
		where += " AND t.exchange = $" + strconv.Itoa(len(args))
	}

	var a statsAgg
	if err := h.pool.QueryRow(r.Context(), `
		SELECT count(*) FILTER (WHERE t.gross_pnl_pct IS NOT NULL),
		       count(*) FILTER (WHERE t.gross_pnl_pct > 0),
		       count(*) FILTER (WHERE t.gross_pnl_pct < 0),
		       COALESCE(sum(t.gross_pnl_pct), 0)::float8,
		       COALESCE(sum(t.gross_pnl_pct) FILTER (WHERE t.gross_pnl_pct > 0), 0)::float8,
		       COALESCE(sum(t.gross_pnl_pct) FILTER (WHERE t.gross_pnl_pct < 0), 0)::float8,
		       COALESCE(sum(t.gross_pnl_usd), 0)::float8,
		       COALESCE(sum(t.gross_pnl_usd) FILTER (WHERE t.gross_pnl_usd > 0), 0)::float8,
		       COALESCE(sum(t.gross_pnl_usd) FILTER (WHERE t.gross_pnl_usd < 0), 0)::float8,
		       count(*) FILTER (WHERE t.net_pnl_pct IS NOT NULL),
		       count(*) FILTER (WHERE t.net_pnl_pct > 0),
		       count(*) FILTER (WHERE t.net_pnl_pct < 0),
		       COALESCE(sum(t.net_pnl_pct), 0)::float8,
		       COALESCE(sum(t.net_pnl_pct) FILTER (WHERE t.net_pnl_pct > 0), 0)::float8,
		       COALESCE(sum(t.net_pnl_pct) FILTER (WHERE t.net_pnl_pct < 0), 0)::float8,
		       COALESCE(sum(t.net_pnl_usd), 0)::float8,
		       COALESCE(sum(t.net_pnl_usd) FILTER (WHERE t.net_pnl_usd > 0), 0)::float8,
		       COALESCE(sum(t.net_pnl_usd) FILTER (WHERE t.net_pnl_usd < 0), 0)::float8,
		       count(*) FILTER (WHERE t.accounting_status = 'legacy'),
		       count(*) FILTER (WHERE t.accounting_status = 'incomplete')
		FROM app.trades t `+where, args...,
	).Scan(
		&a.Gross.N, &a.Gross.Wins, &a.Gross.Losses,
		&a.Gross.SumPct, &a.Gross.SumWinPct, &a.Gross.SumLossPct,
		&a.Gross.TotalUSD, &a.Gross.WinningUSD, &a.Gross.LosingUSD,
		&a.Net.N, &a.Net.Wins, &a.Net.Losses,
		&a.Net.SumPct, &a.Net.SumWinPct, &a.Net.SumLossPct,
		&a.Net.TotalUSD, &a.Net.WinningUSD, &a.Net.LosingUSD,
		&a.LegacyCount, &a.IncompleteCount,
	); err != nil {
		slog.Error("trades.stats", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(computeStats(a)); err != nil {
		slog.Error("trades.stats.encode", "err", err)
	}
}

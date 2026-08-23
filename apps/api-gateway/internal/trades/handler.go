package trades

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	maxLimit     = 200
	defaultLimit = 50
)

// combinedTradesCTE unions app.trades (the pump-short strategy's own
// live/dry-run execution ledger) with app.momentum_flow_paper_probes
// (the momentum_flow WATCH->paper discovery instrumentation's own
// simulated long positions) into one column shape, tagged by origin.
// This is a display-only union: the two underlying tables stay
// physically separate (different writers, different promotion status --
// momentum_flow_paper is explicitly discovery instrumentation, not
// promotion evidence, see docs/research/momentum-flow-paper-v1.md), only
// this read-side query ever treats them as one list. Every consumer of
// this CTE (List and Stats below) must keep origin visible in its own
// response so momentum_flow_paper rows are never silently presented as
// if they were the already-promoted pump-short strategy's own trades.
//
// The momentum_flow side only includes entry_status = 'opened' probes: a
// probe whose entry never actually filled (stale, quote_rejected,
// still pending) is not a position, so it has no place in a trades list.
// Its own position_status ('open'/'closed') already matches app.trades'
// own status vocabulary exactly, so no remapping is needed there.
// entry_slippage_bps/exit_slippage_bps/slippage_usd/pnl_usd/pnl_pct have
// no real equivalent captured on the paper side (spread_bps/impact_bps
// are different metrics, not slippage) and are left NULL rather than
// mislabeled.
//
// fees_usd/funding_usd are coalesced to 0 on the paper side specifically
// (a production incident, 2026-08-16): app.trades' own fees_usd/
// funding_usd are NOT NULL, so tradeRow.FeesUSD/FundingUSD are plain
// float64, not pointers -- but momentum_flow_paper_probes' own columns
// ARE nullable (NULL until that probe's own cost accounting completes,
// which is independent of entry_status='opened'; a still-open probe has
// no accounted costs yet). Scanning that NULL into a non-pointer float64
// panics the whole List query -- verified this is the only such gap:
// entry_vwap/entry_at/entry_filled_notional_usd are always non-NULL for
// entry_status='opened' rows (checked directly against production data),
// only fees_usd/funding_usd are not. Coalescing here matches app.trades'
// own implicit "always a real number, 0 means no cost yet" convention
// rather than changing the shared JSON contract to nullable.
//
// exit_reason (colleague review) is genuinely nullable on both sides of
// the union -- t.notes is NULL for any still-open app.trades row (it is
// only ever written at close), and split_part(NULL, ' ', 1) is itself
// NULL, not ”. tradeRow.ExitReason is *string precisely because of this;
// scanning an open trade's NULL exit_reason into a non-pointer string
// crashed the whole /api/trades endpoint (reproduced directly against
// real Postgres, see TestCombinedTradesCTEAgainstRealPostgres). NULLIF
// additionally normalizes a genuinely-empty notes string to NULL rather
// than an empty-but-non-null exit_reason, so the API never has to
// distinguish "no reason" from "empty-string reason".
//
// strategy_name/strategy_version/strategy_key (colleague review) come
// from app.strategies via strategy_id -- the canonical identity every
// trade already carries -- not from setup_context->>'strategy', which
// only newer strategies populate. pump_short's own trader.py stamps
// setup_context["strategy_version"] instead (a pre-existing convention,
// see journal.strategy_identity's own docstring), so reading only
// setup_context->>'strategy' silently showed every pump_short trade as
// strategy_name="unknown". A LEFT JOIN (not INNER) so one hypothetically
// missing app.strategies row degrades a single trade's identity to
// "unknown" rather than dropping that trade from the page entirely.
const combinedTradesCTE = `
	WITH combined AS (
		SELECT
			COALESCE(t.setup_context->>'strategy', 'pump_short') || ':' || t.id::text AS id,
			'app.trades'::text AS origin,
			COALESCE(s.name || '_v' || s.version, 'unknown') AS strategy_key,
			COALESCE(s.name, 'unknown') AS strategy_name,
			COALESCE(s.version, 'unknown') AS strategy_version,
			CASE WHEN t.setup_context->>'paper' = 'true' THEN 'paper' ELSE 'live' END AS mode,
			NULLIF(split_part(t.notes, ' ', 1), '') AS exit_reason,
			t.symbol, t.exchange, t.market_type, t.side,
			t.size_usd::float8 AS size_usd, t.leverage::float8 AS leverage,
			t.entry_price::float8 AS entry_price, t.entry_at,
			t.exit_price::float8 AS exit_price, t.exit_at,
			t.entry_slippage_bps::float8 AS entry_slippage_bps,
			t.exit_slippage_bps::float8 AS exit_slippage_bps,
			t.fees_usd::float8 AS fees_usd, t.funding_usd::float8 AS funding_usd,
			t.slippage_usd::float8 AS slippage_usd,
			t.gross_pnl_usd::float8 AS gross_pnl_usd, t.gross_pnl_pct::float8 AS gross_pnl_pct,
			CASE WHEN t.accounting_status = 'complete' THEN t.net_pnl_usd::float8 ELSE NULL END AS net_pnl_usd,
			CASE WHEN t.accounting_status = 'complete' THEN t.net_pnl_pct::float8 ELSE NULL END AS net_pnl_pct,
			t.pnl_usd::float8 AS pnl_usd, t.pnl_pct::float8 AS pnl_pct,
			t.accounting_version, t.accounting_status, t.accounting_error,
			t.status, t.outcome_label,
			t.setup_context, t.notes, t.created_at
		FROM app.trades t
		LEFT JOIN app.strategies s ON s.id = t.strategy_id
		UNION ALL
		SELECT
			'momentum_flow_paper:' || p.paper_id::text AS id,
			'momentum_flow_paper'::text AS origin,
			'momentum_flow_v1' AS strategy_key,
			'momentum_flow' AS strategy_name,
			'1' AS strategy_version,
			'paper' AS mode,
			p.exit_reason AS exit_reason,
			p.symbol, p.exchange, p.market_type, 'long'::varchar AS side,
			p.entry_filled_notional_usd::float8 AS size_usd, 1::float8 AS leverage,
			p.entry_vwap::float8 AS entry_price, p.entry_at,
			p.exit_vwap::float8 AS exit_price, p.exit_at,
			NULL::float8 AS entry_slippage_bps,
			NULL::float8 AS exit_slippage_bps,
			coalesce(p.fees_usd, 0)::float8 AS fees_usd, coalesce(p.funding_usd, 0)::float8 AS funding_usd,
			NULL::float8 AS slippage_usd,
			p.gross_pnl_usd::float8 AS gross_pnl_usd, p.gross_return_pct::float8 AS gross_pnl_pct,
			CASE WHEN p.accounting_status = 'complete' THEN p.net_pnl_usd::float8 ELSE NULL END AS net_pnl_usd,
			CASE WHEN p.accounting_status = 'complete' THEN p.net_return_pct::float8 ELSE NULL END AS net_pnl_pct,
			NULL::float8 AS pnl_usd, NULL::float8 AS pnl_pct,
			'momentum_flow_paper_v1'::varchar AS accounting_version,
			coalesce(p.accounting_status, 'pending')::varchar AS accounting_status,
			p.accounting_error,
			p.position_status AS status, p.exit_reason AS outcome_label,
			'{}'::jsonb AS setup_context, NULL::text AS notes, p.created_at
		FROM app.momentum_flow_paper_probes p
		WHERE p.entry_status = 'opened'
	)
`

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
	ID                string          `json:"id"`
	Origin            string          `json:"origin"`
	StrategyKey       string          `json:"strategy_key"`
	StrategyName      string          `json:"strategy_name"`
	StrategyVersion   string          `json:"strategy_version"`
	Mode              string          `json:"mode"`
	ExitReason        *string         `json:"exit_reason"`
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
// Query params: status (open|closed), exchange, origin (pump_short|momentum_flow_paper), limit, offset
func (h *Handler) List(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()

	status := q.Get("status")
	exchange := q.Get("exchange")
	origin := q.Get("origin")
	strategy := q.Get("strategy")
	mode := q.Get("mode")
	side := q.Get("side")

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
		where += " AND status = $" + strconv.Itoa(len(args))
	}
	if exchange != "" {
		args = append(args, exchange)
		where += " AND exchange = $" + strconv.Itoa(len(args))
	}
	if origin != "" {
		args = append(args, origin)
		where += " AND origin = $" + strconv.Itoa(len(args))
	}
	if strategy != "" {
		args = append(args, strategy)
		where += " AND strategy_name = $" + strconv.Itoa(len(args))
	}
	if mode != "" {
		args = append(args, mode)
		where += " AND mode = $" + strconv.Itoa(len(args))
	}
	if side != "" {
		args = append(args, side)
		where += " AND side = $" + strconv.Itoa(len(args))
	}

	var total int
	if err := h.pool.QueryRow(r.Context(),
		combinedTradesCTE+"SELECT COUNT(*) FROM combined "+where, args...,
	).Scan(&total); err != nil {
		slog.Error("trades.count", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	//nolint:gocritic
	dataArgs := append(args, limit, offset)
	n := len(dataArgs)
	rows, err := h.pool.Query(r.Context(), combinedTradesCTE+`
		SELECT id, origin, strategy_key, strategy_name, strategy_version, mode, exit_reason, symbol, exchange, market_type, side,
		       size_usd, leverage,
		       entry_price, entry_at,
		       exit_price, exit_at,
		       entry_slippage_bps, exit_slippage_bps,
		       fees_usd, funding_usd, slippage_usd,
		       gross_pnl_usd, gross_pnl_pct,
		       net_pnl_usd, net_pnl_pct,
		       pnl_usd, pnl_pct,
		       accounting_version, accounting_status, accounting_error,
		       status, outcome_label,
		       setup_context, notes, created_at
		FROM combined `+where+`
		ORDER BY entry_at DESC
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
			&t.ID, &t.Origin, &t.StrategyKey, &t.StrategyName, &t.StrategyVersion, &t.Mode, &t.ExitReason, &t.Symbol, &t.Exchange, &t.MarketType, &t.Side,
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
	// NetSubsetGrossUSD/Pct are the gross figures for the exact same trades NetUSD/
	// NetExpectancy cover (accounting_status='complete'), not the whole closed set --
	// GrossUSD above is a different, usually much larger population (it also includes
	// legacy/incomplete-accounting trades that NetUSD can never cover). Comparing
	// GrossUSD to NetUSD directly is comparing two different sets of trades, not the
	// same trades measured two ways; these fields let the UI show gross and net on an
	// apples-to-apples subset so real cost erosion isn't confused with population mix.
	NetSubsetGrossUSD *float64 `json:"net_subset_gross_usd"`
	NetSubsetGrossPct *float64 `json:"net_subset_gross_pct"`
	LegacyCount       int      `json:"legacy_count"`
	IncompleteCount   int      `json:"incomplete_count"`
}

type statsAgg struct {
	Gross                tradeAgg
	Net                  tradeAgg
	NetSubsetGrossUSD    float64 // sum(gross_pnl_usd) over the same rows counted in Net
	NetSubsetGrossSumPct float64 // sum(gross_pnl_pct) over the same rows counted in Net
	LegacyCount          int
	IncompleteCount      int
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
		subsetGrossUSD := a.NetSubsetGrossUSD
		subsetGrossPct := a.NetSubsetGrossSumPct / float64(a.Net.N)
		s.NetSubsetGrossUSD = &subsetGrossUSD
		s.NetSubsetGrossPct = &subsetGrossPct
	}
	return s
}

// Stats handles GET /api/trades/stats: aggregate performance over the whole set of
// closed trades (optionally filtered by exchange and/or origin), not just one page
// of the list.
// statsFilterWhere builds the shared WHERE clause + positional args for the
// exchange/origin/strategy/mode/side filters both Stats and ByStrategy
// accept -- kept in one place so the two endpoints can never silently drift
// on which filters they honor or in what order args get bound.
func statsFilterWhere(q url.Values) (string, []any) {
	args := []any{}
	where := "WHERE status = 'closed' AND gross_pnl_pct IS NOT NULL"
	if v := q.Get("exchange"); v != "" {
		args = append(args, v)
		where += " AND exchange = $" + strconv.Itoa(len(args))
	}
	if v := q.Get("origin"); v != "" {
		args = append(args, v)
		where += " AND origin = $" + strconv.Itoa(len(args))
	}
	if v := q.Get("strategy"); v != "" {
		args = append(args, v)
		where += " AND strategy_name = $" + strconv.Itoa(len(args))
	}
	if v := q.Get("mode"); v != "" {
		args = append(args, v)
		where += " AND mode = $" + strconv.Itoa(len(args))
	}
	if v := q.Get("side"); v != "" {
		args = append(args, v)
		where += " AND side = $" + strconv.Itoa(len(args))
	}
	return where, args
}

// statsAggColumns is the SELECT list computeStats' Scan target list (below)
// depends on positionally -- shared by Stats (one row) and ByStrategy (one
// row per strategy_name/strategy_version) so the two can never drift apart.
const statsAggColumns = `
	       count(*) FILTER (WHERE gross_pnl_pct IS NOT NULL),
	       count(*) FILTER (WHERE gross_pnl_pct > 0),
	       count(*) FILTER (WHERE gross_pnl_pct < 0),
	       COALESCE(sum(gross_pnl_pct), 0)::float8,
	       COALESCE(sum(gross_pnl_pct) FILTER (WHERE gross_pnl_pct > 0), 0)::float8,
	       COALESCE(sum(gross_pnl_pct) FILTER (WHERE gross_pnl_pct < 0), 0)::float8,
	       COALESCE(sum(gross_pnl_usd), 0)::float8,
	       COALESCE(sum(gross_pnl_usd) FILTER (WHERE gross_pnl_usd > 0), 0)::float8,
	       COALESCE(sum(gross_pnl_usd) FILTER (WHERE gross_pnl_usd < 0), 0)::float8,
	       count(*) FILTER (WHERE net_pnl_pct IS NOT NULL),
	       count(*) FILTER (WHERE net_pnl_pct > 0),
	       count(*) FILTER (WHERE net_pnl_pct < 0),
	       COALESCE(sum(net_pnl_pct), 0)::float8,
	       COALESCE(sum(net_pnl_pct) FILTER (WHERE net_pnl_pct > 0), 0)::float8,
	       COALESCE(sum(net_pnl_pct) FILTER (WHERE net_pnl_pct < 0), 0)::float8,
	       COALESCE(sum(net_pnl_usd), 0)::float8,
	       COALESCE(sum(net_pnl_usd) FILTER (WHERE net_pnl_usd > 0), 0)::float8,
	       COALESCE(sum(net_pnl_usd) FILTER (WHERE net_pnl_usd < 0), 0)::float8,
	       count(*) FILTER (WHERE accounting_status = 'legacy'),
	       count(*) FILTER (WHERE accounting_status = 'incomplete'),
	       COALESCE(sum(gross_pnl_usd) FILTER (WHERE net_pnl_pct IS NOT NULL), 0)::float8,
	       COALESCE(sum(gross_pnl_pct) FILTER (WHERE net_pnl_pct IS NOT NULL), 0)::float8`

// scanStatsAggRow scans exactly the columns statsAggColumns selects, in the
// same order, into a. leading, when given, is scanned first -- ByStrategy's
// GROUP BY prepends strategy_name/strategy_version columns that Stats'
// single-row query doesn't have. pgx.Rows also satisfies the pgx.Row
// interface (both are just Scan(dest ...any) error), so the same helper
// covers Stats' QueryRow and ByStrategy's per-row Query loop -- one Scan
// call site instead of two that could silently drift apart.
func scanStatsAggRow(row pgx.Row, a *statsAgg, leading ...any) error {
	dest := make([]any, 0, len(leading)+22)
	dest = append(dest, leading...)
	dest = append(dest,
		&a.Gross.N, &a.Gross.Wins, &a.Gross.Losses,
		&a.Gross.SumPct, &a.Gross.SumWinPct, &a.Gross.SumLossPct,
		&a.Gross.TotalUSD, &a.Gross.WinningUSD, &a.Gross.LosingUSD,
		&a.Net.N, &a.Net.Wins, &a.Net.Losses,
		&a.Net.SumPct, &a.Net.SumWinPct, &a.Net.SumLossPct,
		&a.Net.TotalUSD, &a.Net.WinningUSD, &a.Net.LosingUSD,
		&a.LegacyCount, &a.IncompleteCount,
		&a.NetSubsetGrossUSD, &a.NetSubsetGrossSumPct,
	)
	return row.Scan(dest...)
}

func (h *Handler) Stats(w http.ResponseWriter, r *http.Request) {
	where, args := statsFilterWhere(r.URL.Query())

	var a statsAgg
	row := h.pool.QueryRow(r.Context(),
		combinedTradesCTE+"SELECT"+statsAggColumns+"\nFROM combined "+where, args...,
	)
	if err := scanStatsAggRow(row, &a); err != nil {
		slog.Error("trades.stats", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(computeStats(a)); err != nil {
		slog.Error("trades.stats.encode", "err", err)
	}
}

// strategyStatsEntry is one (strategy_name, strategy_version) bucket's full
// statsResponse -- flattened by anonymous embedding so the JSON shape is
// exactly Stats' own object shape plus the two identity fields, not a
// nested duplicate of it.
type strategyStatsEntry struct {
	StrategyName    string `json:"strategy_name"`
	StrategyVersion string `json:"strategy_version"`
	statsResponse
}

type byStrategyResponse struct {
	Strategies []strategyStatsEntry `json:"strategies"`
}

// ByStrategy handles GET /api/trades/stats/by-strategy: the same aggregate
// Stats computes, broken down per (strategy_name, strategy_version) instead
// of blended into one number. Different strategy versions are frequently
// different algorithms (see early_momentum v1 vs v4's input-quality
// gating) -- blending them the way the single-bucket Stats endpoint must
// hides exactly the comparison this endpoint exists for. Accepts the same
// exchange/origin/mode/side filters as Stats (and strategy, to narrow to
// one strategy's own versions); the caller controls fan-out, not this
// endpoint deciding what "all strategies" means.
func (h *Handler) ByStrategy(w http.ResponseWriter, r *http.Request) {
	where, args := statsFilterWhere(r.URL.Query())

	rows, err := h.pool.Query(r.Context(), combinedTradesCTE+`
		SELECT strategy_name, strategy_version,`+statsAggColumns+`
		FROM combined `+where+`
		GROUP BY strategy_name, strategy_version
		ORDER BY strategy_name, strategy_version`,
		args...,
	)
	if err != nil {
		slog.Error("trades.by_strategy.query", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	result := make([]strategyStatsEntry, 0)
	for rows.Next() {
		var name, version string
		var a statsAgg
		if err := scanStatsAggRow(rows, &a, &name, &version); err != nil {
			slog.Error("trades.by_strategy.scan", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		result = append(result, strategyStatsEntry{
			StrategyName:    name,
			StrategyVersion: version,
			statsResponse:   computeStats(a),
		})
	}
	if err := rows.Err(); err != nil {
		slog.Error("trades.by_strategy.rows", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(byStrategyResponse{Strategies: result}); err != nil {
		slog.Error("trades.by_strategy.encode", "err", err)
	}
}

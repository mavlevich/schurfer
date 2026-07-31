package research

import (
	"context"
	"encoding/json"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

const (
	liquidTakerContract      = "liquid_taker_candidate_v1"
	liquidTakerWiderContract = "liquid_taker_wider_stop_shadow_v1"
	orderflowContract        = "bybit_orderflow_pilot_v1"
	exitLiquidityContract    = "exit_liquidity_calibration_v1"

	formalEpisodes = 100
	formalClusters = 30
	formalWeeks    = 4
	orderflowDays  = 7
)

var (
	liquidTakerStart      = time.Date(2026, time.July, 30, 0, 0, 0, 0, time.UTC)
	liquidTakerWiderStart = time.Date(2026, time.August, 1, 0, 0, 0, 0, time.UTC)
	orderflowStart        = time.Date(2026, time.July, 30, 18, 15, 0, 0, time.UTC)
	exitLiquidityStart    = time.Date(2026, time.July, 29, 15, 45, 34, 0, time.UTC)
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

type redisHashReader interface {
	HGetAll(ctx context.Context, key string) *redis.MapStringStringCmd
}

type Handler struct {
	db    queryRower
	redis redisHashReader
	now   func() time.Time
}

func NewHandler(pool *pgxpool.Pool, rdb *redis.Client) *Handler {
	return &Handler{
		db:    &poolAdapter{inner: pool},
		redis: rdb,
		now:   time.Now,
	}
}

type Milestone struct {
	Current int  `json:"current"`
	Target  int  `json:"target"`
	Exact   bool `json:"exact"`
}

type CohortProgress struct {
	Key                 string    `json:"key"`
	Title               string    `json:"title"`
	Contract            string    `json:"contract"`
	CohortStart         time.Time `json:"cohort_start"`
	FourWeekCheckpoint  time.Time `json:"four_week_checkpoint"`
	Status              string    `json:"status"`
	MatureInputEpisodes Milestone `json:"mature_input_episodes"`
	AssetClusters       Milestone `json:"asset_clusters"`
	CalendarWeeks       Milestone `json:"calendar_weeks"`
	Interpretation      string    `json:"interpretation"`
}

type ExitLiquidityProgress struct {
	Contract               string    `json:"contract"`
	CohortStart            time.Time `json:"cohort_start"`
	State                  string    `json:"state"`
	ClosedPaperShorts      int       `json:"closed_paper_shorts"`
	CapturedObservations   int       `json:"captured_observations"`
	ComparableObservations Milestone `json:"comparable_observations"`
	DecisionTarget         int       `json:"decision_target"`
	AssetClusters          int       `json:"asset_clusters"`
	MeanDeltaBPS           *float64  `json:"mean_delta_bps"`
	Interpretation         string    `json:"interpretation"`
}

type OrderflowProgress struct {
	Contract                 string    `json:"contract"`
	CohortStart              time.Time `json:"cohort_start"`
	Status                   string    `json:"status"`
	ActivationTotal          int       `json:"activation_total"`
	ActiveCaptures           int       `json:"active_captures"`
	CompletedWindowsEstimate Milestone `json:"completed_windows_estimate"`
	MarketDaysElapsed        Milestone `json:"market_days_elapsed"`
	RecordsPersisted         int64     `json:"records_persisted_total"`
	StorageBytes             int64     `json:"storage_bytes"`
	WindowMaxLagMS           int64     `json:"window_max_lag_ms"`
	DropOrErrorTotal         int64     `json:"drop_or_error_total"`
	UpdatedAt                time.Time `json:"updated_at"`
	Interpretation           string    `json:"interpretation"`
}

type Response struct {
	GeneratedAt        time.Time             `json:"generated_at"`
	Interpretation     string                `json:"interpretation"`
	ProspectiveCohorts []CohortProgress      `json:"prospective_cohorts"`
	ExitLiquidity      ExitLiquidityProgress `json:"exit_liquidity"`
	Orderflow          *OrderflowProgress    `json:"orderflow"`
}

type cohortCounts struct {
	episodes int
	clusters int
	weeks    int
}

const cohortProgressSQL = `
WITH candidate_events AS (
	SELECT DISTINCT d.pump_event_id
	FROM app.trade_decisions AS d
	WHERE d.ts >= $1
	  AND d.ts < $2
	  AND d.strategy_version = 'pump_short_v1_market_quality'
	  AND d.pump_event_id IS NOT NULL
),
episode_inputs AS (
	SELECT
		d.pump_event_id,
		upper(e.base) AS base,
		date_trunc('week', min(d.ts) AT TIME ZONE 'UTC') AS episode_week,
		bool_and(
			d.decision_id IS NOT NULL
			AND d.strategy_version = 'pump_short_v1_market_quality'
			AND upper(d.base) = upper(e.base)
			AND d.price IS NOT NULL
			AND d.price > 0
			AND d.features IS NOT NULL
			AND d.features ? 'config'
			AND d.features ? 'signal'
			AND d.liquidity IS NOT NULL
			AND o.id IS NOT NULL
		) AS mature_input
	FROM candidate_events AS candidates
	JOIN app.trade_decisions AS d
	  ON d.pump_event_id = candidates.pump_event_id
	 AND d.ts >= $1
	 AND d.ts < $2
	JOIN app.pump_events AS e
	  ON e.id = d.pump_event_id
	LEFT JOIN app.trade_decision_outcomes AS o
	  ON o.decision_id = d.decision_id
	 AND o.resolver_version = 'forward_v1'
	 AND o.horizon_minutes = 480
	 AND o.status = 'complete'
	WHERE coalesce(e.entry_qualified_at, e.first_seen_at) >= $1
	  AND e.closed_at IS NOT NULL
	  AND e.closed_at < $2
	GROUP BY d.pump_event_id, e.base
)
SELECT
	count(*) FILTER (WHERE mature_input),
	count(DISTINCT base) FILTER (WHERE mature_input),
	count(DISTINCT episode_week) FILTER (WHERE mature_input)
FROM episode_inputs`

func (h *Handler) cohortProgress(ctx context.Context, since, until time.Time) (cohortCounts, error) {
	var counts cohortCounts
	err := h.db.QueryRow(ctx, cohortProgressSQL, since, until).Scan(
		&counts.episodes,
		&counts.clusters,
		&counts.weeks,
	)
	return counts, err
}

const exitLiquidityProgressSQL = `
WITH calibration AS (
	SELECT
		t.symbol,
		t.exit_slippage_bps::float8 AS modeled_exit_bps,
		o.id AS observation_id,
		o.status AS observation_status,
		o.ask_impact_bps::float8 AS observed_exit_bps,
		(
			o.id IS NOT NULL
			AND o.status = 'sampled'
			AND o.exchange = t.exchange
			AND o.symbol = t.symbol
			AND t.exit_slippage_bps IS NOT NULL
			AND t.exit_slippage_bps >= 0
			AND t.exit_slippage_bps::text NOT IN ('NaN', 'Infinity', '-Infinity')
			AND o.ask_impact_bps IS NOT NULL
			AND o.ask_impact_bps >= 0
			AND o.ask_impact_bps::text NOT IN ('NaN', 'Infinity', '-Infinity')
			AND o.spread_bps IS NOT NULL
			AND o.spread_bps >= 0
			AND o.spread_bps::text NOT IN ('NaN', 'Infinity', '-Infinity')
			AND o.latency_ms >= 0
			AND abs(extract(epoch FROM (t.exit_at - o.observed_at))) <= 120
			AND o.requested_notional_usd >= 0
			AND o.requested_notional_usd::text NOT IN ('NaN', 'Infinity', '-Infinity')
			AND abs(o.requested_notional_usd - t.size_usd) <= 0.01
			AND o.filled_notional_usd IS NOT NULL
			AND o.filled_notional_usd >= 0
			AND o.filled_notional_usd::text NOT IN ('NaN', 'Infinity', '-Infinity')
			AND o.filled_notional_usd + 0.01 >= o.requested_notional_usd
			AND t.entry_at <= t.exit_at
		) AS comparable
	FROM app.trades AS t
	LEFT JOIN app.trade_exit_liquidity_observations AS o
	  ON o.trade_id = t.id
	WHERE t.setup_context->>'paper' = 'true'
	  AND t.side = 'short'
	  AND t.status = 'closed'
	  AND t.exit_at >= $1
	  AND t.exit_at < $2
)
SELECT
	count(*),
	count(observation_id),
	count(*) FILTER (WHERE comparable),
	count(DISTINCT upper(split_part(symbol, '/', 1))) FILTER (WHERE comparable),
	avg(observed_exit_bps - modeled_exit_bps) FILTER (WHERE comparable)
FROM calibration`

func (h *Handler) exitLiquidityProgress(
	ctx context.Context,
	until time.Time,
) (ExitLiquidityProgress, error) {
	progress := ExitLiquidityProgress{
		Contract:       exitLiquidityContract,
		CohortStart:    exitLiquidityStart,
		DecisionTarget: 100,
		Interpretation: "executable_close_quote_not_actual_fill",
	}
	var comparable int
	err := h.db.QueryRow(ctx, exitLiquidityProgressSQL, exitLiquidityStart, until).Scan(
		&progress.ClosedPaperShorts,
		&progress.CapturedObservations,
		&comparable,
		&progress.AssetClusters,
		&progress.MeanDeltaBPS,
	)
	progress.ComparableObservations = Milestone{
		Current: comparable,
		Target:  30,
		Exact:   true,
	}
	switch {
	case comparable >= 100:
		progress.State = "decision_ready"
	case comparable >= 30:
		progress.State = "directional"
	default:
		progress.State = "collecting"
	}
	return progress, err
}

func parseInt64(values map[string]string, key string) (int64, bool) {
	value, ok := values[key]
	if !ok {
		return 0, false
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	return parsed, err == nil
}

func parseFloat64(values map[string]string, key string) (float64, bool) {
	value, ok := values[key]
	if !ok {
		return 0, false
	}
	parsed, err := strconv.ParseFloat(value, 64)
	return parsed, err == nil && !math.IsNaN(parsed) && !math.IsInf(parsed, 0)
}

func (h *Handler) orderflowProgress(ctx context.Context, now time.Time) *OrderflowProgress {
	values, err := h.redis.HGetAll(ctx, "market:orderflow:health").Result()
	if err != nil || len(values) == 0 {
		return nil
	}
	updatedAtMS, ok := parseInt64(values, "updated_at_ms")
	if !ok {
		return nil
	}
	activationTotal, ok := parseInt64(values, "activation_total")
	if !ok {
		return nil
	}
	activeCaptures, ok := parseInt64(values, "active_captures")
	if !ok {
		return nil
	}
	if _, ok := parseFloat64(values, "event_rate_per_sec"); !ok {
		return nil
	}
	completed := max(0, activationTotal-activeCaptures)
	elapsedDays := 0
	if now.After(orderflowStart) {
		elapsedDays = int(now.Sub(orderflowStart)/(24*time.Hour)) + 1
	}
	errors := optionalInt64(values, "queue_dropped_total") +
		optionalInt64(values, "pending_dropped_total") +
		optionalInt64(values, "persist_errors_total") +
		optionalInt64(values, "storage_limited_total")
	return &OrderflowProgress{
		Contract:        orderflowContract,
		CohortStart:     orderflowStart,
		Status:          values["status"],
		ActivationTotal: int(activationTotal),
		ActiveCaptures:  int(activeCaptures),
		CompletedWindowsEstimate: Milestone{
			Current: int(completed),
			Target:  formalEpisodes,
			Exact:   false,
		},
		MarketDaysElapsed: Milestone{
			Current: min(elapsedDays, orderflowDays),
			Target:  orderflowDays,
			Exact:   false,
		},
		RecordsPersisted: optionalInt64(values, "records_persisted_total"),
		StorageBytes:     optionalInt64(values, "storage_bytes"),
		WindowMaxLagMS:   optionalInt64(values, "window_max_lag_ms"),
		DropOrErrorTotal: errors,
		UpdatedAt:        time.UnixMilli(updatedAtMS).UTC(),
		Interpretation:   "operational_estimate_report_validates_complete_episodes_clusters_and_days",
	}
}

func optionalInt64(values map[string]string, key string) int64 {
	value, _ := parseInt64(values, key)
	return value
}

func cohortStatus(now, start time.Time, counts cohortCounts) string {
	if now.Before(start) {
		return "scheduled"
	}
	if counts.episodes >= formalEpisodes &&
		counts.clusters >= formalClusters &&
		counts.weeks >= formalWeeks {
		return "report_required"
	}
	return "collecting"
}

func cohort(
	key, title, contract string,
	start, now time.Time,
	counts cohortCounts,
) CohortProgress {
	return CohortProgress{
		Key:                key,
		Title:              title,
		Contract:           contract,
		CohortStart:        start,
		FourWeekCheckpoint: start.AddDate(0, 0, 28),
		Status:             cohortStatus(now, start, counts),
		MatureInputEpisodes: Milestone{
			Current: counts.episodes,
			Target:  formalEpisodes,
			Exact:   false,
		},
		AssetClusters: Milestone{
			Current: counts.clusters,
			Target:  formalClusters,
			Exact:   false,
		},
		CalendarWeeks: Milestone{
			Current: counts.weeks,
			Target:  formalWeeks,
			Exact:   false,
		},
		Interpretation: "mature_database_inputs_only_formal_replay_may_exclude_paths_or_invalid_inputs",
	}
}

// Readiness returns collection progress only. It deliberately does not run CCXT,
// fetch market paths, or issue a strategy verdict.
func (h *Handler) Readiness(w http.ResponseWriter, r *http.Request) {
	now := h.now().UTC()
	liquidCounts, err := h.cohortProgress(r.Context(), liquidTakerStart, now)
	if err != nil {
		slog.Error("research.liquid_taker_progress", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	widerCounts := cohortCounts{}
	if !now.Before(liquidTakerWiderStart) {
		widerCounts, err = h.cohortProgress(r.Context(), liquidTakerWiderStart, now)
		if err != nil {
			slog.Error("research.liquid_taker_wider_progress", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
	}
	exitProgress, err := h.exitLiquidityProgress(r.Context(), now)
	if err != nil {
		slog.Error("research.exit_liquidity_progress", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	response := Response{
		GeneratedAt:    now,
		Interpretation: "collection_progress_only_no_strategy_change",
		ProspectiveCohorts: []CohortProgress{
			cohort(
				"hyp_008",
				"Liquid taker shelf",
				liquidTakerContract,
				liquidTakerStart,
				now,
				liquidCounts,
			),
			cohort(
				"hyp_010",
				"Liquid taker + wider stop",
				liquidTakerWiderContract,
				liquidTakerWiderStart,
				now,
				widerCounts,
			),
		},
		ExitLiquidity: exitProgress,
		Orderflow:     h.orderflowProgress(r.Context(), now),
	}

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "private, max-age=15")
	if err := json.NewEncoder(w).Encode(response); err != nil {
		slog.Error("research.encode", "err", err)
	}
}

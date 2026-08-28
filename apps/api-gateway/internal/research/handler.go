package research

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"math"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

const (
	liquidTakerContract      = "liquid_taker_candidate_v1"
	liquidTakerWiderContract = "liquid_taker_wider_stop_shadow_v1"
	orderflowContract        = "bybit_orderflow_pilot_v1"
	exitLiquidityContract    = "exit_liquidity_calibration_v1"
	sourceLeadContract       = "source_lead_prospective_capture_v1"

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
	sourceLeadStart       = time.Date(2026, time.August, 2, 0, 0, 0, 0, time.UTC)
	// The date identity registry v2 was frozen and populated with real,
	// evidenced links (research/gate-source-lead-registry-activation-v2;
	// mirrors source_lead_contract.IDENTITY_REGISTRY_V2_START on the
	// analytics side -- keep both in sync). A capture whose
	// source_first_observed_at is before this must never count toward
	// SourceLeadProgress.QualifiedProspective even if its own qualification
	// row says 'qualified': identity was not confirmed at the time that
	// capture occurred, only retroactively. Set a few days past this line's
	// own authoring date, not at midnight today, so it cannot fall before
	// this PR actually merges and deploys (colleague review, 2026-08-28,
	// second round). Bump to the actual deploy date if it lands later;
	// never move it earlier.
	identityRegistryV2Start = time.Date(2026, time.August, 30, 0, 0, 0, 0, time.UTC)
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
	db             queryRower
	redis          redisHashReader
	now            func() time.Time
	checkpointPath string
}

func NewHandler(pool *pgxpool.Pool, rdb *redis.Client) *Handler {
	return &Handler{
		db:             &poolAdapter{inner: pool},
		redis:          rdb,
		now:            time.Now,
		checkpointPath: os.Getenv("RESEARCH_CHECKPOINTS_PATH"),
	}
}

type Milestone struct {
	Current int  `json:"current"`
	Target  int  `json:"target"`
	Exact   bool `json:"exact"`
}

type CohortProgress struct {
	Key                 string                 `json:"key"`
	Title               string                 `json:"title"`
	Contract            string                 `json:"contract"`
	CohortStart         time.Time              `json:"cohort_start"`
	FourWeekCheckpoint  time.Time              `json:"four_week_checkpoint"`
	Status              string                 `json:"status"`
	MatureInputEpisodes Milestone              `json:"mature_input_episodes"`
	AssetClusters       Milestone              `json:"asset_clusters"`
	CalendarWeeks       Milestone              `json:"calendar_weeks"`
	InputDiagnostics    CohortInputDiagnostics `json:"input_diagnostics"`
	LatestReport        *RegisteredReportRun   `json:"latest_report"`
	Interpretation      string                 `json:"interpretation"`
}

type CohortInputDiagnostics struct {
	ClosedCandidateEpisodes     int `json:"closed_candidate_episodes"`
	IgnoredMeasurementDecisions int `json:"ignored_measurement_decisions"`
	UnexpectedStrategyEpisodes  int `json:"unexpected_strategy_episodes"`
	InvalidInputEpisodes        int `json:"invalid_input_episodes"`
	MissingExactOutcomeEpisodes int `json:"missing_exact_outcome_episodes"`
}

type RegisteredReportRun struct {
	Contract                 string    `json:"contract"`
	ReportVersion            string    `json:"report_version"`
	GeneratedAt              time.Time `json:"generated_at"`
	DatasetSince             time.Time `json:"dataset_since"`
	DatasetUntilExclusive    time.Time `json:"dataset_until_exclusive"`
	CodeRevision             string    `json:"code_revision"`
	WorkingTreeDirty         bool      `json:"working_tree_dirty"`
	DecisionInputFingerprint string    `json:"decision_input_fingerprint"`
	Status                   string    `json:"status"`
	Verdict                  string    `json:"verdict"`
	EligibleEpisodes         int       `json:"eligible_episodes"`
	AssetClusters            int       `json:"asset_clusters"`
	CalendarWeeks            int       `json:"calendar_weeks"`
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
	TradeReconnectTotal      int64     `json:"trade_reconnect_total"`
	TradeReadTimeoutTotal    int64     `json:"trade_read_timeout_total"`
	UpdatedAt                time.Time `json:"updated_at"`
	Interpretation           string    `json:"interpretation"`
}

type SourceLeadTargetProgress struct {
	Exchange           string   `json:"exchange"`
	Observations       int      `json:"observations"`
	Sampled            int      `json:"sampled"`
	Excluded           int      `json:"excluded"`
	FetchFailed        int      `json:"fetch_failed"`
	SourceToQuoteP50MS *float64 `json:"source_to_quote_p50_ms"`
	SourceToQuoteP90MS *float64 `json:"source_to_quote_p90_ms"`
	SpreadP50BPS       *float64 `json:"spread_p50_bps"`
	SpreadP90BPS       *float64 `json:"spread_p90_bps"`
	EntryImpactP50BPS  *float64 `json:"entry_impact_p50_bps"`
	EntryImpactP90BPS  *float64 `json:"entry_impact_p90_bps"`
}

type SourceLeadIdentityReviewCandidate struct {
	Base                  string    `json:"base"`
	SourceIdentityKey     *string   `json:"source_identity_key"`
	Captures              int       `json:"captures"`
	FirstObservedAt       time.Time `json:"first_observed_at"`
	LastObservedAt        time.Time `json:"last_observed_at"`
	ExecutableTargets     string    `json:"executable_targets"`
	ExactTargetIdentities int       `json:"exact_target_identities"`
	SourceConflict        bool      `json:"source_conflict"`
}

type SourceLeadProgress struct {
	Contract                string    `json:"contract"`
	CohortStart             time.Time `json:"cohort_start"`
	Status                  string    `json:"status"`
	Captures                int       `json:"captures"`
	SourceEligible          int       `json:"source_eligible"`
	Complete                int       `json:"complete"`
	Excluded                int       `json:"excluded"`
	Abandoned               int       `json:"abandoned"`
	RecentAbandoned         int       `json:"recent_abandoned"`
	RecentCriticalAbandoned int       `json:"recent_critical_abandoned"`
	RecentRoutineAbandoned  int       `json:"recent_routine_abandoned"`
	Collecting              int       `json:"collecting"`
	StaleCollecting         int       `json:"stale_collecting"`
	TargetEligible          Milestone `json:"target_eligible"`
	MatureFourHourWindows   Milestone `json:"mature_four_hour_windows"`
	AssetClusters           Milestone `json:"asset_clusters"`
	CalendarWeeks           Milestone `json:"calendar_weeks"`
	ConfirmedWithinHour     int       `json:"confirmed_within_hour"`
	// Qualified counts every source_lead_qualifications row with
	// status='qualified' under the current qualification_version -- it is
	// NOT itself prospective-clean, because identity_registry_v2 was empty
	// before IdentityRegistryV2Start and a capture's own qualification is
	// only ever computed once, at capture time (colleague review,
	// 2026-08-28: "не применять текущие каталоги ретроактивно" -- identity
	// confirmed today does not make a historical capture's own qualified
	// verdict retroactively trustworthy). QualifiedProspective is the
	// number that actually matters for the money-first net-EV decision:
	// the same qualified count, restricted to captures whose
	// source_first_observed_at is at or after IdentityRegistryV2Start.
	Qualified               int       `json:"qualified"`
	IdentityRegistryV2Start time.Time `json:"identity_registry_v2_start"`
	QualifiedProspective    int       `json:"qualified_prospective"`
	QualificationMissing    int       `json:"qualification_missing"`
	IdentityUnapproved      int       `json:"identity_unapproved"`
	NoExecutableTarget      int       `json:"no_approved_executable_target"`
	// RouteEvidencePending counts qualification_reason=
	// 'route_evidence_not_yet_independent' -- identity and liquidity both
	// checked out and a venue was selected, but ROUTE_EVIDENCE_
	// INDEPENDENTLY_VERIFIED=False (source_lead_qualification.py) means
	// registry v2's evidence only vouches for asset identity, not the
	// specific derivative markets, so nothing can reach status='qualified'
	// yet (colleague review, 2026-08-28, second round). Every one of these
	// rows carries its full would-have-selected venue/impact under its own
	// details['would_select'] in the database, not summarized here.
	RouteEvidencePending        int                                 `json:"route_evidence_pending"`
	SelectedBinance             int                                 `json:"selected_binance"`
	SelectedBybit               int                                 `json:"selected_bybit"`
	IdentityRegistry            *string                             `json:"identity_registry_version"`
	IdentityRegistryFingerprint *string                             `json:"identity_registry_fingerprint"`
	IdentityRegistryMixed       bool                                `json:"identity_registry_mixed"`
	LastObservedAt              *time.Time                          `json:"last_observed_at"`
	Targets                     []SourceLeadTargetProgress          `json:"targets"`
	IdentityReviewCandidates    []SourceLeadIdentityReviewCandidate `json:"identity_review_candidates"`
	HealthFlags                 []string                            `json:"health_flags"`
	LatestReport                *RegisteredReportRun                `json:"latest_report"`
	Interpretation              string                              `json:"interpretation"`
}

type Response struct {
	GeneratedAt        time.Time               `json:"generated_at"`
	Interpretation     string                  `json:"interpretation"`
	ProspectiveCohorts []CohortProgress        `json:"prospective_cohorts"`
	ExitLiquidity      ExitLiquidityProgress   `json:"exit_liquidity"`
	Orderflow          *OrderflowProgress      `json:"orderflow"`
	SourceLead         SourceLeadProgress      `json:"source_lead"`
	CheckpointRunner   *CheckpointOrchestrator `json:"checkpoint_runner"`
}

type cohortCounts struct {
	candidates                  int
	episodes                    int
	clusters                    int
	weeks                       int
	ignoredMeasurementDecisions int
	unexpectedStrategyEpisodes  int
	invalidInputEpisodes        int
	missingExactOutcomeEpisodes int
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
		count(*) FILTER (
			WHERE d.strategy_version = 'pump_short_measurement_v1'
			  AND coalesce(d.features @> '{"measurement_only": true}'::jsonb, false)
		) AS ignored_measurement_decisions,
		bool_and(
			d.strategy_version = 'pump_short_v1_market_quality'
		) FILTER (
			WHERE NOT (
				d.strategy_version = 'pump_short_measurement_v1'
				AND coalesce(d.features @> '{"measurement_only": true}'::jsonb, false)
			)
		) AS strategy_scope_valid,
		bool_and(
			d.decision_id IS NOT NULL
			AND upper(d.base) = upper(e.base)
			AND d.price IS NOT NULL
			AND d.price > 0
			AND d.features IS NOT NULL
			AND d.features ? 'config'
			AND d.features ? 'signal'
			AND d.liquidity IS NOT NULL
		) FILTER (
			WHERE NOT (
				d.strategy_version = 'pump_short_measurement_v1'
				AND coalesce(d.features @> '{"measurement_only": true}'::jsonb, false)
			)
		) AS valid_input,
		bool_and(o.id IS NOT NULL) FILTER (
			WHERE NOT (
				d.strategy_version = 'pump_short_measurement_v1'
				AND coalesce(d.features @> '{"measurement_only": true}'::jsonb, false)
			)
		) AS exact_outcome_complete
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
	count(*),
	count(*) FILTER (
		WHERE strategy_scope_valid AND valid_input AND exact_outcome_complete
	),
	count(DISTINCT base) FILTER (
		WHERE strategy_scope_valid AND valid_input AND exact_outcome_complete
	),
	count(DISTINCT episode_week) FILTER (
		WHERE strategy_scope_valid AND valid_input AND exact_outcome_complete
	),
	coalesce(sum(ignored_measurement_decisions), 0),
	count(*) FILTER (WHERE NOT strategy_scope_valid),
	count(*) FILTER (WHERE NOT valid_input),
	count(*) FILTER (WHERE NOT exact_outcome_complete)
FROM episode_inputs`

func (h *Handler) cohortProgress(ctx context.Context, since, until time.Time) (cohortCounts, error) {
	var counts cohortCounts
	err := h.db.QueryRow(ctx, cohortProgressSQL, since, until).Scan(
		&counts.candidates,
		&counts.episodes,
		&counts.clusters,
		&counts.weeks,
		&counts.ignoredMeasurementDecisions,
		&counts.unexpectedStrategyEpisodes,
		&counts.invalidInputEpisodes,
		&counts.missingExactOutcomeEpisodes,
	)
	return counts, err
}

const latestReportSQL = `
SELECT
	contract,
	report_version,
	generated_at,
	dataset_since,
	dataset_until_exclusive,
	code_revision,
	working_tree_dirty,
	decision_input_fingerprint,
	status,
	verdict,
	eligible_episodes,
	asset_clusters,
	calendar_weeks
FROM app.research_report_runs
WHERE contract = $1
ORDER BY generated_at DESC, id DESC
LIMIT 1`

func (h *Handler) latestReport(ctx context.Context, contract string) (*RegisteredReportRun, error) {
	var report RegisteredReportRun
	err := h.db.QueryRow(ctx, latestReportSQL, contract).Scan(
		&report.Contract,
		&report.ReportVersion,
		&report.GeneratedAt,
		&report.DatasetSince,
		&report.DatasetUntilExclusive,
		&report.CodeRevision,
		&report.WorkingTreeDirty,
		&report.DecisionInputFingerprint,
		&report.Status,
		&report.Verdict,
		&report.EligibleEpisodes,
		&report.AssetClusters,
		&report.CalendarWeeks,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &report, nil
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

const sourceLeadProgressSQL = `
WITH captures AS (
	SELECT
		c.id,
		c.event_id,
		upper(c.base) AS base,
		c.source_first_observed_at,
		c.capture_started_at,
		c.capture_completed_at,
		c.status,
		c.eligibility_reason,
		c.error,
		-- identity_verified is required alongside status='sampled' (colleague
		-- review, 2026-08-28): before registry-activation, a 'sampled' row
		-- could come from the old naive-symbol guess and carry
		-- identity_verified=false -- counting it toward target_eligible would
		-- let a wrong-market observation satisfy the maturity/readiness gate.
		-- source_first_observed_at >= $3 is redundant with the capture-side
		-- gate in capture_claimed_source_leads (which never writes a
		-- 'sampled'+identity_verified row for a pre-cutover capture at all)
		-- but kept as an independent second check -- same defense-in-depth
		-- as QualifiedProspective below, in case any future capture path
		-- ever bypasses that gate.
		coalesce(
			bool_or(
				t.status = 'sampled' AND t.identity_verified
					AND c.source_first_observed_at >= $3
			),
			false
		) AS target_eligible
	FROM app.source_lead_captures AS c
	LEFT JOIN app.source_lead_target_observations AS t
	  ON t.capture_id = c.id
	WHERE c.capture_version = 'source_lead_prospective_capture_v1'
	  AND c.source_first_observed_at >= $1
	  AND c.source_first_observed_at < $2
	GROUP BY c.id
), classified AS (
	SELECT
		captures.*,
		qualification.status AS qualification_status,
		qualification.reason AS qualification_reason,
		qualification.selected_target_exchange,
		qualification.identity_registry_version,
		qualification.identity_registry_fingerprint,
		captures.target_eligible
			AND captures.status = 'complete'
			AND captures.source_first_observed_at + interval '240 minutes' <= $2
			AS mature_four_hour,
		EXISTS (
			SELECT 1
			FROM app.pump_event_sources AS confirmation
			WHERE confirmation.event_id = captures.event_id
			  AND confirmation.exchange IN ('binance', 'bybit')
			  AND confirmation.first_seen_at > captures.source_first_observed_at
			  AND confirmation.first_seen_at <= captures.source_first_observed_at + interval '60 minutes'
		) AS confirmed_within_hour
	FROM captures
	LEFT JOIN app.source_lead_qualifications AS qualification
	  ON qualification.capture_id = captures.id
	 AND qualification.qualification_version = 'source_lead_qualified_capture_v2'
)
SELECT
	count(*),
	count(*) FILTER (WHERE eligibility_reason = 'eligible'),
	count(*) FILTER (WHERE status = 'complete'),
	count(*) FILTER (WHERE status = 'excluded'),
	count(*) FILTER (WHERE status = 'abandoned'),
	count(*) FILTER (
		WHERE status = 'abandoned'
		  AND capture_completed_at >= $2 - interval '24 hours'
	),
	count(*) FILTER (
		WHERE status = 'abandoned'
		  AND capture_completed_at >= $2 - interval '24 hours'
		  AND (
			error = 'capture_queue_full'
			OR error = 'capture_worker_shutdown_timeout'
			OR error LIKE 'capture_worker_failed:%'
		  )
	),
	count(*) FILTER (
		WHERE status = 'abandoned'
		  AND capture_completed_at >= $2 - interval '24 hours'
		  AND NOT coalesce((
			error = 'capture_queue_full'
			OR error = 'capture_worker_shutdown_timeout'
			OR error LIKE 'capture_worker_failed:%'
		  ), false)
	),
	count(*) FILTER (WHERE status = 'collecting'),
	count(*) FILTER (
		WHERE status = 'collecting'
		  AND capture_started_at < $2 - interval '10 minutes'
	),
	count(*) FILTER (WHERE target_eligible AND status = 'complete'),
	count(*) FILTER (WHERE mature_four_hour),
	count(DISTINCT base) FILTER (WHERE mature_four_hour),
	count(DISTINCT date_trunc('week', source_first_observed_at AT TIME ZONE 'UTC'))
		FILTER (WHERE mature_four_hour),
	count(*) FILTER (WHERE target_eligible AND status = 'complete' AND confirmed_within_hour),
	count(*) FILTER (WHERE qualification_status = 'qualified'),
	count(*) FILTER (
		WHERE qualification_status = 'qualified'
		  AND source_first_observed_at >= $3
	),
	count(*) FILTER (WHERE status = 'complete' AND qualification_status IS NULL),
	count(*) FILTER (WHERE qualification_reason = 'source_identity_unapproved'),
	count(*) FILTER (WHERE qualification_reason = 'no_approved_executable_target'),
	count(*) FILTER (WHERE qualification_reason = 'route_evidence_not_yet_independent'),
	count(*) FILTER (WHERE selected_target_exchange = 'binance'),
	count(*) FILTER (WHERE selected_target_exchange = 'bybit'),
	CASE
		WHEN count(DISTINCT identity_registry_version)
			FILTER (WHERE identity_registry_version IS NOT NULL) = 1
		THEN min(identity_registry_version)
	END,
	CASE
		WHEN count(DISTINCT identity_registry_fingerprint)
			FILTER (WHERE identity_registry_fingerprint IS NOT NULL) = 1
		THEN min(identity_registry_fingerprint)
	END,
	count(DISTINCT identity_registry_version)
		FILTER (WHERE identity_registry_version IS NOT NULL) > 1
	OR count(DISTINCT identity_registry_fingerprint)
		FILTER (WHERE identity_registry_fingerprint IS NOT NULL) > 1
	OR count(*) FILTER (
		WHERE (identity_registry_version IS NULL)
			IS DISTINCT FROM (identity_registry_fingerprint IS NULL)
	) > 0,
	max(source_first_observed_at)
FROM classified`

const sourceLeadTargetProgressSQL = `
WITH observations AS (
	SELECT
		t.status,
		t.observed_at,
		t.identity_verified,
		c.source_first_observed_at,
		CASE
			WHEN jsonb_typeof(t.liquidity->'spread_bps') = 'number'
			THEN (t.liquidity->>'spread_bps')::float8
		END AS spread_bps,
		CASE
			WHEN jsonb_typeof(t.liquidity->'ask_impact_bps') = 'number'
			THEN (t.liquidity->>'ask_impact_bps')::float8
		END AS entry_impact_bps
	FROM app.source_lead_target_observations AS t
	JOIN app.source_lead_captures AS c
	  ON c.id = t.capture_id
	WHERE c.capture_version = 'source_lead_prospective_capture_v1'
	  AND c.source_first_observed_at >= $1
	  AND c.source_first_observed_at < $2
	  AND t.target_exchange = $3
)
SELECT
	count(*),
	count(*) FILTER (WHERE status = 'sampled'),
	count(*) FILTER (WHERE status = 'excluded'),
	count(*) FILTER (WHERE status = 'fetch_failed'),
	-- Latency/spread/impact percentiles require identity_verified AND
	-- source_first_observed_at >= $4 (identityRegistryV2Start) alongside
	-- status='sampled' (colleague review, 2026-08-28): these numbers feed
	-- venue-quality analysis directly, so neither a pre-activation
	-- wrong-market row nor a pre-cutover row (redundant with the
	-- capture-side gate, kept as an independent second check) can pollute
	-- them. Sampled/excluded/fetch_failed counts above stay unfiltered --
	-- they are the raw operational funnel, a different (and still honest)
	-- metric.
	percentile_cont(0.5) WITHIN GROUP (
		ORDER BY extract(epoch FROM (observed_at - source_first_observed_at)) * 1000
	) FILTER (
		WHERE status = 'sampled' AND identity_verified
		  AND source_first_observed_at >= $4 AND observed_at >= source_first_observed_at
	),
	percentile_cont(0.9) WITHIN GROUP (
		ORDER BY extract(epoch FROM (observed_at - source_first_observed_at)) * 1000
	) FILTER (
		WHERE status = 'sampled' AND identity_verified
		  AND source_first_observed_at >= $4 AND observed_at >= source_first_observed_at
	),
	percentile_cont(0.5) WITHIN GROUP (ORDER BY spread_bps)
		FILTER (
			WHERE status = 'sampled' AND identity_verified
			  AND source_first_observed_at >= $4 AND spread_bps >= 0
		),
	percentile_cont(0.9) WITHIN GROUP (ORDER BY spread_bps)
		FILTER (
			WHERE status = 'sampled' AND identity_verified
			  AND source_first_observed_at >= $4 AND spread_bps >= 0
		),
	percentile_cont(0.5) WITHIN GROUP (ORDER BY entry_impact_bps)
		FILTER (
			WHERE status = 'sampled' AND identity_verified
			  AND source_first_observed_at >= $4 AND entry_impact_bps >= 0
		),
	percentile_cont(0.9) WITHIN GROUP (ORDER BY entry_impact_bps)
		FILTER (
			WHERE status = 'sampled' AND identity_verified
			  AND source_first_observed_at >= $4 AND entry_impact_bps >= 0
		)
FROM observations`

const sourceLeadIdentityReviewSQL = `
WITH identity_groups AS (
	SELECT
		upper(c.base) AS base,
		c.source_identity_key,
		count(DISTINCT c.id) AS captures,
		min(c.source_first_observed_at) AS first_observed_at,
		max(c.source_first_observed_at) AS last_observed_at,
		string_agg(DISTINCT t.target_exchange, ',' ORDER BY t.target_exchange)
			FILTER (
				WHERE t.status = 'sampled'
				  AND nullif(t.instrument->>'identity_key', '') IS NOT NULL
				  AND jsonb_typeof(t.liquidity->'bid_impact_bps') = 'number'
				  AND jsonb_typeof(t.liquidity->'ask_impact_bps') = 'number'
				  AND jsonb_typeof(t.liquidity->'bid_filled_notional_usd') = 'number'
				  AND jsonb_typeof(t.liquidity->'ask_filled_notional_usd') = 'number'
				  AND (t.liquidity->>'bid_filled_notional_usd')::numeric + 0.01
					>= t.requested_notional_usd
				  AND (t.liquidity->>'ask_filled_notional_usd')::numeric + 0.01
					>= t.requested_notional_usd
			) AS executable_targets,
		count(DISTINCT (t.target_exchange, t.instrument->>'identity_key'))
			FILTER (
				WHERE t.status = 'sampled'
				  AND nullif(t.instrument->>'identity_key', '') IS NOT NULL
			) AS exact_target_identities,
		bool_or(c.source_payload @> '{"identity_conflict": true}'::jsonb) AS source_conflict
	FROM app.source_lead_captures AS c
	LEFT JOIN app.source_lead_target_observations AS t ON t.capture_id = c.id
	WHERE c.capture_version = 'source_lead_prospective_capture_v1'
	  AND c.source_first_observed_at >= $1
	  AND c.source_first_observed_at < $2
	  AND c.status = 'complete'
	  AND c.eligibility_reason = 'eligible'
	GROUP BY upper(c.base), c.source_identity_key
)
SELECT coalesce(
	jsonb_agg(
		jsonb_build_object(
			'base', base,
			'source_identity_key', source_identity_key,
			'captures', captures,
			'first_observed_at', first_observed_at,
			'last_observed_at', last_observed_at,
			'executable_targets', coalesce(executable_targets, ''),
			'exact_target_identities', exact_target_identities,
			'source_conflict', source_conflict
		) ORDER BY last_observed_at DESC, base, source_identity_key
	), '[]'::jsonb
)::text
FROM identity_groups`

func sourceLeadStatus(now time.Time, progress SourceLeadProgress) string {
	switch {
	case now.Before(progress.CohortStart):
		return "scheduled"
	case progress.StaleCollecting > 0:
		return "unhealthy"
	case progress.IdentityRegistryMixed:
		return "unhealthy"
	case progress.RecentCriticalAbandoned > 0:
		return "degraded"
	case progress.MatureFourHourWindows.Current >= formalEpisodes &&
		progress.AssetClusters.Current >= formalClusters &&
		progress.CalendarWeeks.Current >= formalWeeks:
		return "report_required"
	default:
		return "collecting"
	}
}

func (h *Handler) sourceLeadProgress(
	ctx context.Context,
	now time.Time,
) (SourceLeadProgress, error) {
	progress := SourceLeadProgress{
		Contract:                sourceLeadContract,
		CohortStart:             sourceLeadStart,
		IdentityRegistryV2Start: identityRegistryV2Start,
		Targets: []SourceLeadTargetProgress{
			{Exchange: "binance"},
			{Exchange: "bybit"},
		},
		HealthFlags:              []string{},
		IdentityReviewCandidates: []SourceLeadIdentityReviewCandidate{},
		Interpretation: "exact_operational_capture_progress_no_strategy_verdict_" +
			"provisional_identity",
	}
	if now.Before(sourceLeadStart) {
		progress.Status = sourceLeadStatus(now, progress)
		progress.TargetEligible = Milestone{Target: formalEpisodes, Exact: true}
		progress.MatureFourHourWindows = Milestone{Target: formalEpisodes, Exact: true}
		progress.AssetClusters = Milestone{Target: formalClusters, Exact: true}
		progress.CalendarWeeks = Milestone{Target: formalWeeks, Exact: true}
		return progress, nil
	}

	var targetEligible, mature, clusters, weeks int
	err := h.db.QueryRow(
		ctx, sourceLeadProgressSQL, sourceLeadStart, now, identityRegistryV2Start,
	).Scan(
		&progress.Captures,
		&progress.SourceEligible,
		&progress.Complete,
		&progress.Excluded,
		&progress.Abandoned,
		&progress.RecentAbandoned,
		&progress.RecentCriticalAbandoned,
		&progress.RecentRoutineAbandoned,
		&progress.Collecting,
		&progress.StaleCollecting,
		&targetEligible,
		&mature,
		&clusters,
		&weeks,
		&progress.ConfirmedWithinHour,
		&progress.Qualified,
		&progress.QualifiedProspective,
		&progress.QualificationMissing,
		&progress.IdentityUnapproved,
		&progress.NoExecutableTarget,
		&progress.RouteEvidencePending,
		&progress.SelectedBinance,
		&progress.SelectedBybit,
		&progress.IdentityRegistry,
		&progress.IdentityRegistryFingerprint,
		&progress.IdentityRegistryMixed,
		&progress.LastObservedAt,
	)
	if err != nil {
		return progress, err
	}
	progress.TargetEligible = Milestone{Current: targetEligible, Target: formalEpisodes, Exact: true}
	progress.MatureFourHourWindows = Milestone{Current: mature, Target: formalEpisodes, Exact: true}
	progress.AssetClusters = Milestone{Current: clusters, Target: formalClusters, Exact: true}
	progress.CalendarWeeks = Milestone{Current: weeks, Target: formalWeeks, Exact: true}

	for index, exchange := range []string{"binance", "bybit"} {
		target := SourceLeadTargetProgress{Exchange: exchange}
		err = h.db.QueryRow(
			ctx,
			sourceLeadTargetProgressSQL,
			sourceLeadStart,
			now,
			exchange,
			identityRegistryV2Start,
		).Scan(
			&target.Observations,
			&target.Sampled,
			&target.Excluded,
			&target.FetchFailed,
			&target.SourceToQuoteP50MS,
			&target.SourceToQuoteP90MS,
			&target.SpreadP50BPS,
			&target.SpreadP90BPS,
			&target.EntryImpactP50BPS,
			&target.EntryImpactP90BPS,
		)
		if err != nil {
			return progress, err
		}
		progress.Targets[index] = target
	}
	var identityReviewJSON string
	err = h.db.QueryRow(
		ctx,
		sourceLeadIdentityReviewSQL,
		sourceLeadStart,
		now,
	).Scan(&identityReviewJSON)
	if err != nil {
		return progress, err
	}
	if err = json.Unmarshal(
		[]byte(identityReviewJSON),
		&progress.IdentityReviewCandidates,
	); err != nil {
		return progress, err
	}
	progress.LatestReport, err = h.latestReport(ctx, sourceLeadContract)
	if err != nil {
		return progress, err
	}
	if progress.StaleCollecting > 0 {
		progress.HealthFlags = append(progress.HealthFlags, "collecting_older_than_10m")
	}
	if progress.RecentCriticalAbandoned > 0 {
		progress.HealthFlags = append(progress.HealthFlags, "critical_capture_failure_last_24h")
	}
	if progress.IdentityRegistryMixed {
		progress.HealthFlags = append(progress.HealthFlags, "mixed_identity_registry_contract")
	}
	progress.Status = sourceLeadStatus(now, progress)
	return progress, nil
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
		RecordsPersisted:      optionalInt64(values, "records_persisted_total"),
		StorageBytes:          optionalInt64(values, "storage_bytes"),
		WindowMaxLagMS:        optionalInt64(values, "window_max_lag_ms"),
		DropOrErrorTotal:      errors,
		TradeReconnectTotal:   optionalInt64(values, "trade_reconnect_total"),
		TradeReadTimeoutTotal: optionalInt64(values, "trade_read_timeout_total"),
		UpdatedAt:             time.UnixMilli(updatedAtMS).UTC(),
		Interpretation:        "operational_estimate_report_validates_complete_episodes_clusters_and_days",
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
	latestReport *RegisteredReportRun,
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
		InputDiagnostics: CohortInputDiagnostics{
			ClosedCandidateEpisodes:     counts.candidates,
			IgnoredMeasurementDecisions: counts.ignoredMeasurementDecisions,
			UnexpectedStrategyEpisodes:  counts.unexpectedStrategyEpisodes,
			InvalidInputEpisodes:        counts.invalidInputEpisodes,
			MissingExactOutcomeEpisodes: counts.missingExactOutcomeEpisodes,
		},
		LatestReport:   latestReport,
		Interpretation: "mature_database_inputs_only_formal_replay_may_exclude_paths_or_invalid_inputs",
	}
}

// Readiness returns collection progress only. It deliberately does not run CCXT,
// fetch market paths, or issue a strategy verdict.
func (h *Handler) Readiness(w http.ResponseWriter, r *http.Request) {
	now := h.now().UTC()
	checkpointRunner := readCheckpointOrchestrator(h.checkpointPath)
	if checkpointRunner != nil {
		checkpointRunner.Stale = checkpointSnapshotIsStale(now, checkpointRunner.GeneratedAt)
	}
	liquidCounts, err := h.cohortProgress(r.Context(), liquidTakerStart, now)
	if err != nil {
		slog.Error("research.liquid_taker_progress", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	liquidReport, err := h.latestReport(r.Context(), liquidTakerContract)
	if err != nil {
		slog.Error("research.liquid_taker_latest_report", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	widerCounts := cohortCounts{}
	var widerReport *RegisteredReportRun
	if !now.Before(liquidTakerWiderStart) {
		widerCounts, err = h.cohortProgress(r.Context(), liquidTakerWiderStart, now)
		if err != nil {
			slog.Error("research.liquid_taker_wider_progress", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		widerReport, err = h.latestReport(r.Context(), liquidTakerWiderContract)
		if err != nil {
			slog.Error("research.liquid_taker_wider_latest_report", "err", err)
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
	sourceLeadProgress, err := h.sourceLeadProgress(r.Context(), now)
	if err != nil {
		slog.Error("research.source_lead_progress", "err", err)
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
				liquidReport,
			),
			cohort(
				"hyp_010",
				"Liquid taker + wider stop",
				liquidTakerWiderContract,
				liquidTakerWiderStart,
				now,
				widerCounts,
				widerReport,
			),
		},
		ExitLiquidity:    exitProgress,
		Orderflow:        h.orderflowProgress(r.Context(), now),
		SourceLead:       sourceLeadProgress,
		CheckpointRunner: checkpointRunner,
	}

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "private, max-age=15")
	if err := json.NewEncoder(w).Encode(response); err != nil {
		slog.Error("research.encode", "err", err)
	}
}

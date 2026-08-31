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
	"golang.org/x/sync/errgroup"
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
	// The date identity registry v3 was frozen and populated with
	// evidence-backed links (research/gate-source-lead-registry-
	// activation-v3; mirrors source_lead_contract.IDENTITY_REGISTRY_V3_START
	// on the analytics side -- keep both in sync). A capture whose
	// source_first_observed_at is before this must never count toward
	// SourceLeadProgress.QualifiedProspective even if its own qualification
	// row says 'qualified': identity was not confirmed at the time that
	// capture occurred, only retroactively. Set a few days past this line's
	// own authoring date, not at midnight today, so it cannot fall before
	// this PR actually merges and deploys (colleague review, 2026-08-28,
	// second round, applied again for the v3 cutover). Bump to the actual
	// deploy date if it lands later; never move it earlier.
	identityRegistryV3Start = time.Date(2026, time.September, 3, 0, 0, 0, 0, time.UTC)
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

// defaultReadinessSubcallTimeout bounds each of Readiness's independent
// sub-calls (see Handler.subcallContext). Chosen well under the server's
// 30s WriteTimeout (apps/api-gateway/cmd/api-gateway/main.go) so that even
// if every sub-call happens to be this slow at once, they run concurrently
// rather than summed -- total wall time stays close to this single budget,
// not a multiple of it. Not tied to any specific query's normal latency;
// revisit if a legitimately slower query needs more room once it exists.
const defaultReadinessSubcallTimeout = 8 * time.Second

type Handler struct {
	db             queryRower
	redis          redisHashReader
	now            func() time.Time
	checkpointPath string
	// subcallTimeout overrides defaultReadinessSubcallTimeout when positive
	// -- zero-value (the common case: NewHandler always sets it, but a
	// hand-built Handler{} in a test does not) falls back to the default in
	// subcallContext, never to an instantly-expired zero timeout. Tests use
	// this to prove Readiness actually bounds a hanging sub-call without
	// paying the full production timeout in wall-clock test time.
	subcallTimeout time.Duration
}

func NewHandler(pool *pgxpool.Pool, rdb *redis.Client) *Handler {
	return &Handler{
		db:             &poolAdapter{inner: pool},
		redis:          rdb,
		now:            time.Now,
		checkpointPath: os.Getenv("RESEARCH_CHECKPOINTS_PATH"),
		subcallTimeout: defaultReadinessSubcallTimeout,
	}
}

// subcallContext derives a per-sub-call timeout from parent (itself already
// derived from the request context by errgroup.WithContext in Readiness),
// so a sub-call is bounded by BOTH the client disconnecting/the request
// context and its own timeout budget, whichever fires first.
func (h *Handler) subcallContext(parent context.Context) (context.Context, context.CancelFunc) {
	timeout := h.subcallTimeout
	if timeout <= 0 {
		timeout = defaultReadinessSubcallTimeout
	}
	return context.WithTimeout(parent, timeout)
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
	// NOT itself prospective-clean, because identity_registry_v3 was empty
	// before IdentityRegistryV3Start and a capture's own qualification is
	// only ever computed once, at capture time (colleague review,
	// 2026-08-28: "не применять текущие каталоги ретроактивно" -- identity
	// confirmed today does not make a historical capture's own qualified
	// verdict retroactively trustworthy). QualifiedProspective is the
	// number that actually matters for the money-first net-EV decision:
	// the same qualified count, restricted to captures whose
	// source_first_observed_at is at or after IdentityRegistryV3Start.
	Qualified               int       `json:"qualified"`
	IdentityRegistryV3Start time.Time `json:"identity_registry_v3_start"`
	QualifiedProspective    int       `json:"qualified_prospective"`
	QualificationMissing    int       `json:"qualification_missing"`
	IdentityUnapproved      int       `json:"identity_unapproved"`
	NoExecutableTarget      int       `json:"no_approved_executable_target"`
	// RouteEvidencePending counts qualification_reason=
	// 'route_evidence_not_yet_independent' -- identity and liquidity both
	// checked out and a venue was selected, but
	// ROUTE_EVIDENCE_INDEPENDENTLY_VERIFIED=False at capture time
	// (source_lead_qualification.py) meant registry v2's evidence only
	// vouched for asset identity, not the specific derivative markets, so
	// qualification could not reach status='qualified' yet (colleague
	// review, 2026-08-28, second round). Flipped to True in
	// research/gate-source-lead-registry-activation-v3 (PR 3 of 3), so this
	// count is now purely historical -- no new row can be tagged this
	// reason going forward, but the qualification join above is not
	// version-scoped (colleague review, PR 3 review round), so every
	// pre-flip row still tagged this reason stays visible here rather than
	// disappearing on deploy. Every one of these rows carries its full
	// would-have-selected venue/impact under its own
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
	GeneratedAt        time.Time        `json:"generated_at"`
	Interpretation     string           `json:"interpretation"`
	ProspectiveCohorts []CohortProgress `json:"prospective_cohorts"`
	// ExitLiquidity and SourceLead are nullable for the same reason
	// Orderflow already was: Readiness runs each section concurrently with
	// its own timeout budget (fix/research-readiness-handler-concurrency-v1)
	// and degrades a slow or failing section to nil instead of failing the
	// whole response -- see Readiness's own doc comment.
	ExitLiquidity    *ExitLiquidityProgress  `json:"exit_liquidity"`
	Orderflow        *OrderflowProgress      `json:"orderflow"`
	SourceLead       *SourceLeadProgress     `json:"source_lead"`
	CheckpointRunner *CheckpointOrchestrator `json:"checkpoint_runner"`
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

// liquidTakerClosedCounts and liquidTakerWiderClosedCounts freeze the formal
// checkpoint counts each cohort matured at (see ROADMAP.md, 2026-08-29
// closeout: both do_not_promote). A promotion decision is locked forever at
// the earliest maturity checkpoint by design -- it does not move as more
// data accumulates -- so these numbers never change again. Serving them as
// constants instead of re-running the historical cohortProgressSQL query on
// every request removes that query's only two call sites, which is what
// produced the 2026-08-29 Readiness timeout incident (18.6s query against
// production data volume). Diagnostic sub-counts (candidates, ignored
// measurement decisions, invalid input episodes) are not preserved from the
// frozen checkpoint and are intentionally left at zero; the frontend hides
// that breakdown for closed cohorts rather than show fabricated numbers.
var (
	liquidTakerClosedCounts      = cohortCounts{episodes: 494, clusters: 207, weeks: 4}
	liquidTakerWiderClosedCounts = cohortCounts{episodes: 429, clusters: 183, weeks: 4}
)

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
	-- Not filtered to qualification_version = 'source_lead_qualified_capture_v3'
	-- (colleague review, 2026-08-29/30, PR 3 review round): captures spans the
	-- full history from sourceLeadStart, but a capture's own qualification row
	-- is written exactly once, at capture time, tagged with whatever
	-- QUALIFICATION_VERSION was live then (source_lead_qualification.py) --
	-- ux_source_lead_qualification_capture_version enforces at most one row
	-- per (capture_id, qualification_version), and application discipline
	-- ("computed once, at capture time") never writes a second one under a
	-- different version for the same capture. Filtering this join to only
	-- the current version made every pre-cutover capture's already-computed
	-- qualification (status/reason/selected exchange) disappear from every
	-- count below the moment this code deploys, even though nothing about
	-- that capture's own row changed -- a scope mismatch between the
	-- full-history captures CTE and a version-scoped join. Joining
	-- unconditionally keeps that history visible; the identity_registry_
	-- version/fingerprint distinct-count logic further down already exists
	-- to surface exactly this kind of multi-version window as "mixed", not
	-- to silently hide it.
	LEFT JOIN app.source_lead_qualifications AS qualification
	  ON qualification.capture_id = captures.id
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
	-- source_first_observed_at >= $4 (identityRegistryV3Start) alongside
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
		IdentityRegistryV3Start: identityRegistryV3Start,
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
		ctx, sourceLeadProgressSQL, sourceLeadStart, now, identityRegistryV3Start,
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
			identityRegistryV3Start,
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

// runSection launches fn on g under its own per-call timeout budget
// (h.subcallContext) and sends fn's own result to a buffered channel of
// capacity 1, returned for the caller to read once g.Wait() completes. A
// non-nil err is logged under logKey and degrades the section to T's zero
// value -- deliberately never returned to g itself, so one failing or slow
// section can never cancel or fail the others (see Readiness's own doc
// comment). Colleague review: this is the isolation mechanism itself, not
// a convention layered on top of it -- a channel's send/receive is Go's
// own happens-before guarantee, so even a future section carelessly added
// to reuse another section's variable cannot reintroduce a data race the
// way it could with N goroutines each assumed (by convention only) to
// write exactly one shared local variable.
func runSection[T any](
	h *Handler, g *errgroup.Group, gCtx context.Context, logKey string,
	fn func(ctx context.Context) (T, error),
) <-chan T {
	out := make(chan T, 1)
	g.Go(func() error {
		ctx, cancel := h.subcallContext(gCtx)
		defer cancel()
		result, err := fn(ctx)
		if err != nil {
			slog.Error(logKey, "err", err)
			var zero T
			out <- zero
			return nil
		}
		out <- result
		return nil
	})
	return out
}

// Readiness returns collection progress only. It deliberately does not run CCXT,
// fetch market paths, or issue a strategy verdict.
//
// Each independent section below runs concurrently (errgroup.WithContext)
// via runSection, under its own context.WithTimeout budget
// (h.subcallTimeout via subcallContext, defaultReadinessSubcallTimeout
// unless a test overrides it) -- fix/research-readiness-handler-
// concurrency-v1, tech debt logged in ROADMAP.md against the 2026-08-29
// production incident: the sequential, unbounded cohortProgressSQL query
// (18.6s against production data volume) starved every section behind it
// and timed out the whole endpoint. A section's own DB/Redis error is
// caught and logged inside runSection and degrades only that section to
// its zero value in the response -- it is deliberately never returned to
// the errgroup itself, so one slow or failing section can never cancel or
// fail the others. g.Wait()'s own error return is therefore not expected
// to fire in normal operation; it exists only so that stays true if a
// future section is added carelessly.
func (h *Handler) Readiness(w http.ResponseWriter, r *http.Request) {
	now := h.now().UTC()
	checkpointRunner := readCheckpointOrchestrator(h.checkpointPath)
	if checkpointRunner != nil {
		checkpointRunner.Stale = checkpointSnapshotIsStale(now, checkpointRunner.GeneratedAt)
	}

	g, gCtx := errgroup.WithContext(r.Context())

	// hyp_008 and hyp_010 are permanently closed (do_not_promote, 2026-08-29
	// -- see ROADMAP.md). Their live cohortProgressSQL query was removed;
	// only the cheap, contract-indexed latest-report lookup still runs.
	liquidCh := runSection(h, g, gCtx, "research.liquid_taker_latest_report",
		func(ctx context.Context) (*RegisteredReportRun, error) {
			return h.latestReport(ctx, liquidTakerContract)
		})
	widerCh := runSection(h, g, gCtx, "research.liquid_taker_wider_latest_report",
		func(ctx context.Context) (*RegisteredReportRun, error) {
			return h.latestReport(ctx, liquidTakerWiderContract)
		})
	exitCh := runSection(h, g, gCtx, "research.exit_liquidity_progress",
		func(ctx context.Context) (*ExitLiquidityProgress, error) {
			progress, err := h.exitLiquidityProgress(ctx, now)
			if err != nil {
				return nil, err
			}
			return &progress, nil
		})
	sourceLeadCh := runSection(h, g, gCtx, "research.source_lead_progress",
		func(ctx context.Context) (*SourceLeadProgress, error) {
			progress, err := h.sourceLeadProgress(ctx, now)
			if err != nil {
				return nil, err
			}
			return &progress, nil
		})
	orderflowCh := runSection(h, g, gCtx, "research.orderflow_progress",
		func(ctx context.Context) (*OrderflowProgress, error) {
			return h.orderflowProgress(ctx, now), nil
		})

	_ = g.Wait()

	liquidReport, widerReport := <-liquidCh, <-widerCh
	exitProgress, sourceLead, orderflow := <-exitCh, <-sourceLeadCh, <-orderflowCh

	liquidCohort := cohort(
		"hyp_008",
		"Liquid taker shelf",
		liquidTakerContract,
		liquidTakerStart,
		now,
		liquidTakerClosedCounts,
		liquidReport,
	)
	liquidCohort.Status = "closed"
	widerCohort := cohort(
		"hyp_010",
		"Liquid taker + wider stop",
		liquidTakerWiderContract,
		liquidTakerWiderStart,
		now,
		liquidTakerWiderClosedCounts,
		widerReport,
	)
	widerCohort.Status = "closed"

	response := Response{
		GeneratedAt:        now,
		Interpretation:     "collection_progress_only_no_strategy_change",
		ProspectiveCohorts: []CohortProgress{liquidCohort, widerCohort},
		ExitLiquidity:      exitProgress,
		Orderflow:          orderflow,
		SourceLead:         sourceLead,
		CheckpointRunner:   checkpointRunner,
	}

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "private, max-age=15")
	if err := json.NewEncoder(w).Encode(response); err != nil {
		slog.Error("research.encode", "err", err)
	}
}

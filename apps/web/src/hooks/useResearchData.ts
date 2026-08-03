import { useQuery } from '@tanstack/react-query';

export interface ResearchMilestone {
  current: number;
  target: number;
  exact: boolean;
}

export interface ProspectiveCohort {
  key: string;
  title: string;
  contract: string;
  cohort_start: string;
  four_week_checkpoint: string;
  status: 'scheduled' | 'collecting' | 'report_required';
  mature_input_episodes: ResearchMilestone;
  asset_clusters: ResearchMilestone;
  calendar_weeks: ResearchMilestone;
  input_diagnostics: {
    closed_candidate_episodes: number;
    ignored_measurement_decisions: number;
    unexpected_strategy_episodes: number;
    invalid_input_episodes: number;
    missing_exact_outcome_episodes: number;
  };
  latest_report: {
    contract: string;
    report_version: string;
    generated_at: string;
    dataset_since: string;
    dataset_until_exclusive: string;
    code_revision: string;
    working_tree_dirty: boolean;
    decision_input_fingerprint: string;
    status: string;
    verdict: string;
    eligible_episodes: number;
    asset_clusters: number;
    calendar_weeks: number;
  } | null;
  interpretation: string;
}

export interface ExitLiquidityProgress {
  contract: string;
  cohort_start: string;
  state: 'collecting' | 'directional' | 'decision_ready';
  closed_paper_shorts: number;
  captured_observations: number;
  comparable_observations: ResearchMilestone;
  decision_target: number;
  asset_clusters: number;
  mean_delta_bps: number | null;
  interpretation: string;
}

export interface OrderflowProgress {
  contract: string;
  cohort_start: string;
  status: string;
  activation_total: number;
  active_captures: number;
  completed_windows_estimate: ResearchMilestone;
  market_days_elapsed: ResearchMilestone;
  records_persisted_total: number;
  storage_bytes: number;
  window_max_lag_ms: number;
  drop_or_error_total: number;
  updated_at: string;
  interpretation: string;
}

export interface SourceLeadTargetProgress {
  exchange: string;
  observations: number;
  sampled: number;
  excluded: number;
  fetch_failed: number;
  source_to_quote_p50_ms: number | null;
  source_to_quote_p90_ms: number | null;
  spread_p50_bps: number | null;
  spread_p90_bps: number | null;
  entry_impact_p50_bps: number | null;
  entry_impact_p90_bps: number | null;
}

export interface SourceLeadIdentityReviewCandidate {
  base: string;
  source_identity_key: string | null;
  captures: number;
  first_observed_at: string;
  last_observed_at: string;
  executable_targets: string;
  exact_target_identities: number;
  source_conflict: boolean;
}

export interface SourceLeadProgress {
  contract: string;
  cohort_start: string;
  status: 'scheduled' | 'collecting' | 'degraded' | 'unhealthy' | 'report_required';
  captures: number;
  source_eligible: number;
  complete: number;
  excluded: number;
  abandoned: number;
  recent_abandoned: number;
  recent_critical_abandoned: number;
  recent_routine_abandoned: number;
  collecting: number;
  stale_collecting: number;
  target_eligible: ResearchMilestone;
  mature_four_hour_windows: ResearchMilestone;
  asset_clusters: ResearchMilestone;
  calendar_weeks: ResearchMilestone;
  confirmed_within_hour: number;
  qualified: number;
  qualification_missing: number;
  identity_unapproved: number;
  no_approved_executable_target: number;
  selected_binance: number;
  selected_bybit: number;
  identity_registry_version: string | null;
  identity_registry_fingerprint: string | null;
  identity_registry_mixed: boolean;
  last_observed_at: string | null;
  targets: SourceLeadTargetProgress[];
  identity_review_candidates: SourceLeadIdentityReviewCandidate[];
  health_flags: string[];
  latest_report: ProspectiveCohort['latest_report'];
  interpretation: string;
}

export interface ResearchCheckpoint {
  key: string;
  title: string;
  contract: string;
  due_at: string;
  state: string;
  next_attempt_at: string | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  report_status: string | null;
  verdict: string | null;
  report_file: string | null;
  report_sha256: string | null;
  error: string | null;
  alert_error: string | null;
}

export interface CheckpointRunner {
  version: string;
  generated_at: string;
  runner_state: string;
  stale: boolean;
  checkpoints: ResearchCheckpoint[];
}

export interface ResearchReadinessResponse {
  generated_at: string;
  interpretation: string;
  prospective_cohorts: ProspectiveCohort[];
  exit_liquidity: ExitLiquidityProgress;
  orderflow: OrderflowProgress | null;
  source_lead: SourceLeadProgress;
  checkpoint_runner: CheckpointRunner | null;
}

async function fetchResearchReadiness(): Promise<ResearchReadinessResponse> {
  const response = await fetch('/api/research/readiness');
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json() as Promise<ResearchReadinessResponse>;
}

export function useResearchReadiness() {
  return useQuery({
    queryKey: ['research-readiness'],
    queryFn: fetchResearchReadiness,
    refetchInterval: 60_000,
    staleTime: 30_000,
    placeholderData: (previous) => previous,
  });
}

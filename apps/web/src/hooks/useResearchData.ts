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

export interface ResearchReadinessResponse {
  generated_at: string;
  interpretation: string;
  prospective_cohorts: ProspectiveCohort[];
  exit_liquidity: ExitLiquidityProgress;
  orderflow: OrderflowProgress | null;
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

import { Activity, ArrowRight, CalendarClock, FlaskConical, Radio, Scale } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { PageShell } from '@/components/shared/PageShell';
import { cn } from '@/lib/utils';
import {
  useResearchReadiness,
  type ProspectiveCohort,
  type ResearchMilestone,
} from '@/hooks/useResearchData';

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return 'n/a';
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GiB`;
  return `${(value / 1024 ** 2).toFixed(1)} MiB`;
}

function statusBadge(status: string) {
  const label = status.split('_').join(' ');
  if (status === 'decision_ready' || status === 'ok') {
    return <Badge variant="success">{label}</Badge>;
  }
  if (status === 'directional') {
    return <Badge variant="warning">directional only</Badge>;
  }
  if (status === 'report_required') {
    return <Badge variant="warning">run formal report</Badge>;
  }
  return <Badge variant="secondary">{label}</Badge>;
}

function MilestoneRow({ label, milestone }: { label: string; milestone: ResearchMilestone }) {
  const progress = Math.min(100, (milestone.current / Math.max(1, milestone.target)) * 100);
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-mono">
          {milestone.current} / {milestone.target}
          {!milestone.exact && <span className="ml-1 text-muted-foreground">est.</span>}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className={cn(
            'h-full rounded-full transition-[width]',
            progress >= 100 ? 'bg-green-500' : 'bg-sky-500',
          )}
          style={{ width: `${progress}%` }}
        />
      </div>
    </div>
  );
}

function CohortCard({ cohort }: { cohort: ProspectiveCohort }) {
  return (
    <Card>
      <CardHeader className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">{cohort.title}</CardTitle>
            <p className="mt-1 font-mono text-xs text-muted-foreground">{cohort.contract}</p>
          </div>
          {statusBadge(cohort.status)}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>Starts {formatDate(cohort.cohort_start)}</span>
          <span>4-week checkpoint {formatDate(cohort.four_week_checkpoint)}</span>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <MilestoneRow label="Mature database inputs" milestone={cohort.mature_input_episodes} />
        <MilestoneRow label="Asset clusters" milestone={cohort.asset_clusters} />
        <MilestoneRow label="UTC weeks" milestone={cohort.calendar_weeks} />
        <p className="text-xs leading-relaxed text-muted-foreground">
          These are lightweight input counters. The registered CCXT replay can still exclude invalid
          inputs or unavailable exact-venue paths.
        </p>
      </CardContent>
    </Card>
  );
}

const orderflowLanes = [
  {
    name: 'Early long',
    description: 'Does pre-trigger buying pressure lead a tradeable long move?',
  },
  {
    name: 'Squeeze avoidance',
    description: 'Can late buying pressure keep the short out of a continuation squeeze?',
  },
  {
    name: 'Delayed short',
    description: 'Does fading buy pressure improve short entry timing after the trigger?',
  },
];

export function ResearchPage() {
  const { data, isError, isFetching, dataUpdatedAt } = useResearchReadiness();

  return (
    <PageShell width="wide">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <FlaskConical className="h-5 w-5 text-sky-400" />
            <h1 className="text-xl font-bold tracking-tight">Research Readiness</h1>
          </div>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            Collection progress for frozen strategy contracts. This page never promotes a strategy
            or changes production settings.
          </p>
        </div>
        <div className="text-right text-xs text-muted-foreground">
          <div>{isFetching ? 'Refreshing...' : 'Refreshes every minute'}</div>
          {dataUpdatedAt > 0 && (
            <div>Updated {new Date(dataUpdatedAt).toLocaleTimeString('en-US')}</div>
          )}
        </div>
      </div>

      {isError && (
        <Card>
          <CardContent className="py-5 text-sm text-red-400">
            Research progress is unavailable. Existing collectors and strategies are unaffected.
          </CardContent>
        </Card>
      )}

      {!data && !isError && (
        <Card>
          <CardContent className="py-5 text-sm text-muted-foreground">
            Loading research milestones...
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            {data.prospective_cohorts.map((cohort) => (
              <CohortCard key={cohort.key} cohort={cohort} />
            ))}
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <CardTitle className="flex items-center gap-2 text-base">
                      <Scale className="h-4 w-4 text-violet-400" />
                      Exit quote calibration
                    </CardTitle>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      {data.exit_liquidity.contract}
                    </p>
                  </div>
                  {statusBadge(data.exit_liquidity.state)}
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <MilestoneRow
                  label="Comparable close quotes"
                  milestone={data.exit_liquidity.comparable_observations}
                />
                <div className="grid grid-cols-2 gap-3 text-sm">
                  <div className="rounded-md border p-3">
                    <p className="text-xs text-muted-foreground">Capture</p>
                    <p className="mt-1 font-mono">
                      {data.exit_liquidity.captured_observations} /{' '}
                      {data.exit_liquidity.closed_paper_shorts}
                    </p>
                  </div>
                  <div className="rounded-md border p-3">
                    <p className="text-xs text-muted-foreground">Mean quote delta</p>
                    <p className="mt-1 font-mono">
                      {data.exit_liquidity.mean_delta_bps === null
                        ? 'n/a'
                        : `${data.exit_liquidity.mean_delta_bps >= 0 ? '+' : ''}${data.exit_liquidity.mean_delta_bps.toFixed(2)} bps`}
                    </p>
                  </div>
                </div>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  The observed value is an executable quote at close time, not an actual fill.
                  Thirty samples permit a directional read; 100 are the decision-grade target.
                </p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <CardTitle className="flex items-center gap-2 text-base">
                      <Radio className="h-4 w-4 text-emerald-400" />
                      Bybit order flow
                    </CardTitle>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      {data.orderflow?.contract ?? 'bybit_orderflow_pilot_v1'}
                    </p>
                  </div>
                  {statusBadge(data.orderflow?.status ?? 'telemetry unavailable')}
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                {data.orderflow ? (
                  <>
                    <MilestoneRow
                      label="Completed capture windows"
                      milestone={data.orderflow.completed_windows_estimate}
                    />
                    <MilestoneRow
                      label="Market days elapsed"
                      milestone={data.orderflow.market_days_elapsed}
                    />
                    <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                      <div>
                        <p className="text-xs text-muted-foreground">Active</p>
                        <p className="font-mono">{data.orderflow.active_captures}</p>
                      </div>
                      <div>
                        <p className="text-xs text-muted-foreground">Lag max</p>
                        <p className="font-mono">{data.orderflow.window_max_lag_ms} ms</p>
                      </div>
                      <div>
                        <p className="text-xs text-muted-foreground">Stored</p>
                        <p className="font-mono">{formatBytes(data.orderflow.storage_bytes)}</p>
                      </div>
                      <div>
                        <p className="text-xs text-muted-foreground">Drops/errors</p>
                        <p
                          className={cn(
                            'font-mono',
                            data.orderflow.drop_or_error_total > 0 && 'text-red-400',
                          )}
                        >
                          {data.orderflow.drop_or_error_total}
                        </p>
                      </div>
                    </div>
                    <p className="text-xs leading-relaxed text-muted-foreground">
                      Completed windows are an operational estimate. The file report verifies all
                      controls, endpoints, 30 clusters, and 7 distinct market days.
                    </p>
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    Collector telemetry is unavailable. No readiness claim is made.
                  </p>
                )}
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Order-flow research lanes</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid gap-3 md:grid-cols-3">
                {orderflowLanes.map((lane) => (
                  <div key={lane.name} className="rounded-md border p-4">
                    <div className="flex items-center justify-between gap-2">
                      <p className="font-medium">{lane.name}</p>
                      <ArrowRight className="h-4 w-4 text-muted-foreground" />
                    </div>
                    <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                      {lane.description}
                    </p>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardContent className="flex flex-col gap-3 py-4 sm:flex-row sm:items-center">
              <CalendarClock className="h-5 w-5 shrink-0 text-muted-foreground" />
              <div className="flex-1">
                <p className="text-sm font-medium">Decision discipline</p>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  Formal strategy output stays in the frozen reports. Until their sample, diversity,
                  time, and path-completeness gates pass, production score, exits, size, leverage,
                  and DRY_RUN remain unchanged.
                </p>
              </div>
              <Badge variant="outline" className="w-fit">
                <Activity className="mr-1 h-3 w-3" />
                collection only
              </Badge>
            </CardContent>
          </Card>
        </>
      )}
    </PageShell>
  );
}

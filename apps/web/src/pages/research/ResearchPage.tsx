import {
  Activity,
  ArrowRight,
  CalendarClock,
  FlaskConical,
  GitBranch,
  Radio,
  Scale,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { PageShell } from '@/components/shared/PageShell';
import { cn } from '@/lib/utils';
import {
  useResearchReadiness,
  type ProspectiveCohort,
  type CheckpointRunner,
  type ResearchMilestone,
  type SourceLeadProgress,
} from '@/hooks/useResearchData';

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'UTC',
    timeZoneName: 'short',
  });
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return 'n/a';
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(2)} GiB`;
  return `${(value / 1024 ** 2).toFixed(1)} MiB`;
}

function statusBadge(status: string) {
  const label = status.split('_').join(' ');
  if (
    status === 'decision_ready' ||
    status === 'discovery_ready' ||
    status === 'shadow_candidate' ||
    status === 'boundary_only_ready' ||
    status === 'ok'
  ) {
    return <Badge variant="success">{label}</Badge>;
  }
  if (status === 'directional') {
    return <Badge variant="warning">directional only</Badge>;
  }
  if (status === 'report_required') {
    return <Badge variant="warning">run formal report</Badge>;
  }
  if (status === 'closed') {
    return <Badge variant="secondary">closed</Badge>;
  }
  if (status === 'unhealthy' || status === 'error' || status === 'no_go' || status === 'stale') {
    return <Badge variant="destructive">{label}</Badge>;
  }
  if (status === 'degraded') {
    return <Badge variant="warning">degraded</Badge>;
  }
  return <Badge variant="secondary">{label}</Badge>;
}

function CheckpointRunnerCard({ runner }: { runner: CheckpointRunner | null }) {
  return (
    <Card>
      <CardHeader className="space-y-2">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <CalendarClock className="h-4 w-4 text-sky-400" />
              Automated checkpoints
            </CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              One bounded report at a time with host memory and disk preflight.
            </p>
          </div>
          {statusBadge(runner ? (runner.stale ? 'stale' : runner.runner_state) : 'not installed')}
        </div>
        {runner && (
          <p className="text-xs text-muted-foreground">
            Last scheduler pass {formatDateTime(runner.generated_at)}
            {runner.stale && ' · expected hourly; inspect the systemd timer'}
          </p>
        )}
      </CardHeader>
      <CardContent>
        {runner ? (
          <div className="divide-y rounded-md border">
            {runner.checkpoints.map((checkpoint) => (
              <div
                key={checkpoint.key}
                className="flex flex-col gap-2 p-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="text-sm font-medium">{checkpoint.title}</p>
                    {statusBadge(checkpoint.state)}
                  </div>
                  <p className="mt-1 truncate font-mono text-xs text-muted-foreground">
                    {checkpoint.contract}
                  </p>
                  {checkpoint.error && (
                    <p className="mt-1 text-xs text-red-400">{checkpoint.error}</p>
                  )}
                  {checkpoint.alert_error && (
                    <p className="mt-1 text-xs text-amber-400">{checkpoint.alert_error}</p>
                  )}
                </div>
                <div className="shrink-0 text-left text-xs text-muted-foreground sm:text-right">
                  <p>
                    {checkpoint.last_success_at
                      ? `Last report ${formatDateTime(checkpoint.last_success_at)}`
                      : `Due ${formatDateTime(checkpoint.due_at)}`}
                  </p>
                  <p>
                    {checkpoint.verdict && checkpoint.verdict !== 'withheld'
                      ? `Verdict ${checkpoint.verdict.split('_').join(' ')}`
                      : checkpoint.next_attempt_at
                        ? `Next check ${formatDateTime(checkpoint.next_attempt_at)}`
                        : 'No next run scheduled'}
                  </p>
                  {checkpoint.report_sha256 && (
                    <p className="font-mono">sha256:{checkpoint.report_sha256.slice(0, 12)}</p>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            Scheduler status is unavailable. Collection continues, but milestone reports still
            require manual runs.
          </p>
        )}
      </CardContent>
    </Card>
  );
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
  const diagnostics = cohort.input_diagnostics;
  const closed = cohort.status === 'closed';
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
        {!closed && (
          <>
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div className="rounded-md border p-3">
                <p className="text-xs text-muted-foreground">Closed candidates</p>
                <p className="mt-1 font-mono">{diagnostics.closed_candidate_episodes}</p>
              </div>
              <div className="rounded-md border p-3">
                <p className="text-xs text-muted-foreground">Measurement rows ignored</p>
                <p className="mt-1 font-mono">{diagnostics.ignored_measurement_decisions}</p>
              </div>
            </div>
            {(diagnostics.unexpected_strategy_episodes > 0 ||
              diagnostics.invalid_input_episodes > 0 ||
              diagnostics.missing_exact_outcome_episodes > 0) && (
              <div className="space-y-1 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs">
                <p className="font-medium text-amber-300">Input flags</p>
                <p className="text-muted-foreground">
                  unexpected strategy {diagnostics.unexpected_strategy_episodes} · invalid input{' '}
                  {diagnostics.invalid_input_episodes} · missing exact 8h outcome{' '}
                  {diagnostics.missing_exact_outcome_episodes}
                </p>
              </div>
            )}
          </>
        )}
        <div className="rounded-md border p-3 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-medium">Latest registered report</p>
            {cohort.latest_report && statusBadge(cohort.latest_report.status)}
          </div>
          {cohort.latest_report ? (
            <div className="mt-2 space-y-1 text-muted-foreground">
              <p>
                {formatDateTime(cohort.latest_report.generated_at)} · cutoff{' '}
                {formatDateTime(cohort.latest_report.dataset_until_exclusive)}
              </p>
              <p>
                {cohort.latest_report.eligible_episodes} episodes ·{' '}
                {cohort.latest_report.asset_clusters} clusters ·{' '}
                {cohort.latest_report.calendar_weeks} weeks · verdict{' '}
                <span className="font-mono text-foreground">{cohort.latest_report.verdict}</span>
              </p>
              <p className="font-mono">
                {cohort.latest_report.code_revision.slice(0, 8)} · input{' '}
                {cohort.latest_report.decision_input_fingerprint.slice(0, 10)}
                {cohort.latest_report.working_tree_dirty && (
                  <span className="ml-2 text-amber-300">dirty tree</span>
                )}
              </p>
            </div>
          ) : (
            <p className="mt-2 text-muted-foreground">
              No successful production report has been registered yet.
            </p>
          )}
        </div>
        <p className="text-xs leading-relaxed text-muted-foreground">
          {closed
            ? 'This cohort reached formal maturity and its promotion decision is final -- see ROADMAP.md for the full writeup.'
            : 'Measurement-only observations are ignored only when both their known strategy version and persisted marker match. Unexpected strategy rows still fail closed. The registered CCXT replay can additionally exclude unavailable exact-venue paths.'}
        </p>
      </CardContent>
    </Card>
  );
}

function formatMetric(value: number | null, suffix: string, digits = 1): string {
  if (value === null || !Number.isFinite(value)) return 'n/a';
  return `${value.toFixed(digits)} ${suffix}`;
}

function formatLatency(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return 'n/a';
  if (value >= 1_000) return `${(value / 1_000).toFixed(2)} s`;
  return `${value.toFixed(0)} ms`;
}

function SourceLeadCard({ progress }: { progress: SourceLeadProgress }) {
  const identityCandidates = progress.identity_review_candidates ?? [];

  return (
    <Card>
      <CardHeader className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <GitBranch className="h-4 w-4 text-cyan-400" />
              Gate source lead
            </CardTitle>
            <p className="mt-1 font-mono text-xs text-muted-foreground">{progress.contract}</p>
          </div>
          {statusBadge(progress.status)}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>Forward cutoff {formatDateTime(progress.cohort_start)}</span>
          <span>
            Last capture{' '}
            {progress.last_observed_at ? formatDateTime(progress.last_observed_at) : 'none'}
          </span>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <MilestoneRow label="Target-eligible leads" milestone={progress.target_eligible} />
          <MilestoneRow label="Mature 4h windows" milestone={progress.mature_four_hour_windows} />
          <MilestoneRow label="Asset clusters" milestone={progress.asset_clusters} />
          <MilestoneRow label="UTC weeks" milestone={progress.calendar_weeks} />
        </div>

        <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div className="rounded-md border p-3">
            <p className="text-xs text-muted-foreground">Denominator</p>
            <p className="mt-1 font-mono">{progress.captures}</p>
          </div>
          <div className="rounded-md border p-3">
            <p className="text-xs text-muted-foreground">Source eligible</p>
            <p className="mt-1 font-mono">{progress.source_eligible}</p>
          </div>
          <div className="rounded-md border p-3">
            <p className="text-xs text-muted-foreground">Complete</p>
            <p className="mt-1 font-mono">{progress.complete}</p>
          </div>
          <div className="rounded-md border p-3">
            <p className="text-xs text-muted-foreground">Confirmed ≤1h</p>
            <p className="mt-1 font-mono">{progress.confirmed_within_hour}</p>
          </div>
        </div>

        <div className="rounded-md border p-3 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-medium">Qualified capture</p>
            <span className="font-mono text-muted-foreground">
              {progress.identity_registry_version ?? 'registry not observed'}
              {progress.identity_registry_fingerprint &&
                ` · sha256:${progress.identity_registry_fingerprint.slice(0, 12)}`}
              {progress.identity_registry_mixed && ' · mixed contract'}
            </span>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-5">
            <div>
              <p className="text-muted-foreground">Qualified</p>
              <p className="mt-1 font-mono text-foreground">{progress.qualified}</p>
            </div>
            <div>
              <p className="text-muted-foreground">Route evidence pending</p>
              <p
                className={cn(
                  'mt-1 font-mono text-foreground',
                  progress.route_evidence_pending > 0 && 'text-amber-300',
                )}
              >
                {progress.route_evidence_pending}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground">Identity unapproved</p>
              <p className="mt-1 font-mono text-foreground">{progress.identity_unapproved}</p>
            </div>
            <div>
              <p className="text-muted-foreground">No executable target</p>
              <p className="mt-1 font-mono text-foreground">
                {progress.no_approved_executable_target}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground">Missing qualification</p>
              <p
                className={cn(
                  'mt-1 font-mono text-foreground',
                  progress.qualification_missing > 0 && 'text-amber-300',
                )}
              >
                {progress.qualification_missing}
              </p>
            </div>
          </div>
          {progress.route_evidence_pending > 0 && (
            <p className="mt-3 text-muted-foreground">
              Identity and liquidity confirmed for {progress.route_evidence_pending} capture
              {progress.route_evidence_pending === 1 ? '' : 's'}, but the specific derivative
              markets are not yet independently evidenced — see each capture's own
              <code className="mx-1 rounded bg-muted px-1">would_select</code>
              detail. None of these count as qualified until that evidence exists.
            </p>
          )}
          <p className="mt-3 text-muted-foreground">
            selected Binance <span className="font-mono">{progress.selected_binance}</span> · Bybit{' '}
            <span className="font-mono">{progress.selected_bybit}</span>
          </p>
        </div>

        <div className="rounded-md border p-3 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-medium">Point-in-time identity review</p>
            <span className="text-muted-foreground">
              {identityCandidates.length} exact Gate groups
            </span>
          </div>
          {identityCandidates.length > 0 ? (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full min-w-[680px] text-left">
                <thead className="text-muted-foreground">
                  <tr className="border-b">
                    <th className="pb-2 pr-3 font-medium">Asset</th>
                    <th className="pb-2 pr-3 font-medium">Observed</th>
                    <th className="pb-2 pr-3 font-medium">Executable targets</th>
                    <th className="pb-2 pr-3 font-medium">Exact identities</th>
                    <th className="pb-2 font-medium">Raw source flag</th>
                  </tr>
                </thead>
                <tbody>
                  {identityCandidates.map((candidate) => (
                    <tr
                      key={`${candidate.base}:${candidate.source_identity_key ?? 'missing'}`}
                      className="border-b last:border-0"
                    >
                      <td className="py-2 pr-3">
                        <p className="font-mono text-foreground">{candidate.base}</p>
                        <p
                          className="max-w-56 truncate font-mono text-[10px] text-muted-foreground"
                          title={candidate.source_identity_key ?? 'missing source identity'}
                        >
                          {candidate.source_identity_key ?? 'missing source identity'}
                        </p>
                      </td>
                      <td className="py-2 pr-3 font-mono text-foreground">{candidate.captures}</td>
                      <td className="py-2 pr-3 font-mono text-foreground">
                        {candidate.executable_targets || 'none'}
                      </td>
                      <td className="py-2 pr-3 font-mono text-foreground">
                        {candidate.exact_target_identities}
                      </td>
                      <td
                        className={cn(
                          'py-2 font-mono',
                          candidate.source_conflict ? 'text-red-400' : 'text-muted-foreground',
                        )}
                      >
                        {candidate.source_conflict ? 'capture conflict' : 'none observed'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="mt-3 text-muted-foreground">
              No complete eligible source identities have been captured yet.
            </p>
          )}
          <p className="mt-3 text-muted-foreground">
            This table shows raw point-in-time observations only. It does not classify or approve
            equal tickers. The versioned Python report is the sole source of conflict state and
            still requires authoritative evidence before any strategy cohort can start.
          </p>
        </div>

        {(progress.collecting > 0 || progress.excluded > 0 || progress.abandoned > 0) && (
          <div className="rounded-md border p-3 text-xs text-muted-foreground">
            collecting <span className="font-mono text-foreground">{progress.collecting}</span> ·
            excluded <span className="font-mono text-foreground"> {progress.excluded}</span> ·
            abandoned{' '}
            <span
              className={cn(
                'font-mono',
                progress.recent_critical_abandoned > 0 && 'text-amber-300',
              )}
            >
              {progress.abandoned}
            </span>
            {progress.recent_abandoned > 0 && (
              <span className="text-muted-foreground">
                {' '}
                ({progress.recent_critical_abandoned} critical · {progress.recent_routine_abandoned}{' '}
                routine in 24h)
              </span>
            )}
            {progress.stale_collecting > 0 && (
              <>
                {' '}
                · stale <span className="font-mono text-red-400">{progress.stale_collecting}</span>
              </>
            )}
          </div>
        )}

        {progress.health_flags.length > 0 && (
          <div className="rounded-md border border-red-500/30 bg-red-500/5 p-3 text-xs">
            <p className="font-medium text-red-300">Capture health requires attention</p>
            <p className="mt-1 font-mono text-muted-foreground">
              {progress.health_flags.join(' · ')}
            </p>
          </div>
        )}

        <div className="grid gap-3 lg:grid-cols-2">
          {progress.targets.map((target) => (
            <div key={target.exchange} className="rounded-md border p-3">
              <div className="flex items-center justify-between gap-2">
                <p className="font-medium capitalize">{target.exchange}</p>
                <span className="font-mono text-xs text-muted-foreground">
                  {target.sampled}/{target.observations} sampled
                </span>
              </div>
              <div className="mt-3 grid grid-cols-3 gap-3 text-xs">
                <div>
                  <p className="text-muted-foreground">Source → quote</p>
                  <p className="mt-1 font-mono">
                    {formatLatency(target.source_to_quote_p50_ms)} p50
                  </p>
                  <p className="font-mono text-muted-foreground">
                    {formatLatency(target.source_to_quote_p90_ms)} p90
                  </p>
                </div>
                <div>
                  <p className="text-muted-foreground">Spread</p>
                  <p className="mt-1 font-mono">{formatMetric(target.spread_p50_bps, 'bps')} p50</p>
                  <p className="font-mono text-muted-foreground">
                    {formatMetric(target.spread_p90_bps, 'bps')} p90
                  </p>
                </div>
                <div>
                  <p className="text-muted-foreground">$50 entry impact</p>
                  <p className="mt-1 font-mono">
                    {formatMetric(target.entry_impact_p50_bps, 'bps')} p50
                  </p>
                  <p className="font-mono text-muted-foreground">
                    {formatMetric(target.entry_impact_p90_bps, 'bps')} p90
                  </p>
                </div>
              </div>
              {(target.fetch_failed > 0 || target.excluded > 0) && (
                <p className="mt-3 text-xs text-muted-foreground">
                  excluded {target.excluded} · fetch failed{' '}
                  <span className={cn(target.fetch_failed > 0 && 'text-amber-300')}>
                    {target.fetch_failed}
                  </span>
                </p>
              )}
            </div>
          ))}
        </div>

        <div className="rounded-md border p-3 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-medium">Latest registered report</p>
            {progress.latest_report && statusBadge(progress.latest_report.status)}
          </div>
          {progress.latest_report ? (
            <div className="mt-2 space-y-1 text-muted-foreground">
              <p>
                {formatDateTime(progress.latest_report.generated_at)} · cutoff{' '}
                {formatDateTime(progress.latest_report.dataset_until_exclusive)}
              </p>
              <p>
                {progress.latest_report.eligible_episodes} episodes ·{' '}
                {progress.latest_report.asset_clusters} clusters ·{' '}
                {progress.latest_report.calendar_weeks} weeks · verdict{' '}
                <span className="font-mono text-foreground">{progress.latest_report.verdict}</span>
              </p>
            </div>
          ) : (
            <p className="mt-2 text-muted-foreground">
              No successful production report has been registered yet.
            </p>
          )}
        </div>

        <p className="text-xs leading-relaxed text-muted-foreground">
          Raw target observations retain provisional base matching; only Qualified capture uses
          reviewed exact identity links. This card measures collection quality and capacity—not
          strategy edge.
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
          <CheckpointRunnerCard runner={data.checkpoint_runner} />

          {data.source_lead ? (
            <SourceLeadCard progress={data.source_lead} />
          ) : (
            <Card>
              <CardHeader className="space-y-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <CardTitle className="flex items-center gap-2 text-base">
                    <GitBranch className="h-4 w-4 text-cyan-400" />
                    Gate source lead
                  </CardTitle>
                  {statusBadge('telemetry unavailable')}
                </div>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground">
                  Source-lead progress is unavailable. No readiness claim is made.
                </p>
              </CardContent>
            </Card>
          )}

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
                      {data.exit_liquidity?.contract ?? 'exit_liquidity_calibration_v1'}
                    </p>
                  </div>
                  {statusBadge(data.exit_liquidity?.state ?? 'telemetry unavailable')}
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                {data.exit_liquidity ? (
                  <>
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
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    Exit quote calibration is unavailable. No readiness claim is made.
                  </p>
                )}
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
                    <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
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
                      <div>
                        <p className="text-xs text-muted-foreground">WS recovery</p>
                        <p className="font-mono">
                          {data.orderflow.trade_reconnect_total} reconnects
                        </p>
                        <p className="text-xs text-muted-foreground">
                          {data.orderflow.trade_read_timeout_total} read timeouts
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

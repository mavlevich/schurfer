import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useTokenStats } from '@/hooks/useTokenData';
import { fmtPct, pctColor } from '@/lib/formatters';

const CONFIDENCE_STYLES: Record<string, string> = {
  low: 'text-muted-foreground bg-muted border border-border',
  medium: 'text-yellow-400 bg-yellow-400/10 border border-yellow-400/20',
  high: 'text-green-400 bg-green-400/10 border border-green-400/20',
};

export function TokenStats({ base }: { base: string }) {
  const { data: stats, isPending, isError } = useTokenStats(base);

  if (isPending) {
    return <Skeleton className="h-[200px] w-full" />;
  }
  if (isError || !stats) {
    // Empty state: no stats yet
    return (
      <Card className="border-dashed">
        <CardContent className="flex items-center gap-3 py-4 text-sm text-muted-foreground">
          <span>
            No historical stats yet — data will appear once the first pump episode closes.
          </span>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
            Historical stats · {stats.episode_count} episodes
          </CardTitle>
          <span
            className={`rounded px-2 py-0.5 text-xs font-medium ${CONFIDENCE_STYLES[stats.confidence]}`}
          >
            {stats.confidence} confidence
          </span>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-6 sm:grid-cols-4">
          <div>
            <p className="text-xs text-muted-foreground mb-1">Avg 24h high</p>
            <p className={`text-lg font-mono font-bold ${pctColor(stats.avg_peak_pct)}`}>
              {fmtPct(stats.avg_peak_pct)}
            </p>
            <p className="text-xs text-muted-foreground mt-0.5">
              med {fmtPct(stats.median_peak_pct)}
            </p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground mb-1">
              Avg retrace from 24h high{' '}
              {stats.retrace_count < stats.episode_count && (
                <span className="text-yellow-500">
                  ({stats.retrace_count}/{stats.episode_count})
                </span>
              )}
            </p>
            {stats.avg_retrace_pct != null ? (
              <>
                <p className={`text-lg font-mono font-bold ${pctColor(stats.avg_retrace_pct)}`}>
                  {fmtPct(stats.avg_retrace_pct)}
                </p>
                <p className="text-xs text-muted-foreground mt-0.5">
                  med {stats.median_retrace_pct != null ? fmtPct(stats.median_retrace_pct) : '—'}
                </p>
              </>
            ) : (
              <p className="text-lg font-mono text-muted-foreground">—</p>
            )}
          </div>
          <div>
            <p className="text-xs text-muted-foreground mb-1">24h retrace range</p>
            {stats.min_retrace_pct != null && stats.max_retrace_pct != null ? (
              <>
                <p className="text-sm font-mono">
                  <span className={pctColor(stats.max_retrace_pct)}>
                    {fmtPct(stats.max_retrace_pct)}
                  </span>
                  {' → '}
                  <span className={pctColor(stats.min_retrace_pct)}>
                    {fmtPct(stats.min_retrace_pct)}
                  </span>
                </p>
                <p className="text-xs text-muted-foreground mt-0.5">best → worst</p>
              </>
            ) : (
              <p className="text-lg font-mono text-muted-foreground">—</p>
            )}
          </div>
          <div>
            <p className="text-xs text-muted-foreground mb-1">Avg duration</p>
            <p className="text-lg font-mono font-bold">{stats.avg_duration_hours.toFixed(1)}h</p>
            <p className="text-xs text-muted-foreground mt-0.5">
              med {stats.median_duration_hours.toFixed(1)}h
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

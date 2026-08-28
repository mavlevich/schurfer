import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useTokenSignals } from '@/hooks/useTokenData';
import type { SignalsResponse } from '../../types';

const VERDICT_STYLES: Record<string, { label: string; badge: string; bar: string }> = {
  pumping: {
    label: 'Pumping',
    badge: 'text-blue-400 bg-blue-400/10 border border-blue-400/20',
    bar: 'bg-blue-400',
  },
  cooling_off: {
    label: 'Cooling Off',
    badge: 'text-yellow-400 bg-yellow-400/10 border border-yellow-400/20',
    bar: 'bg-yellow-400',
  },
  short_setup: {
    label: 'Short Setup',
    badge: 'text-orange-400 bg-orange-400/10 border border-orange-400/20',
    bar: 'bg-orange-400',
  },
  prime_short: {
    label: 'Prime Short',
    badge: 'text-red-400 bg-red-400/10 border border-red-400/20',
    bar: 'bg-red-400',
  },
  insufficient_data: {
    label: 'Insufficient Data',
    badge: 'text-muted-foreground bg-muted border border-border',
    bar: 'bg-muted-foreground',
  },
};

const COMPONENT_ROWS: { key: keyof SignalsResponse['components']; label: string }[] = [
  { key: 'pump_age', label: 'Pump Age' },
  { key: 'price_extent', label: 'Price Extent' },
  { key: 'oi_trend', label: 'OI Trend' },
  { key: 'funding_rate', label: 'Funding Rate' },
  { key: 'retrace_from_peak', label: 'Retrace from 24h High' },
];

function PointsDots({ points, max }: { points: number; max: number }) {
  return (
    <span className="flex gap-0.5 justify-end">
      {Array.from({ length: max }).map((_, i) => (
        <span
          key={i}
          className={`inline-block h-2 w-2 rounded-full ${
            i < points
              ? points === max
                ? 'bg-orange-400'
                : 'bg-yellow-400'
              : 'bg-muted-foreground/30'
          }`}
        />
      ))}
    </span>
  );
}

export function TokenSignals({ base }: { base: string }) {
  const { data: signals, isPending, isError } = useTokenSignals(base);

  if (isPending) {
    return <Skeleton className="h-[280px] w-full" />;
  }
  if (isError || !signals) {
    return null;
  }

  const v = VERDICT_STYLES[signals.verdict] ?? VERDICT_STYLES['insufficient_data'];
  const scorePct = (signals.score / signals.max_score) * 100;

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
            Short Readiness
          </CardTitle>
          <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${v.badge}`}>
            {v.label}
          </span>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-baseline gap-1">
          <span className="text-3xl font-bold font-mono">{signals.score}</span>
          <span className="text-lg text-muted-foreground">/ {signals.max_score}</span>
        </div>
        <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-500 ${v.bar}`}
            style={{ width: `${scorePct}%` }}
          />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[480px] text-sm">
            <thead>
              <tr className="border-b text-xs text-muted-foreground">
                <th className="px-0 py-2 text-left font-normal">Signal</th>
                <th className="px-4 py-2 text-right font-normal">Points</th>
                <th className="px-4 py-2 text-left font-normal">Detail</th>
              </tr>
            </thead>
            <tbody>
              {COMPONENT_ROWS.map(({ key, label }) => {
                const c = signals.components[key];
                return (
                  <tr key={key} className="border-b last:border-0">
                    <td className="px-0 py-2.5 font-medium">{label}</td>
                    <td className="px-4 py-2.5">
                      <div className="flex justify-end">
                        <PointsDots points={c.points} max={c.max} />
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground text-xs">{c.note}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {(!signals.data_quality.oi || !signals.data_quality.funding) && (
          <p className="text-xs text-muted-foreground">
            ⚠ Data unavailable:{' '}
            {[!signals.data_quality.oi && 'OI', !signals.data_quality.funding && 'Funding']
              .filter(Boolean)
              .join(', ')}
            {'. Affected components defaulted to 0 pts'}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

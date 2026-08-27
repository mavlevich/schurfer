import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useTokenEpisodes } from '@/hooks/useTokenData';
import { Percent } from '@/components/ui/domain/Percent';
import { TimeFormatted } from '@/components/ui/domain/TimeFormatted';

export function TokenEpisodes({ base }: { base: string }) {
  const { data: episodes, isPending, isError } = useTokenEpisodes(base);

  if (isPending) {
    return <Skeleton className="h-[250px] w-full" />;
  }
  if (isError || !episodes || episodes.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
          Pump episodes · {episodes.length} recorded
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-b text-xs text-muted-foreground">
                <th className="px-4 py-2 text-left">#</th>
                <th className="px-4 py-2 text-left">First seen</th>
                <th className="px-4 py-2 text-left">Ended</th>
                <th className="px-4 py-2 text-right">Observed peak</th>
                <th className="px-4 py-2 text-right">24h high</th>
                <th className="px-4 py-2 text-right">24h retrace</th>
                <th className="px-4 py-2 text-center">Status</th>
              </tr>
            </thead>
            <tbody>
              {episodes.map((ep) => (
                <tr key={ep.episode} className="border-b last:border-0">
                  <td className="px-4 py-3 font-mono text-muted-foreground">{ep.episode}</td>
                  <td className="px-4 py-3 text-left">
                    <TimeFormatted value={ep.first_seen_at} />
                  </td>
                  <td className="px-4 py-3 text-left">
                    <TimeFormatted value={ep.closed_at} />
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Percent value={ep.observed_peak_pct} className="font-bold" />
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Percent value={ep.exchange_24h_high_pct} />
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Percent value={ep.retrace_pct} colorize={false} />
                  </td>
                  <td className="px-4 py-3 text-center">
                    {ep.is_live ? (
                      <span className="text-xs font-medium text-green-400">LIVE</span>
                    ) : (
                      <span className="text-xs text-muted-foreground">closed</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

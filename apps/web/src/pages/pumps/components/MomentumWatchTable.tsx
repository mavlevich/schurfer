import { RefreshCw, WifiOff } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { useMomentumWatch } from '@/hooks/usePumpsData';
import { timeAgo, fmtSignedPct, fmtUsdCompact, signedColor } from '@/lib/formatters';

export function MomentumWatchTable() {
  const { data, isError, isFetching, dataUpdatedAt } = useMomentumWatch();
  const watch = data?.watch ?? [];
  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt) : null;
  const showEmpty = !isFetching && !isError && data !== undefined && watch.length === 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Prospective longs: symbols with an active momentum_flow WATCH episode (60m price return,
          OI growth, order-flow imbalance), not a 24h % change scan.
        </p>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {isError ? (
            <>
              <WifiOff className="h-3 w-3 text-red-400" />
              <span className="text-red-400">API offline</span>
            </>
          ) : (
            <>
              <RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} />
              {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString('en-US')}` : 'Loading...'}
            </>
          )}
        </div>
      </div>

      {showEmpty && (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            No active momentum_flow WATCH episodes right now.
          </CardContent>
        </Card>
      )}

      {watch.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              {watch.length} active
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[820px] text-sm">
                <thead>
                  <tr className="border-b text-xs text-muted-foreground">
                    <th className="px-4 py-2 text-left">Symbol</th>
                    <th className="px-4 py-2 text-left">Exchange</th>
                    <th className="px-4 py-2 text-right">First watch</th>
                    <th className="px-4 py-2 text-right">Clear streak</th>
                    <th className="px-4 py-2 text-right">60m return</th>
                    <th className="px-4 py-2 text-right">15m return</th>
                    <th className="px-4 py-2 text-right">OI growth 60m</th>
                    <th className="px-4 py-2 text-right">Buy imbalance 15m</th>
                    <th className="px-4 py-2 text-right">Flow 15m</th>
                  </tr>
                </thead>
                <tbody>
                  {watch.map((e) => (
                    <tr
                      key={`${e.exchange}:${e.symbol}:${e.episode_id}`}
                      className="border-b last:border-0 hover:bg-accent/30 transition-colors"
                    >
                      <td className="px-4 py-3 font-mono font-semibold">{e.symbol}</td>
                      <td className="px-4 py-3">
                        <Badge variant="secondary" className="text-xs font-normal">
                          {e.exchange}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                        {timeAgo(e.first_watch_at)}
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                        {e.clear_streak}
                      </td>
                      <td
                        className={`px-4 py-3 text-right font-mono font-bold ${signedColor(e.price_return_60m_pct)}`}
                      >
                        {fmtSignedPct(e.price_return_60m_pct)}
                      </td>
                      <td
                        className={`px-4 py-3 text-right font-mono ${signedColor(e.price_return_15m_pct)}`}
                      >
                        {fmtSignedPct(e.price_return_15m_pct)}
                      </td>
                      <td
                        className={`px-4 py-3 text-right font-mono ${signedColor(e.oi_growth_60m_pct)}`}
                      >
                        {fmtSignedPct(e.oi_growth_60m_pct)}
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                        {e.buy_imbalance_15m === null ? '—' : e.buy_imbalance_15m.toFixed(2)}
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                        {fmtUsdCompact(e.flow_notional_15m_usd)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

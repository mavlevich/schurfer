import { Link } from 'react-router';
import { RefreshCw, WifiOff } from 'lucide-react';
import { Nav } from '@/components/Nav';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { usePumps, usePumpsHistory } from '@/hooks/usePumpsData';
import type { ExchangeEntry } from './types';
import { formatVolume, summarizeVolume, volumeRank } from './volume';

function fmtPct(n: number) {
  return `+${n.toFixed(1)}%`;
}

function pctColor(pct: number) {
  if (pct >= 100) return 'text-red-400';
  if (pct >= 50) return 'text-orange-400';
  return 'text-yellow-400';
}

function timeAgo(sec: number) {
  const diff = Math.floor(Date.now() / 1000 - sec);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function fmtPrice(n: number): string {
  if (n === 0) return '—';
  if (n >= 1000) return `$${n.toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
  if (n >= 1) return `$${n.toFixed(4)}`;
  if (n >= 0.0001) return `$${n.toFixed(6)}`;
  return `$${n.toPrecision(4)}`;
}

function topPrice(exchanges: ExchangeEntry[]): number {
  let best = 0;
  let bestVol = -1;
  for (const e of exchanges) {
    const p = parseFloat(e.price);
    const volume = volumeRank(e.volume_24h_usd);
    if (p > 0 && volume > bestVol) {
      best = p;
      bestVol = volume;
    }
  }
  return best;
}

function high24hPct(exchanges: ExchangeEntry[]): number {
  let max = 0;
  for (const e of exchanges) {
    const price = parseFloat(e.price);
    const high = parseFloat(e.high_24h);
    if (price > 0 && high > 0 && e.change_pct > -100) {
      const open = price / (1 + e.change_pct / 100);
      const pct = ((high - open) / open) * 100;
      if (pct > max) max = pct;
    }
  }
  return max;
}

export function PumpsPage() {
  const { data, isError, isFetching, dataUpdatedAt } = usePumps();
  const { data: history = [] } = usePumpsHistory();

  const pumps = data?.pumps ?? [];
  const liveSet = new Set(pumps.map((p) => p.base));
  const historical = history.filter((h) => !liveSet.has(h.base));
  const hasAny = pumps.length > 0 || historical.length > 0;
  const lastUpdated = dataUpdatedAt ? new Date(dataUpdatedAt) : null;

  // Show empty only after first successful response with empty data, never alongside error banner
  const showEmpty = !isFetching && !isError && data !== undefined && !hasAny;

  return (
    <div className="min-h-screen bg-background">
      <Nav />
      <div className="mx-auto max-w-6xl p-4 md:p-8 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold tracking-tight">Pump Scanner</h1>
            <p className="text-sm text-muted-foreground">
              Linear perps with 24h change ≥ {data?.min_change_pct ?? 30}% across all exchanges
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            {isError ? (
              <>
                <WifiOff className="h-3 w-3 text-red-400" />
                <span className="text-red-400">API offline</span>
                {lastUpdated && (
                  <span className="text-muted-foreground/60">
                    · last seen {lastUpdated.toLocaleTimeString('en-US')}
                  </span>
                )}
              </>
            ) : (
              <>
                <RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} />
                {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString('en-US')}` : 'Loading...'}
              </>
            )}
          </div>
        </div>

        {!data && !isError && <p className="text-sm text-muted-foreground">Fetching pumps...</p>}

        {showEmpty && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              No pumps above {data?.min_change_pct ?? 30}% right now.
            </CardContent>
          </Card>
        )}

        {hasAny && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
                {pumps.length > 0
                  ? `${pumps.length} active · ${historical.length} in 24h history`
                  : `${historical.length} in 24h history`}
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] text-sm">
                  <thead>
                    <tr className="border-b text-xs text-muted-foreground">
                      <th className="px-4 py-2 text-left">Token</th>
                      <th className="px-4 py-2 text-right">Observed peak</th>
                      <th className="px-4 py-2 text-right">24h high</th>
                      <th className="px-4 py-2 text-right">Now</th>
                      <th className="px-4 py-2 text-right">Price</th>
                      <th className="px-4 py-2 text-left">Exchanges</th>
                      <th className="px-4 py-2 text-right">Volume</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pumps.map((p) => {
                      const hist = history.find((h) => h.base === p.base && h.is_live);
                      const observedPeakPct = Math.max(
                        hist?.observed_peak_pct ?? 0,
                        p.max_change_pct,
                      );
                      const rollingHighPct = Math.max(
                        hist?.exchange_24h_high_pct ?? 0,
                        high24hPct(p.exchanges),
                      );
                      const volume = summarizeVolume(p.exchanges);
                      return (
                        <tr
                          key={p.base}
                          className="border-b last:border-0 hover:bg-accent/30 transition-colors"
                        >
                          <td className="px-4 py-3 font-mono font-semibold">
                            <Link
                              to={`/pumps/${p.base}`}
                              className="hover:text-primary transition-colors"
                            >
                              {p.base}
                            </Link>
                          </td>
                          <td
                            className={`px-4 py-3 text-right font-mono font-bold ${pctColor(observedPeakPct)}`}
                          >
                            {fmtPct(observedPeakPct)}
                          </td>
                          <td
                            className={`px-4 py-3 text-right font-mono ${pctColor(rollingHighPct)}`}
                          >
                            {fmtPct(rollingHighPct)}
                          </td>
                          <td
                            className={`px-4 py-3 text-right font-mono ${pctColor(p.max_change_pct)}`}
                          >
                            {fmtPct(p.max_change_pct)}
                          </td>
                          <td className="px-4 py-3 text-right font-mono text-sm">
                            {fmtPrice(topPrice(p.exchanges))}
                          </td>
                          <td className="px-4 py-3">
                            <div className="flex flex-wrap gap-1">
                              {p.exchanges.map((e) => (
                                <Badge
                                  key={e.exchange}
                                  variant="secondary"
                                  className="text-xs font-normal"
                                >
                                  {e.exchange} {fmtPct(e.change_pct)}
                                </Badge>
                              ))}
                            </div>
                          </td>
                          <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                            {formatVolume(volume)}
                          </td>
                        </tr>
                      );
                    })}

                    {historical.map((h) => {
                      const volume = summarizeVolume(h.exchanges);
                      return (
                        <tr
                          key={h.base}
                          className="border-b last:border-0 opacity-50 hover:opacity-80 transition-opacity"
                        >
                          <td className="px-4 py-3 font-mono font-semibold">
                            <Link
                              to={`/pumps/${h.base}`}
                              className="hover:text-primary transition-colors"
                            >
                              {h.base}
                            </Link>
                            <div className="text-xs text-muted-foreground font-normal leading-tight">
                              on radar {timeAgo(h.first_seen_at)} · last seen{' '}
                              {timeAgo(h.last_seen_at)}
                            </div>
                          </td>
                          <td
                            className={`px-4 py-3 text-right font-mono font-bold ${pctColor(h.observed_peak_pct)}`}
                          >
                            {fmtPct(h.observed_peak_pct)}
                          </td>
                          <td
                            className={`px-4 py-3 text-right font-mono ${pctColor(h.exchange_24h_high_pct)}`}
                          >
                            {fmtPct(h.exchange_24h_high_pct)}
                          </td>
                          <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                            {fmtPct(h.last_pct)}
                          </td>
                          <td className="px-4 py-3 text-right font-mono text-sm text-muted-foreground">
                            {fmtPrice(topPrice(h.exchanges))}
                          </td>
                          <td className="px-4 py-3">
                            <div className="flex flex-wrap gap-1">
                              {h.exchanges.map((e) => (
                                <Badge
                                  key={e.exchange}
                                  variant="outline"
                                  className="text-xs font-normal opacity-60"
                                >
                                  {e.exchange}
                                </Badge>
                              ))}
                            </div>
                          </td>
                          <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                            {formatVolume(volume)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

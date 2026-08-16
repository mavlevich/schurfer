import { useState } from 'react';
import { Link } from 'react-router';
import { RefreshCw, WifiOff } from 'lucide-react';
import { PageShell } from '@/components/shared/PageShell';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { usePumps, usePumpsHistory, useMomentumWatch } from '@/hooks/usePumpsData';
import type { ExchangeEntry } from './types';
import { formatVolume, summarizeVolume, volumeRank } from './volume';

type ScannerTab = 'pump' | 'momentum_watch';

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

// fmtSignedPct is used for momentum_flow WATCH features, which (unlike the
// pump scanner's own always-positive % change) can legitimately be negative
// (e.g. price_return_15m_pct pulling back inside an otherwise-active episode).
function fmtSignedPct(n: number | null): string {
  if (n === null) return '—';
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
}

function fmtUsdCompact(n: number | null): string {
  if (n === null) return '—';
  const sign = n < 0 ? '-' : '';
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}k`;
  return `${sign}$${abs.toFixed(0)}`;
}

function signedColor(n: number | null): string {
  if (n === null) return 'text-muted-foreground';
  return n >= 0 ? 'text-green-400' : 'text-red-400';
}

// MomentumWatchTable is the prospective-long counterpart of the pump-scanner
// table above, but built from a completely different signal (momentum_flow's
// own 60m price return / OI growth / order-flow imbalance, not 24h % change).
// Deliberately its own table rather than merged rows in the pump table --
// see useMomentumWatch and the backend's momentumWatchQuery for why these
// stay two separate reads instead of one row-shaped union.
function MomentumWatchTable() {
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

// ScannerTabButton mirrors the active/inactive treatment used by Nav's own
// NavLink, so switching surfaces within the Scanner page feels consistent
// with switching between pages.
function ScannerTabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded px-3 py-1.5 text-sm transition-colors ${
        active ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:text-foreground'
      }`}
    >
      {children}
    </button>
  );
}

export function PumpsPage() {
  const [tab, setTab] = useState<ScannerTab>('pump');
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
    <PageShell width="wide" className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Scanner</h1>
          <p className="text-sm text-muted-foreground">
            {tab === 'pump'
              ? `Linear perps with 24h change ≥ ${data?.min_change_pct ?? 30}% across all exchanges`
              : 'Symbols with an active momentum_flow WATCH episode (prospective longs)'}
          </p>
        </div>
        {tab === 'pump' && (
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
        )}
      </div>

      <div className="flex items-center gap-1 border-b pb-2">
        <ScannerTabButton active={tab === 'pump'} onClick={() => setTab('pump')}>
          Pump Scanner
        </ScannerTabButton>
        <ScannerTabButton
          active={tab === 'momentum_watch'}
          onClick={() => setTab('momentum_watch')}
        >
          Momentum Flow (long)
        </ScannerTabButton>
      </div>

      {tab === 'momentum_watch' && <MomentumWatchTable />}

      {tab === 'pump' && (
        <>
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
        </>
      )}
    </PageShell>
  );
}

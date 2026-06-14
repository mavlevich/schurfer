import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { RefreshCw, WifiOff } from 'lucide-react';
import { Nav } from '@/components/Nav';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';

interface ExchangeEntry {
  exchange: string;
  symbol: string;
  price: string;
  change_pct: number;
  high_24h: string;
  volume_24h_usd: number;
}

interface PumpEntry {
  base: string;
  max_change_pct: number;
  exchanges: ExchangeEntry[];
}

interface PumpsResponse {
  ts: number;
  count: number;
  min_change_pct: number | null;
  pumps: PumpEntry[];
  errors?: Record<string, string>;
  scanned?: string[];
}

interface HistoryEntry {
  base: string;
  first_seen_at: number;
  last_seen_at: number;
  peak_pct: number;
  last_pct: number;
  is_live: boolean;
  exchanges: ExchangeEntry[];
}

function fmtPct(n: number) {
  return `+${n.toFixed(1)}%`;
}

function fmtVol(n: number) {
  if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(0)}M`;
  return `$${n.toFixed(0)}`;
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
  const [data, setData] = useState<PumpsResponse | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [offline, setOffline] = useState(false);
  // true only on the very first load — suppresses "No pumps" until we have a real response
  const initialized = useRef(false);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const load = async () => {
    try {
      const [liveRes, histRes] = await Promise.all([
        window.fetch('/api/pumps'),
        window.fetch('/api/pumps/history'),
      ]);
      if (liveRes.ok) setData((await liveRes.json()) as PumpsResponse);
      if (histRes.ok) setHistory((await histRes.json()) as HistoryEntry[]);
      setLastUpdated(new Date());
      setOffline(false);
    } catch {
      setOffline(true);
    } finally {
      initialized.current = true;
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), 60_000);
    return () => clearInterval(id);
  }, []);

  const pumps = data?.pumps ?? [];
  const liveSet = new Set(pumps.map((p) => p.base));
  const historical = history.filter((h) => !liveSet.has(h.base));
  const hasAny = pumps.length > 0 || historical.length > 0;

  // "No pumps" only shows after first successful response with actually empty data
  const showEmpty = !loading && initialized.current && data !== null && !hasAny;

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
            {offline ? (
              <>
                <WifiOff className="h-3 w-3 text-red-400" />
                <span className="text-red-400">API offline</span>
                {lastUpdated && (
                  <span className="text-muted-foreground/60">
                    · last seen {lastUpdated.toLocaleTimeString()}
                  </span>
                )}
              </>
            ) : (
              <>
                <RefreshCw className="h-3 w-3" />
                {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : 'Loading...'}
              </>
            )}
          </div>
        </div>

        {loading && <p className="text-sm text-muted-foreground">Fetching pumps...</p>}

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
                <table className="w-full min-w-[640px] text-sm">
                  <thead>
                    <tr className="border-b text-xs text-muted-foreground">
                      <th className="px-4 py-2 text-left">Token</th>
                      <th className="px-4 py-2 text-right">Peak 24h</th>
                      <th className="px-4 py-2 text-right">Now</th>
                      <th className="px-4 py-2 text-left">Exchanges</th>
                      <th className="px-4 py-2 text-right">Volume</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pumps.map((p) => {
                      const hist = history.find((h) => h.base === p.base);
                      const peakPct = Math.max(
                        hist?.peak_pct ?? 0,
                        p.max_change_pct,
                        high24hPct(p.exchanges),
                      );
                      const totalVol = p.exchanges.reduce((s, e) => s + e.volume_24h_usd, 0);
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
                            className={`px-4 py-3 text-right font-mono font-bold ${pctColor(peakPct)}`}
                          >
                            {fmtPct(peakPct)}
                          </td>
                          <td
                            className={`px-4 py-3 text-right font-mono ${pctColor(p.max_change_pct)}`}
                          >
                            {fmtPct(p.max_change_pct)}
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
                            {fmtVol(totalVol)}
                          </td>
                        </tr>
                      );
                    })}

                    {historical.map((h) => {
                      const totalVol = h.exchanges.reduce((s, e) => s + e.volume_24h_usd, 0);
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
                            className={`px-4 py-3 text-right font-mono font-bold ${pctColor(h.peak_pct)}`}
                          >
                            {fmtPct(h.peak_pct)}
                          </td>
                          <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                            {fmtPct(h.last_pct)}
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
                            {fmtVol(totalVol)}
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

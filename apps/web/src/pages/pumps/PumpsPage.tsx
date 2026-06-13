import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
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

export function PumpsPage() {
  const [data, setData] = useState<PumpsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const fetch = async () => {
    try {
      const res = await window.fetch('/api/pumps');
      if (res.ok) {
        setData((await res.json()) as PumpsResponse);
        setLastUpdated(new Date());
      }
    } catch {
      // api-gateway not reachable
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void fetch();
    const id = setInterval(() => void fetch(), 60_000);
    return () => clearInterval(id);
  }, []);

  const pumps = data?.pumps ?? [];

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
            <RefreshCw className="h-3 w-3" />
            {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : 'Loading...'}
          </div>
        </div>

        {loading && <p className="text-sm text-muted-foreground">Fetching pumps...</p>}

        {!loading && pumps.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              No pumps found — scanner may still be warming up.
            </CardContent>
          </Card>
        )}

        {pumps.length > 0 && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
                {pumps.length} tokens pumping
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[600px] text-sm">
                  <thead>
                    <tr className="border-b text-xs text-muted-foreground">
                      <th className="px-4 py-2 text-left">Token</th>
                      <th className="px-4 py-2 text-right">Max 24h</th>
                      <th className="px-4 py-2 text-left">Exchanges</th>
                      <th className="px-4 py-2 text-right">Best price</th>
                      <th className="px-4 py-2 text-right">Volume</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pumps.map((p) => {
                      const best = p.exchanges[0];
                      const totalVol = p.exchanges.reduce((s, e) => s + e.volume_24h_usd, 0);
                      return (
                        <tr
                          key={p.base}
                          className="border-b last:border-0 hover:bg-accent/30 transition-colors"
                        >
                          <td className="px-4 py-3 font-mono font-semibold">{p.base}</td>
                          <td
                            className={`px-4 py-3 text-right font-mono font-bold ${pctColor(p.max_change_pct)}`}
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
                            ${best?.price ?? '—'}
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

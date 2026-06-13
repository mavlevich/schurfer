import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import { CandlestickSeries, ColorType, createChart } from 'lightweight-charts';
import type { UTCTimestamp } from 'lightweight-charts';
import { Nav } from '@/components/Nav';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

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

interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface OHLCVResponse {
  base: string;
  exchange: string;
  interval: number;
  candles: Candle[];
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

export function TokenPage() {
  const { base } = useParams<{ base: string }>();
  const [pump, setPump] = useState<PumpEntry | null>(null);
  const [ohlcv, setOHLCV] = useState<OHLCVResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const chartContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!base) return;

    setPump(null);
    setOHLCV(null);
    setLoading(true);

    const controller = new AbortController();
    const encoded = encodeURIComponent(base);

    const load = async () => {
      try {
        const [pumpRes, ohlcvRes] = await Promise.all([
          window.fetch(`/api/pumps/${encoded}`, { signal: controller.signal }),
          window.fetch(`/api/pumps/${encoded}/ohlcv?interval=60&limit=200`, {
            signal: controller.signal,
          }),
        ]);
        if (pumpRes.ok) setPump((await pumpRes.json()) as PumpEntry);
        if (ohlcvRes.ok) setOHLCV((await ohlcvRes.json()) as OHLCVResponse);
      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') return;
      } finally {
        setLoading(false);
      }
    };

    void load();
    return () => controller.abort();
  }, [base]);

  useEffect(() => {
    const container = chartContainerRef.current;
    if (!container || !ohlcv?.candles.length) return;

    const chart = createChart(container, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#9ca3af',
      },
      grid: {
        vertLines: { color: '#1f293780' },
        horzLines: { color: '#1f293780' },
      },
      autoSize: true,
      height: 380,
      timeScale: { timeVisible: true, secondsVisible: false },
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderVisible: false,
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    });

    series.setData(
      ohlcv.candles.map((c) => ({
        time: c.time as UTCTimestamp,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    );
    chart.timeScale().fitContent();

    return () => {
      chart.remove();
    };
  }, [ohlcv]);

  return (
    <div className="min-h-screen bg-background">
      <Nav />
      <div className="mx-auto max-w-6xl p-4 md:p-8 space-y-4">
        <Link
          to="/pumps"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          <ArrowLeft className="h-3 w-3" />
          Pump Scanner
        </Link>

        {loading && <p className="text-sm text-muted-foreground">Loading...</p>}

        {!loading && !pump && !ohlcv && (
          <p className="text-sm text-muted-foreground">Token not found.</p>
        )}

        {(pump ?? ohlcv) && (
          <div>
            <h1 className="text-2xl font-bold font-mono tracking-tight">
              {base}
              {pump && (
                <span className={`ml-3 text-xl ${pctColor(pump.max_change_pct)}`}>
                  {fmtPct(pump.max_change_pct)}
                </span>
              )}
            </h1>
            <p className="text-sm text-muted-foreground mt-1">
              {pump
                ? `Active on ${pump.exchanges.length} exchange${pump.exchanges.length !== 1 ? 's' : ''}`
                : 'No longer in pump list'}
              {ohlcv && ` · 1h chart via ${ohlcv.exchange}`}
            </p>
          </div>
        )}

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
              Price chart · 1h candles
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0 pb-2">
            {ohlcv?.candles.length ? (
              <div ref={chartContainerRef} className="h-[380px] w-full" />
            ) : (
              !loading && (
                <p className="py-12 text-center text-sm text-muted-foreground">Chart unavailable</p>
              )
            )}
          </CardContent>
        </Card>

        {pump && pump.exchanges.length > 0 && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
                Exchange breakdown
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[480px] text-sm">
                  <thead>
                    <tr className="border-b text-xs text-muted-foreground">
                      <th className="px-4 py-2 text-left">Exchange</th>
                      <th className="px-4 py-2 text-right">24h %</th>
                      <th className="px-4 py-2 text-right">Price</th>
                      <th className="px-4 py-2 text-right">24h High</th>
                      <th className="px-4 py-2 text-right">Volume</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pump.exchanges.map((e) => (
                      <tr key={e.exchange} className="border-b last:border-0">
                        <td className="px-4 py-3 font-medium capitalize">{e.exchange}</td>
                        <td
                          className={`px-4 py-3 text-right font-mono font-bold ${pctColor(e.change_pct)}`}
                        >
                          {fmtPct(e.change_pct)}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                          ${e.price}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                          ${e.high_24h}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                          {fmtVol(e.volume_24h_usd)}
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
    </div>
  );
}

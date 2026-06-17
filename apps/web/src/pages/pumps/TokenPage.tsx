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

interface TokenEpisode {
  base: string;
  episode: number;
  first_seen_at: number;
  last_seen_at: number;
  closed_at: number | null;
  peak_pct: number;
  last_pct: number;
  retrace_pct: number | null;
  is_live: boolean;
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

interface SignalComponent {
  value: number;
  points: number;
  max: number;
  note: string;
}

interface SignalsResponse {
  base: string;
  verdict: string;
  score: number;
  max_score: number;
  episode: {
    id: number;
    first_seen_at: number;
    age_hours: number;
    peak_pct: number;
    last_pct: number;
    is_open: boolean;
  };
  components: {
    pump_age: SignalComponent;
    price_extent: SignalComponent;
    oi_trend: SignalComponent;
    funding_rate: SignalComponent;
    retrace_from_peak: SignalComponent;
  };
  data_quality: {
    oi: boolean;
    funding: boolean;
  };
}

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
  { key: 'retrace_from_peak', label: 'Retrace from Peak' },
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

function fmtPct(n: number) {
  return `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`;
}

function fmtTs(unix: number) {
  return new Date(unix * 1000).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
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

const INTERVALS: { label: string; range: string; minutes: number; limit: number }[] = [
  { label: '5m', range: 'last 24h', minutes: 5, limit: 288 },
  { label: '15m', range: 'last 48h', minutes: 15, limit: 192 },
  { label: '1h', range: 'last 8d', minutes: 60, limit: 200 },
  { label: '4h', range: 'last 30d', minutes: 240, limit: 180 },
];

function fmtPrice(s: string): string {
  const n = parseFloat(s);
  if (!isFinite(n) || s === '') return s;
  if (n >= 1000) return n.toFixed(2);
  if (n >= 0.01) return n.toFixed(4);
  // Tiny prices: 4 significant digits, no scientific notation
  const exp = Math.floor(Math.log10(n));
  return n.toFixed(Math.min(-exp + 3, 10));
}

export function TokenPage() {
  const { base } = useParams<{ base: string }>();
  const [pump, setPump] = useState<PumpEntry | null>(null);
  const [ohlcv, setOHLCV] = useState<OHLCVResponse | null>(null);
  const [episodes, setEpisodes] = useState<TokenEpisode[]>([]);
  const [signals, setSignals] = useState<SignalsResponse | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(true);
  const [chartLoading, setChartLoading] = useState(true);
  const [chartInterval, setChartInterval] = useState(15);
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const selectedInterval = INTERVALS.find((i) => i.minutes === chartInterval) ?? INTERVALS[1];

  useEffect(() => {
    if (!base) return;

    setPump(null);
    setEpisodes([]);
    setSignals(null);
    setDetailsLoading(true);

    const controller = new AbortController();
    const encoded = encodeURIComponent(base);

    const loadDetails = async () => {
      try {
        const [pumpRes, historyRes, signalsRes] = await Promise.all([
          window.fetch(`/api/pumps/${encoded}`, { signal: controller.signal }),
          window.fetch(`/api/pumps/${encoded}/history`, { signal: controller.signal }),
          window.fetch(`/api/pumps/${encoded}/signals`, { signal: controller.signal }),
        ]);
        if (pumpRes.ok) setPump((await pumpRes.json()) as PumpEntry);
        if (historyRes.ok) setEpisodes((await historyRes.json()) as TokenEpisode[]);
        // 404 = no open episode — card simply won't render
        if (signalsRes.ok) setSignals((await signalsRes.json()) as SignalsResponse);
      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') return;
      } finally {
        setDetailsLoading(false);
      }
    };

    void loadDetails();
    return () => controller.abort();
  }, [base]);

  useEffect(() => {
    if (!base) return;

    setOHLCV(null);
    setChartLoading(true);

    const controller = new AbortController();
    const encoded = encodeURIComponent(base);

    const loadChart = async () => {
      try {
        const res = await window.fetch(
          `/api/pumps/${encoded}/ohlcv?interval=${selectedInterval.minutes}&limit=${selectedInterval.limit}`,
          { signal: controller.signal },
        );
        if (res.ok) setOHLCV((await res.json()) as OHLCVResponse);
      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') return;
      } finally {
        setChartLoading(false);
      }
    };

    void loadChart();
    return () => controller.abort();
  }, [base, selectedInterval.limit, selectedInterval.minutes]);

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
      localization: { locale: 'en-US' },
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

        {detailsLoading && <p className="text-sm text-muted-foreground">Loading...</p>}

        {!detailsLoading && !chartLoading && !pump && !ohlcv && episodes.length === 0 && (
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
              {ohlcv && ` · ${selectedInterval.label} chart via ${ohlcv.exchange}`}
            </p>
          </div>
        )}

        <Card>
          <CardHeader className="pb-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
                Price chart
                {ohlcv && ` · ${ohlcv.exchange}`}
                {` · ${selectedInterval.label} · ${selectedInterval.range}`}
              </CardTitle>
              <div className="flex gap-1">
                {INTERVALS.map((iv) => (
                  <button
                    key={iv.minutes}
                    type="button"
                    onClick={() => setChartInterval(iv.minutes)}
                    className={`px-2 py-0.5 text-xs rounded font-mono transition-colors ${
                      chartInterval === iv.minutes
                        ? 'bg-primary text-primary-foreground'
                        : 'text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {iv.label}
                  </button>
                ))}
              </div>
            </div>
          </CardHeader>
          <CardContent className="p-0 pb-2">
            <div className="relative h-[380px] w-full">
              {chartLoading ? (
                <p className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground animate-pulse">
                  Loading chart...
                </p>
              ) : ohlcv?.candles.length ? (
                <div ref={chartContainerRef} className="absolute inset-0" />
              ) : (
                <p className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
                  Chart unavailable
                </p>
              )}
            </div>
          </CardContent>
        </Card>

        {signals &&
          (() => {
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
                              <td className="px-4 py-2.5 text-muted-foreground text-xs">
                                {c.note}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  {(!signals.data_quality.oi || !signals.data_quality.funding) && (
                    <p className="text-xs text-muted-foreground">
                      ⚠ Data unavailable:{' '}
                      {[
                        !signals.data_quality.oi && 'OI',
                        !signals.data_quality.funding && 'Funding',
                      ]
                        .filter(Boolean)
                        .join(', ')}
                      {' — affected components defaulted to 0 pts'}
                    </p>
                  )}
                </CardContent>
              </Card>
            );
          })()}

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
                          ${fmtPrice(e.price)}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                          ${fmtPrice(e.high_24h)}
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

        {episodes.length > 0 && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
                Pump episodes · {episodes.length} recorded
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px] text-sm">
                  <thead>
                    <tr className="border-b text-xs text-muted-foreground">
                      <th className="px-4 py-2 text-left">#</th>
                      <th className="px-4 py-2 text-left">First seen</th>
                      <th className="px-4 py-2 text-left">Ended</th>
                      <th className="px-4 py-2 text-right">Peak</th>
                      <th className="px-4 py-2 text-right">Retrace</th>
                      <th className="px-4 py-2 text-center">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {episodes.map((ep) => (
                      <tr key={ep.episode} className="border-b last:border-0">
                        <td className="px-4 py-3 font-mono text-muted-foreground">{ep.episode}</td>
                        <td className="px-4 py-3 text-muted-foreground">
                          {fmtTs(ep.first_seen_at)}
                        </td>
                        <td className="px-4 py-3 text-muted-foreground">
                          {ep.closed_at ? fmtTs(ep.closed_at) : '—'}
                        </td>
                        <td
                          className={`px-4 py-3 text-right font-mono font-bold ${pctColor(ep.peak_pct)}`}
                        >
                          {fmtPct(ep.peak_pct)}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                          {ep.retrace_pct != null ? fmtPct(ep.retrace_pct) : '—'}
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
        )}
      </div>
    </div>
  );
}

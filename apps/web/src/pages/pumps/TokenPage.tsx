import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import { CandlestickSeries, ColorType, createChart } from 'lightweight-charts';
import type { UTCTimestamp } from 'lightweight-charts';
import { Nav } from '@/components/Nav';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useToken, useTokenEpisodes, useTokenSignals, useTokenStats } from '@/hooks/useTokenData';
import { useOHLCV, INTERVALS, getInterval } from '@/hooks/useOHLCV';
import type { OHLCVResponse, SignalsResponse, TokenStats, TokenEpisode } from './types';
import { formatVolume } from './volume';

const CONFIDENCE_STYLES: Record<string, string> = {
  low: 'text-muted-foreground bg-muted border border-border',
  medium: 'text-yellow-400 bg-yellow-400/10 border border-yellow-400/20',
  high: 'text-green-400 bg-green-400/10 border border-green-400/20',
};

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

function pctColor(pct: number) {
  if (pct >= 100) return 'text-red-400';
  if (pct >= 50) return 'text-orange-400';
  return 'text-yellow-400';
}

function fmtPrice(s: string): string {
  const n = parseFloat(s);
  if (!isFinite(n) || n <= 0 || s === '') return 'n/a';
  if (n >= 1000) return `$${n.toFixed(2)}`;
  if (n >= 0.01) return `$${n.toFixed(4)}`;
  const exp = Math.floor(Math.log10(n));
  return `$${n.toFixed(Math.min(-exp + 3, 10))}`;
}

function SignalsCard({ signals }: { signals: SignalsResponse }) {
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

function StatsCard({ stats }: { stats: TokenStats }) {
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

function EpisodesCard({ episodes }: { episodes: TokenEpisode[] }) {
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
                  <td className="px-4 py-3 text-muted-foreground">{fmtTs(ep.first_seen_at)}</td>
                  <td className="px-4 py-3 text-muted-foreground">
                    {ep.closed_at ? fmtTs(ep.closed_at) : '—'}
                  </td>
                  <td
                    className={`px-4 py-3 text-right font-mono font-bold ${pctColor(ep.observed_peak_pct)}`}
                  >
                    {fmtPct(ep.observed_peak_pct)}
                  </td>
                  <td
                    className={`px-4 py-3 text-right font-mono ${pctColor(ep.exchange_24h_high_pct)}`}
                  >
                    {fmtPct(ep.exchange_24h_high_pct)}
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
  );
}

function PriceChart({
  ohlcv,
  isFetching,
  chartInterval,
  onIntervalChange,
}: {
  ohlcv: OHLCVResponse | undefined;
  isFetching: boolean;
  chartInterval: number;
  onIntervalChange: (minutes: number) => void;
}) {
  type ChartApi = ReturnType<typeof createChart>;
  type SeriesApi = ReturnType<ChartApi['addSeries']>;

  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ChartApi | null>(null);
  const seriesRef = useRef<SeriesApi | null>(null);
  const selectedInterval = getInterval(chartInterval);

  // Create chart and series once on mount; destroy on unmount.
  useEffect(() => {
    const container = chartContainerRef.current;
    if (!container) return;

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

    chartRef.current = chart;
    seriesRef.current = series;

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Update data without recreating the chart — preserves user pan/zoom.
  useEffect(() => {
    if (!seriesRef.current || !ohlcv?.candles.length) return;

    const minPrice = Math.min(...ohlcv.candles.map((c) => c.low));
    const priceFormat =
      minPrice >= 100
        ? { precision: 2, minMove: 0.01 }
        : minPrice >= 1
          ? { precision: 4, minMove: 0.0001 }
          : minPrice >= 0.01
            ? { precision: 6, minMove: 0.000001 }
            : { precision: 8, minMove: 0.00000001 };

    seriesRef.current.applyOptions({ priceFormat });
    seriesRef.current.setData(
      ohlcv.candles.map((c) => ({
        time: c.time as UTCTimestamp,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [ohlcv]);

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
            Price chart
            {ohlcv && ` · ${ohlcv.exchange}`}
            {` · ${selectedInterval.label} · ${selectedInterval.range}`}
            {isFetching && <span className="ml-1 opacity-40">↻</span>}
          </CardTitle>
          <div className="flex gap-1">
            {INTERVALS.map((iv) => (
              <button
                key={iv.minutes}
                type="button"
                onClick={() => onIntervalChange(iv.minutes)}
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
        {/* Container is always mounted so lightweight-charts never unmounts mid-render */}
        <div className="relative h-[380px] w-full">
          <div ref={chartContainerRef} className="absolute inset-0" />
          {isFetching && !ohlcv && (
            <p className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground animate-pulse">
              Loading chart...
            </p>
          )}
          {!isFetching && !ohlcv?.candles.length && (
            <p className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
              Chart unavailable
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export function TokenPage() {
  const { base } = useParams<{ base: string }>();
  const [chartInterval, setChartInterval] = useState(15);

  const { data: pump, isPending: pumpPending, isError: pumpError } = useToken(base);
  const {
    data: episodes = [],
    isPending: episodesPending,
    isError: episodesError,
  } = useTokenEpisodes(base);
  const { data: signals } = useTokenSignals(base);
  const { data: stats } = useTokenStats(base);
  const { data: ohlcv, isFetching: chartFetching } = useOHLCV(base, chartInterval);

  const detailsLoading = pumpPending || episodesPending;
  const detailsError = pumpError || episodesError;
  const hasDetails = pump != null || episodes.length > 0;
  const notFound = !detailsLoading && !detailsError && !hasDetails;
  const detailsUnavailable = !detailsLoading && detailsError && !hasDetails;

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
        {notFound && <p className="text-sm text-muted-foreground">Token not found.</p>}
        {detailsUnavailable && (
          <p className="text-sm text-red-400">Unable to load token details. Please retry.</p>
        )}
        {detailsError && hasDetails && (
          <p className="text-sm text-yellow-400">Some token details are temporarily unavailable.</p>
        )}

        {!notFound && !detailsUnavailable && (
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
              {ohlcv && ` · ${getInterval(chartInterval).label} chart via ${ohlcv.exchange}`}
            </p>
          </div>
        )}

        {!notFound && !detailsUnavailable && (
          <PriceChart
            ohlcv={ohlcv}
            isFetching={chartFetching}
            chartInterval={chartInterval}
            onIntervalChange={setChartInterval}
          />
        )}

        {signals && <SignalsCard signals={signals} />}

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
                          {fmtPrice(e.price)}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                          {fmtPrice(e.high_24h)}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-muted-foreground">
                          {formatVolume({
                            value: e.volume_24h_usd,
                            partial: false,
                          })}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        )}

        {episodes.length > 0 && <EpisodesCard episodes={episodes} />}

        {stats && <StatsCard stats={stats} />}

        {!detailsLoading && pump && !stats && (
          <Card className="border-dashed">
            <CardContent className="flex items-center gap-3 py-4 text-sm text-muted-foreground">
              <span>
                No historical stats yet — data will appear once the first pump episode closes.
              </span>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

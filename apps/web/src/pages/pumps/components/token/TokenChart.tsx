import { useEffect, useMemo, useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  type SeriesMarker,
  type UTCTimestamp,
} from 'lightweight-charts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useOHLCV, INTERVALS, getInterval } from '@/hooks/useOHLCV';
import { useTokenEpisodes } from '@/hooks/useTokenData';
import { useDecisionBuckets } from '@/hooks/useDecisionBuckets';
import {
  ALL_MARKERS_VISIBLE,
  alignBuckets,
  buildChartMarkers,
  describeBucket,
  type DecisionBucket,
  type MarkerVisibility,
} from '../../decisionMarkers';

interface HoverState {
  bucket: DecisionBucket;
  x: number;
  y: number;
}

export function TokenChart({ base }: { base: string }) {
  const [chartInterval, setChartInterval] = useState(15);
  const [visibility, setVisibility] = useState<MarkerVisibility>(ALL_MARKERS_VISIBLE);
  const [hover, setHover] = useState<HoverState | null>(null);
  const { data: ohlcv, isFetching } = useOHLCV(base, chartInterval);
  const { data: episodes } = useTokenEpisodes(base);

  const candleTimes = useMemo(() => (ohlcv?.candles ?? []).map((candle) => candle.time), [ohlcv]);
  // The window the chart is actually drawing, so the request covers what is on
  // screen rather than the most recent page of everything. `until` is exclusive
  // and the last candle is still open, so it reaches past it by one interval.
  const sinceSeconds = candleTimes.length ? candleTimes[0] : undefined;
  const untilSeconds = candleTimes.length
    ? candleTimes[candleTimes.length - 1] + chartInterval * 60
    : undefined;

  // What the system decided about this token, and why. The reason is the point:
  // a chart that shows only where price moved cannot say why nothing was opened
  // there.
  const { data: bucketsData } = useDecisionBuckets({
    base,
    intervalMinutes: chartInterval,
    sinceSeconds,
    untilSeconds,
  });

  type ChartApi = ReturnType<typeof createChart>;
  type SeriesApi = ReturnType<ChartApi['addSeries']>;

  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ChartApi | null>(null);
  const seriesRef = useRef<SeriesApi | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const markersRef = useRef<any>(null);
  const selectedInterval = getInterval(chartInterval);

  const alignedBuckets = useMemo(
    () => alignBuckets(bucketsData?.buckets ?? [], candleTimes),
    [bucketsData, candleTimes],
  );
  // Every candle's bucket, not only the ones that got a marker: the marker says
  // where something changed, and the tooltip answers for whatever is hovered.
  const bucketByTime = useMemo(() => {
    const map = new Map<number, DecisionBucket>();
    for (const bucket of alignedBuckets) map.set(bucket.time, bucket);
    return map;
  }, [alignedBuckets]);
  const bucketByTimeRef = useRef(bucketByTime);
  bucketByTimeRef.current = bucketByTime;

  const markers = useMemo(
    () => buildChartMarkers({ episodes, buckets: bucketsData?.buckets, candleTimes, visibility }),
    [episodes, bucketsData, candleTimes, visibility],
  );

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
    markersRef.current = createSeriesMarkers(series);

    // The tooltip lightweight-charts does not provide. `SeriesMarker` has no
    // `title` field in v5, so the previous version computed a description for
    // every marker and threw it away -- hidden by an `as unknown as` cast that
    // silenced the type error saying so.
    chart.subscribeCrosshairMove((param) => {
      if (param.time === undefined || !param.point) {
        setHover(null);
        return;
      }
      const bucket = bucketByTimeRef.current.get(param.time as number);
      if (!bucket) {
        setHover(null);
        return;
      }
      setHover({ bucket, x: param.point.x, y: param.point.y });
    });

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Update data without recreating the chart — preserves user pan/zoom.
  useEffect(() => {
    if (!seriesRef.current) return;
    if (!ohlcv || !ohlcv.candles.length) {
      seriesRef.current.setData([]);
      if (markersRef.current) markersRef.current.setMarkers([]);
      return;
    }

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

  // Markers are applied separately from the candles, and unconditionally,
  // including when empty: switching to a token with nothing to show must clear
  // the previous token's markers rather than leave them floating over the new
  // candles (colleague review). Toggling the legend must not refit the time
  // scale, which is why this is not folded into the effect above.
  useEffect(() => {
    if (!markersRef.current) return;
    markersRef.current.setMarkers(markers as SeriesMarker<UTCTimestamp>[]);
  }, [markers]);

  const evaluated = alignedBuckets.reduce((sum, bucket) => sum + bucket.count, 0);

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
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-1 text-xs text-muted-foreground">
          <MarkerToggle
            label="Pump starts"
            swatch="#facc15"
            checked={visibility.episodes}
            onChange={(next) => setVisibility((v) => ({ ...v, episodes: next }))}
          />
          <MarkerToggle
            label="Opened"
            swatch="#34d399"
            checked={visibility.opened}
            onChange={(next) => setVisibility((v) => ({ ...v, opened: next }))}
          />
          <MarkerToggle
            label="Skip reason changed"
            swatch="#64748b"
            checked={visibility.skipped}
            onChange={(next) => setVisibility((v) => ({ ...v, skipped: next }))}
          />
          {/* Coverage, stated rather than implied. A marker per candle would be
              unreadable, so most candles have none; without this line an empty
              stretch reads as "nothing was evaluated" when it means "nothing
              changed". */}
          {bucketsData && (
            <span className="font-mono">
              {evaluated.toLocaleString()} evaluations in {alignedBuckets.length} candles
              {bucketsData.truncated && (
                <span className="text-amber-500"> · truncated, older candles not shown</span>
              )}
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent className="p-0 pb-2">
        <div className="relative h-[380px] w-full">
          <div ref={chartContainerRef} className="absolute inset-0" />
          {hover && (
            <div
              className="pointer-events-none absolute z-20 max-w-[260px] rounded border border-border bg-popover px-2 py-1 text-xs text-popover-foreground shadow-md"
              style={{
                // Clamped so a tooltip near the right or bottom edge stays on
                // screen instead of being clipped by the chart container.
                left: Math.min(hover.x + 12, 640),
                top: Math.min(hover.y + 12, 320),
              }}
            >
              {describeBucket(hover.bucket)}
            </div>
          )}
          {isFetching && !ohlcv && (
            <div className="absolute inset-0 flex flex-col items-center justify-center bg-muted/20 animate-pulse rounded-md z-10">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground mb-2" />
              <p className="text-sm text-muted-foreground">Loading chart...</p>
            </div>
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

function MarkerToggle({
  label,
  swatch,
  checked,
  onChange,
}: {
  label: string;
  swatch: string;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-1.5 select-none">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="h-3 w-3 accent-primary"
      />
      <span
        aria-hidden
        className="inline-block h-2 w-2 rounded-full"
        style={{ backgroundColor: swatch }}
      />
      {label}
    </label>
  );
}

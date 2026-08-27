import { useEffect, useRef, useState } from 'react';
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

export function TokenChart({ base }: { base: string }) {
  const [chartInterval, setChartInterval] = useState(15);
  const { data: ohlcv, isFetching } = useOHLCV(base, chartInterval);
  const { data: episodes } = useTokenEpisodes(base);

  type ChartApi = ReturnType<typeof createChart>;
  type SeriesApi = ReturnType<ChartApi['addSeries']>;

  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ChartApi | null>(null);
  const seriesRef = useRef<SeriesApi | null>(null);
  const markersRef = useRef<any>(null);
  const selectedInterval = getInterval(chartInterval);

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

    if (episodes && episodes.length > 0) {
      const getPeakColor = (pct: number) => {
        if (pct >= 100) return '#f87171';
        if (pct >= 50) return '#fb923c';
        return '#facc15';
      };

      const candleTimes = ohlcv.candles.map((c) => c.time);
      const getNearestCandleTime = (ts: number) => {
        if (!candleTimes.length) return ts as UTCTimestamp;
        let closest = candleTimes[0];
        for (const ct of candleTimes) {
          if (ct <= ts) closest = ct;
          else break;
        }
        return closest as UTCTimestamp;
      };

      const markers: SeriesMarker<UTCTimestamp>[] = episodes
        .filter((e) => e.first_seen_at)
        .map((e) => ({
          time: getNearestCandleTime(e.first_seen_at),
          position: 'aboveBar',
          color: getPeakColor(e.observed_peak_pct),
          shape: 'circle',
          size: 1,
        }))
        .sort((a, b) => (a.time as number) - (b.time as number));

      // Deduplicate by time (lightweight-charts crashes on duplicate times)
      const seenTimes = new Set<number>();
      const uniqueMarkers: SeriesMarker<UTCTimestamp>[] = [];
      for (const m of markers) {
        if (!seenTimes.has(m.time as number)) {
          seenTimes.add(m.time as number);
          uniqueMarkers.push(m);
        }
      }

      if (markersRef.current) markersRef.current.setMarkers(uniqueMarkers);
    }

    chartRef.current?.timeScale().fitContent();
  }, [ohlcv, episodes]);

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
      </CardHeader>
      <CardContent className="p-0 pb-2">
        <div className="relative h-[380px] w-full">
          <div ref={chartContainerRef} className="absolute inset-0" />
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

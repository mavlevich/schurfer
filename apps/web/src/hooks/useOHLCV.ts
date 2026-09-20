import { keepPreviousData, useQuery } from '@tanstack/react-query';
import type { OHLCVResponse } from '@/pages/pumps/types';

export const INTERVALS = [
  { label: '5m', range: 'last 3d', minutes: 5, limit: 1000 },
  { label: '15m', range: 'last 10d', minutes: 15, limit: 1000 },
  { label: '1h', range: 'last 41d', minutes: 60, limit: 1000 },
  { label: '4h', range: 'last 166d', minutes: 240, limit: 1000 },
  { label: '1d', range: 'last 3y', minutes: 1440, limit: 1000 },
  { label: '1w', range: 'last 20y', minutes: 10080, limit: 1000 },
] as const;

export type IntervalMinutes = (typeof INTERVALS)[number]['minutes'];

export function getInterval(minutes: number) {
  return INTERVALS.find((i) => i.minutes === minutes) ?? INTERVALS[1];
}

// useOHLCV fetches candles for base at the given interval. Pass exchange to pin a
// specific source (the user picked it from the response's `sources`); omit it to
// let the server resolve the deterministic default (real futures route first).
export function useOHLCV(base: string | undefined, minutes: number, exchange?: string) {
  const iv = getInterval(minutes);
  return useQuery({
    queryKey: ['ohlcv', base, minutes, exchange ?? ''],
    queryFn: async () => {
      const params = new URLSearchParams({
        interval: String(iv.minutes),
        limit: String(iv.limit),
      });
      if (exchange) params.set('exchange', exchange);
      const res = await fetch(`/api/pumps/${encodeURIComponent(base!)}/ohlcv?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json() as Promise<OHLCVResponse>;
    },
    enabled: !!base,
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
    retry: (_count, err) => !String(err).includes('HTTP 4'),
    retryDelay: 2000,
  });
}
